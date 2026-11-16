"""Offline batch state regressions; synthetic subprocesses are not attribution tests."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stepwise"))
import run_batch


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class TestFluxBatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime" / "stepwise"
        save(self.runtime.parent / "RELEASE_MANIFEST.json", {"files": {}})
        save(self.runtime / "skills/v3/manifest.json", {"files": {}})
        self.manifest = self.root / "manifest.json"
        self.cases = [{"case_id": case_id, "identity": {"id": case_id, "attempt": 1},
                       "available_steps": [1, 3]} for case_id in ("a", "b")]
        save(self.manifest, {"cases": self.cases})
        self.output = self.root / "batch"
        root_patch = patch.object(run_batch, "ROOT", self.runtime)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        release_patch = patch.object(run_batch, "verify_release")
        release_patch.start()
        self.addCleanup(release_patch.stop)
        subprocess_patch = patch.object(run_batch.subprocess, "run", side_effect=self.prepare)
        self.child = subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

    def identity(self, scope=None):
        return run_batch.batch_identity(self.manifest, scope or ["a", "b"], 3600)

    def attempt(self, case_id):
        return self.output / "runs" / case_id / "attempt_01"

    def bind_batch(self, scope=None):
        save(self.output / "batch_identity.json", self.identity(scope))

    def stage(self, case_id, state="ready_for_flux"):
        directory = self.attempt(case_id)
        save(directory / "input_binding.json", {
            **{key: self.identity()[key] for key in run_batch.BINDING_KEYS},
            "case_id": case_id, "manifest_path": str(self.manifest),
        })
        (directory / "flux_task.md").write_text("Synthetic Flux task", encoding="utf-8")
        save(directory / "status.json", {"case_id": case_id, "state": state})
        if state == "validated":
            save(directory / "result.json", {"case": next(c["identity"] for c in self.cases
                                                        if c["case_id"] == case_id)})
        return directory

    def submit(self, case_id):
        workspace = self.attempt(case_id) / "workspace"
        save(workspace / "checkpoint_plan.json", {"checkpoints": []})
        save(workspace / "global_summary.json", {"goals": []})
        for step in (1, 3):
            save(workspace / "step_judgments" / f"step_{step:03d}.json", {"step": step})

    def prepare(self, command, **kwargs):
        self.assertEqual(command[:2], [sys.executable, str(self.runtime / "run_case.py")])
        self.assertNotIn("--finalize", command)
        case_id = command[command.index("--case") + 1]
        directory = Path(command[command.index("--out") + 1])
        self.assertEqual(directory, self.attempt(case_id))
        self.assertFalse(directory.exists(), "Preparation must never overwrite an attempt")
        self.stage(case_id)
        kwargs["stdout"].write(b'{"state":"ready_for_flux"}\n')
        return subprocess.CompletedProcess(command, 0)

    def run_cli(self, *flags):
        argv = ["run_batch.py", "--manifest", str(self.manifest), "--out", str(self.output), *flags]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return run_batch.main()

    def summary(self):
        return run_batch.read_json(self.output / "batch_state.json")

    def snapshot_attempts(self):
        return {path.relative_to(self.output).as_posix(): path.read_bytes()
                for path in (self.output / "runs").rglob("*") if path.is_file()}

    def test_prepared_is_not_completed_and_commands_use_bound_scope(self):
        self.assertEqual(self.run_cli("--cases", "b", "a", "--jobs", "5"), 0)
        summary = self.summary()
        self.assertEqual(summary["state"], "prepared")
        self.assertEqual(summary["selected"], 2)
        self.assertEqual(summary["finished_records"], 0)
        self.assertEqual(summary["states"], {"ready_for_flux": 2})
        self.assertEqual(list(summary["results"]), ["b", "a"])
        commands = [call.args[0] for call in self.child.call_args_list]
        self.assertEqual([command[command.index("--case") + 1] for command in commands], ["b", "a"])
        binding = self.identity(["b", "a"])
        for command in commands:
            self.assertEqual(command[2::2], ["--manifest", "--manifest-sha256", "--release-sha256",
                                          "--skill-manifest-sha256", "--case", "--out", "--wall"])
            for flag, key in (("--manifest-sha256", "manifest_sha256"),
                              ("--release-sha256", "runtime_manifest_sha256"),
                              ("--skill-manifest-sha256", "skill_manifest_sha256")):
                self.assertEqual(command[command.index(flag) + 1], binding[key])
        dispatch = run_batch.read_json(self.output / "dispatch.json")
        self.assertEqual(dispatch["jobs"], 5)
        self.assertEqual(dispatch["batch_identity"], binding)
        self.assertEqual(dispatch["tasks"], [
            {"case_id": case_id, "out": str(self.attempt(case_id)),
             "task_path": str(self.attempt(case_id) / "flux_task.md"), "state": "ready_for_flux"}
            for case_id in ("b", "a")])
        self.assertFalse((self.output / "batch.lock").exists())

    def test_default_concurrency_hint_is_valid_and_no_threads_are_required(self):
        self.assertEqual(self.run_cli("--cases", "a"), 0)
        self.assertIn(self.summary()["jobs"], range(1, 6))
        self.assertEqual(self.child.call_count, 1)
        self.assertFalse(self.attempt("b").exists())

    def test_invalid_jobs_wall_and_modes_fail_before_creating_output(self):
        for flags in (("--jobs", "0"), ("--jobs", "6"), ("--wall", "0"),
                      ("--collect", "--finalize"), ("--env-file", "unused")):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as error:
                self.run_cli(*flags)
            self.assertEqual(error.exception.code, 2)
        self.child.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_scope_rejects_duplicates_unknown_and_unsafe_case_ids(self):
        for requested in (["a", "a"], ["missing"], []):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                run_batch.selected_cases({"cases": self.cases}, requested)
        for available in (("A", "a"), ("../escape",), ("a/b",), ("CON",)):
            with self.subTest(available=available), self.assertRaises(ValueError):
                run_batch.selected_cases({"cases": [{"case_id": value} for value in available]}, None)

    def test_resume_rejects_changed_scope_and_wall_without_overwriting(self):
        self.assertEqual(self.run_cli(), 0)
        before = self.snapshot_attempts()
        metadata = (self.output / "batch_state.json").read_bytes()
        self.child.reset_mock()
        for flags in (("--resume", "--cases", "a"), ("--resume", "--cases", "b", "a"),
                      ("--resume", "--wall", "120"), ()):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "identical input scope"):
                self.run_cli(*flags)
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)
        self.assertEqual((self.output / "batch_state.json").read_bytes(), metadata)

    def test_resume_collect_finalize_reject_every_changed_hash(self):
        self.assertEqual(self.run_cli(), 0)
        self.child.reset_mock()
        before = self.snapshot_attempts()
        for path in (self.manifest, self.runtime.parent / "RELEASE_MANIFEST.json",
                     self.runtime / "skills/v3/manifest.json"):
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            try:
                for flag in ("--resume", "--collect", "--finalize"):
                    with self.subTest(path=path.name, flag=flag), self.assertRaisesRegex(ValueError, "identical"):
                        self.run_cli(flag)
            finally:
                path.write_bytes(original)
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)

    def test_manifest_change_during_initialization_is_rejected(self):
        original_identity = run_batch.batch_identity
        def changed_identity(*args):
            self.manifest.write_bytes(self.manifest.read_bytes() + b"\n")
            return original_identity(*args)
        with patch.object(run_batch, "batch_identity", side_effect=changed_identity), \
                self.assertRaisesRegex(ValueError, "changed during"):
            self.run_cli()
        self.child.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_resume_preserves_prepared_pending_and_terminal_attempts(self):
        for state in ("ready_for_flux", "pending", "validated", "invalid", "runtime_failed",
                      "infra_failed", "interrupted"):
            with self.subTest(state=state):
                self.bind_batch()
                self.stage("a", state)
                self.stage("b", state)
                before = self.snapshot_attempts()
                self.assertEqual(self.run_cli("--resume"), 0 if state in ("ready_for_flux", "validated") else 1)
                self.assertEqual(self.summary()["finished_records"], 2 if state in run_batch.FINAL_STATES else 0)
                self.assertEqual(self.snapshot_attempts(), before)
        self.child.assert_not_called()

    def test_resume_does_not_overwrite_partial_attempt(self):
        self.bind_batch()
        directory = self.attempt("a")
        directory.mkdir(parents=True)
        (directory / "existing.txt").write_text("Keep original", encoding="utf-8")
        self.stage("b")
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--resume"), 1)
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)
        self.assertEqual(self.summary()["states"], {"pending": 1, "ready_for_flux": 1})
        self.assertEqual(self.summary()["finished_records"], 0)

    def test_ready_does_not_trigger_stop_on_invalid(self):
        self.bind_batch()
        self.stage("a")
        self.assertEqual(self.run_cli("--resume", "--stop-on-invalid"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.summary()["finished_records"], 0)

    def test_invalid_and_pause_keep_remaining_scope_explicit(self):
        self.bind_batch()
        self.stage("a", "invalid")
        self.assertEqual(self.run_cli("--resume", "--stop-on-invalid"), 1)
        self.assertEqual(self.summary()["states"], {"invalid": 1, "not_dispatched": 1})
        self.child.assert_not_called()
        self.assertFalse(self.attempt("b").exists())
        (self.output / "PAUSE").touch()
        self.assertEqual(self.run_cli("--resume"), 1)
        self.child.assert_not_called()
        self.assertFalse(self.attempt("b").exists())

    def test_existing_lock_is_not_removed_or_overwritten(self):
        self.bind_batch()
        (self.output / "batch.lock").write_text("Existing owner", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.run_cli("--resume")
        self.assertEqual((self.output / "batch.lock").read_text(), "Existing owner")
        self.child.assert_not_called()

    def test_collect_and_finalize_require_existing_batch(self):
        for flag in ("--collect", "--finalize"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "existing bound batch"):
                self.run_cli(flag)
        self.child.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_collect_only_reads_statuses_and_never_prepares_missing_case(self):
        self.bind_batch()
        self.stage("a")
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--collect"), 1)
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)
        self.assertFalse(self.attempt("b").exists())
        summary = self.summary()
        self.assertEqual(summary["state"], "awaiting_flux")
        self.assertEqual(summary["finished_records"], 0)
        self.assertEqual(summary["states"], {"ready_for_flux": 1, "not_dispatched": 1})

    def test_collect_final_results_preserves_real_validation_states(self):
        self.bind_batch()
        self.stage("a", "validated")
        self.stage("b", "invalid")
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--collect"), 1)
        self.assertEqual(self.summary()["state"], "completed")
        self.assertEqual(self.summary()["finished_records"], 2)
        self.assertEqual(self.summary()["states"], {"validated": 1, "invalid": 1})
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)

    def test_collect_rejects_wrong_binding_status_and_result_identity(self):
        self.bind_batch()
        self.stage("b")
        corruptions = (
            ("input_binding.json", {"case_id": "a", "manifest_sha256": "changed"}),
            ("status.json", {"case_id": "b", "state": "validated"}),
            ("result.json", {"case": {"id": "b", "attempt": 1}}),
            ("result.json", {"case": {"id": "a", "attempt": 2}}),
        )
        for name, value in corruptions:
            with self.subTest(name=name, value=value):
                self.stage("a", "validated")
                save(self.attempt("a") / name, value)
                self.assertEqual(self.run_cli("--collect"), 1)
                self.assertEqual(self.summary()["results"]["a"]["state"], "runtime_failed")
        self.child.assert_not_called()

    def test_collect_does_not_trust_unreadable_or_resultless_validated_status(self):
        self.bind_batch()
        self.stage("b")
        for broken in ("result", "status", "binding"):
            with self.subTest(broken=broken):
                self.stage("a", "validated")
                path = self.attempt("a") / {"result": "result.json", "status": "status.json",
                                           "binding": "input_binding.json"}[broken]
                path.write_text("not JSON", encoding="utf-8")
                self.assertEqual(self.run_cli("--collect"), 1)
                self.assertEqual(self.summary()["results"]["a"]["state"], "runtime_failed")
        self.child.assert_not_called()

    def test_missing_status_is_ready_only_when_bound_prompt_exists(self):
        self.bind_batch()
        self.stage("a")
        self.stage("b")
        (self.attempt("a") / "status.json").unlink()
        (self.attempt("b") / "flux_task.md").unlink()
        self.assertEqual(self.run_cli("--collect"), 1)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "pending": 1})
        self.assertEqual(self.summary()["finished_records"], 0)
        self.child.assert_not_called()

    def test_finalize_leaves_missing_or_incomplete_tasks_untouched(self):
        self.bind_batch()
        self.stage("a")
        workspace = self.attempt("a") / "workspace"
        save(workspace / "checkpoint_plan.json", {})
        save(workspace / "global_summary.json", {})
        save(workspace / "step_judgments/step_001.json", {})
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--finalize"), 1)
        self.child.assert_not_called()
        self.assertEqual(self.snapshot_attempts(), before)
        self.assertEqual(self.summary()["finished_records"], 0)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "not_dispatched": 1})

    def test_finalize_nonzero_ready_and_zero_stdout_do_not_fake_validation(self):
        self.bind_batch()
        self.stage("a")
        self.stage("b")
        self.submit("a")
        status_before = (self.attempt("a") / "status.json").read_bytes()
        for returncode in (1, 0):
            with self.subTest(returncode=returncode):
                self.child.reset_mock()
                def still_ready(command, **kwargs):
                    self.assertEqual(command[-1], "--finalize")
                    kwargs["stdout"].write(b'{"state":"validated"}\n')
                    return subprocess.CompletedProcess(command, returncode)
                self.child.side_effect = still_ready
                self.assertEqual(self.run_cli("--finalize"), 1)
                self.assertEqual(self.child.call_count, 1)
                self.assertEqual(self.summary()["finished_records"], 0)
                self.assertEqual(self.summary()["states"], {"ready_for_flux": 2})
                self.assertEqual((self.attempt("a") / "status.json").read_bytes(), status_before)
        self.assertFalse((self.attempt("a") / "result.json").exists())

    def test_finalize_uses_real_status_and_preserves_existing_final_result(self):
        self.bind_batch()
        self.stage("a")
        self.submit("a")
        self.stage("b", "validated")
        before_b = {p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()}
        for final_state in ("invalid", "validated"):
            with self.subTest(final_state=final_state):
                self.stage("a")
                self.child.reset_mock()
                def finalize(command, **kwargs):
                    self.assertEqual(command[-1], "--finalize")
                    self.assertEqual(command[command.index("--case") + 1], "a")
                    self.stage("a", final_state)
                    return subprocess.CompletedProcess(command, 0 if final_state == "validated" else 1)
                self.child.side_effect = finalize
                self.assertEqual(self.run_cli("--finalize"), 0 if final_state == "validated" else 1)
                self.assertEqual(self.child.call_count, 1)
                self.assertEqual(self.summary()["finished_records"], 2)
                self.assertEqual(self.summary()["results"]["a"]["state"], final_state)
                self.assertEqual({p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()}, before_b)

    def test_finalize_never_calls_case_with_changed_binding(self):
        self.bind_batch()
        self.stage("a")
        self.submit("a")
        save(self.attempt("a") / "input_binding.json", {"case_id": "a", "manifest_sha256": "wrong"})
        self.assertEqual(self.run_cli("--finalize"), 1)
        self.child.assert_not_called()
        self.assertEqual(self.summary()["results"]["a"]["state"], "runtime_failed")

    def test_launcher_timeout_releases_batch_lock_and_preserves_other_scope(self):
        self.child.side_effect = subprocess.TimeoutExpired(["local preparation"], 1)
        self.assertEqual(self.run_cli("--stop-on-invalid"), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.summary()["states"], {"runtime_failed": 1, "not_dispatched": 1})
        self.assertFalse((self.output / "batch.lock").exists())


if __name__ == "__main__":
    unittest.main()
