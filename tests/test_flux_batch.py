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
        self.root = Path(temporary.name).resolve()
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

    def identity(self, scope=None, max_attempts=3):
        return run_batch.batch_identity(self.manifest, scope or ["a", "b"], 3600, max_attempts)

    def attempt(self, case_id, index=1):
        return self.output / "runs" / case_id / f"attempt_{index:02d}"

    def bind_batch(self, scope=None, max_attempts=3):
        save(self.output / "batch_identity.json", self.identity(scope, max_attempts))

    def stage(self, case_id, state="ready_for_flux", index=1, retry_context=None, **extra):
        directory = self.attempt(case_id, index)
        save(directory / "input_binding.json", {
            **{key: self.identity()[key] for key in run_batch.BINDING_KEYS},
            "case_id": case_id, "manifest_path": str(self.manifest),
        })
        if retry_context is not None:
            save(directory / "retry_context.json", retry_context)
        (directory / "flux_task.md").write_text("Synthetic Flux task", encoding="utf-8")
        save(directory / "status.json", {"case_id": case_id, "state": state, **extra})
        if state == "validated":
            save(directory / "result.json", {"case": next(c["identity"] for c in self.cases
                                                        if c["case_id"] == case_id)})
        return directory

    def submit(self, case_id, index=1):
        workspace = self.attempt(case_id, index) / "workspace"
        save(workspace / "checkpoint_plan.json", {"checkpoints": []})
        save(workspace / "global_summary.json", {"goals": []})
        for step in (1, 3):
            save(workspace / "step_judgments" / f"step_{step:03d}.json", {"step": step})

    def prepare(self, command, **kwargs):
        self.assertEqual(command[:2], [sys.executable, str(self.runtime / "run_case.py")])
        self.assertNotIn("--finalize", command)
        case_id = command[command.index("--case") + 1]
        directory = Path(command[command.index("--out") + 1])
        index = run_batch.attempt_index(directory)
        self.assertEqual(directory, self.attempt(case_id, index))
        self.assertFalse(directory.exists(), "Preparation must never overwrite an attempt")
        retry_context = None
        if "--retry-context" in command:
            retry_context = run_batch.read_json(Path(command[command.index("--retry-context") + 1]))
            self.assertEqual(retry_context["case_id"], case_id)
            self.assertEqual(retry_context["attempt_index"], index)
        self.stage(case_id, index=index, retry_context=retry_context)
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
             "task_path": str(self.attempt(case_id) / "flux_task.md"), "state": "ready_for_flux",
             "attempt_index": 1, "retry_of": None, "retry_required": False}
            for case_id in ("b", "a")])
        self.assertFalse((self.output / "batch.lock").exists())

    def test_default_concurrency_hint_is_valid_and_no_threads_are_required(self):
        self.assertEqual(self.run_cli("--cases", "a"), 0)
        self.assertIn(self.summary()["jobs"], range(1, 6))
        self.assertEqual(self.child.call_count, 1)
        self.assertFalse(self.attempt("b").exists())

    def test_invalid_jobs_wall_and_modes_fail_before_creating_output(self):
        for flags in (("--jobs", "0"), ("--jobs", "6"), ("--wall", "0"),
                      ("--max-attempts", "0"), ("--max-attempts", "11"),
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
        before = {path: data for path, data in self.snapshot_attempts().items()
                  if path.startswith("runs/a/attempt_01/")}
        metadata = (self.output / "batch_state.json").read_bytes()
        self.child.reset_mock()
        for flags in (("--resume", "--cases", "a"), ("--resume", "--cases", "b", "a"),
                      ("--resume", "--wall", "120"), ()):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "identical input scope"):
                self.run_cli(*flags)
        self.child.assert_not_called()
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, before)
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

    def test_resume_preserves_ready_validated_and_interrupted_attempts(self):
        self.bind_batch()
        self.stage("a", "ready_for_flux")
        self.stage("b", "validated")
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--resume"), 0)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "validated": 1})
        self.assertEqual(self.summary()["finished_records"], 1)
        self.assertEqual(self.snapshot_attempts(), before)
        self.child.assert_not_called()

        (self.output / "runs").rename(self.output / "old-runs")
        self.stage("a", "interrupted")
        self.stage("b", "interrupted")
        before = self.snapshot_attempts()
        self.assertEqual(self.run_cli("--resume"), 1)
        self.assertEqual(self.summary()["states"], {"interrupted": 2})
        self.assertEqual(self.summary()["finished_records"], 2)
        self.assertEqual(self.snapshot_attempts(), before)
        self.child.assert_not_called()

    def test_resume_creates_next_attempt_without_overwriting_partial_attempt(self):
        self.bind_batch()
        directory = self.attempt("a")
        directory.mkdir(parents=True)
        (directory / "existing.txt").write_text("Keep original", encoding="utf-8")
        self.stage("b")
        before = {path: data for path, data in self.snapshot_attempts().items()
                  if path.startswith("runs/a/attempt_01/")}
        self.assertEqual(self.run_cli("--resume"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, before)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 2})
        self.assertEqual(self.summary()["results"]["a"]["attempts_used"], 2)
        self.assertTrue((self.attempt("a", 2) / "retry_context.json").is_file())

    def test_ready_does_not_trigger_stop_on_invalid(self):
        self.bind_batch()
        self.stage("a")
        self.assertEqual(self.run_cli("--resume", "--stop-on-invalid"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.summary()["finished_records"], 0)

    def test_invalid_is_retried_with_previous_validation_errors(self):
        self.bind_batch(["a"])
        self.stage("a", "invalid", error="Step assembly: Undelivered evidence source S0007",
                   schema_errors=["Additional properties are not allowed ('type' was unexpected)"],
                   validation_errors=[])
        original = {path: data for path, data in self.snapshot_attempts().items()
                    if path.startswith("runs/a/attempt_01/")}
        self.assertEqual(self.run_cli("--resume", "--cases", "a"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, original)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertEqual(retry["previous_attempt"], "attempt_01")
        self.assertTrue(any("Undelivered evidence source S0007" in error for error in retry["errors"]))
        self.assertIn("Additional properties are not allowed ('type' was unexpected)",
                      retry["schema_errors"])
        self.assertTrue(any("已登记送达" in hint for hint in retry["repair_hints"]))
        self.assertEqual(self.summary()["state"], "prepared")
        self.assertEqual(self.summary()["results"]["a"]["state"], "ready_for_flux")

    def test_invalid_without_result_exports_attribution_from_global_summary(self):
        self.bind_batch(["a"])
        directory = self.stage("a", "invalid", validation_errors=["Step assembly: Undelivered evidence source"])
        save(directory / "workspace" / "global_summary.json", {
            "outcome": {"decision": "fail", "completion": "partial"},
            "attribution": {"initial": {
                "step": 2, "responsible_actor": "worker", "stage": "verification",
                "labels": {"l1": "framework", "l2": "worker", "l3": "no_goal_state_verification"},
            }},
            "synthesis": {"summary": "Invalid attempt attribution remains available."},
        })
        self.assertEqual(self.run_cli("--collect", "--cases", "a"), 1)
        summary = self.summary()["results"]["a"]["attribution_summary"]
        self.assertEqual(summary["decision"], "fail")
        self.assertEqual(summary["initial"]["l3"], "no_goal_state_verification")
        aggregate = run_batch.read_json(self.output / "attribution_results.json")
        case = aggregate["cases"][0]
        self.assertEqual(case["result_source"], "global_summary")
        self.assertEqual(case["synthesis"]["summary"], "Invalid attempt attribution remains available.")

    def test_unusable_result_falls_back_to_global_summary(self):
        self.bind_batch(["a"])
        directory = self.stage("a", "invalid")
        save(directory / "result.json", {"case": {"id": "a"}})
        save(directory / "workspace" / "global_summary.json", {
            "outcome": {"decision": "fail", "completion": "partial"},
            "attribution": {"initial": {"step": 1, "responsible_actor": "worker",
                                        "stage": "planning", "labels": {"l3": "wrong_goal"}}},
            "synthesis": {"summary": "Fallback attribution."},
        })
        self.assertEqual(self.run_cli("--collect", "--cases", "a"), 1)
        aggregate = run_batch.read_json(self.output / "attribution_results.json")
        case = aggregate["cases"][0]
        self.assertEqual(case["result_source"], "global_summary")
        self.assertEqual(case["attribution"]["initial"]["labels"]["l3"], "wrong_goal")

    def test_retry_keeps_the_invalid_attempt_attribution_in_the_batch_export(self):
        self.bind_batch(["a"])
        directory = self.stage("a", "invalid", validation_errors=["Step assembly: Undelivered evidence source"])
        save(directory / "workspace" / "global_summary.json", {
            "outcome": {"decision": "fail", "completion": "partial"},
            "attribution": {"initial": {"step": 1, "responsible_actor": "worker",
                                        "stage": "verification", "labels": {"l3": "wrong_target"}}},
            "synthesis": {"summary": "Retained invalid attempt."},
        })
        self.assertEqual(self.run_cli("--resume", "--cases", "a"), 0)
        aggregate = run_batch.read_json(self.output / "attribution_results.json")
        attempts = {item["attempt"]: item for item in aggregate["cases"]}
        self.assertTrue(attempts["attempt_01"]["available"])
        self.assertEqual(attempts["attempt_01"]["outcome"]["decision"], "fail")
        self.assertEqual(attempts["attempt_02"]["state"], "ready_for_flux")

    def test_retry_hints_cover_windows_schema_contamination_and_coverage_errors(self):
        hints = run_batch.repair_hints([
            "Schema $.goals[0].extra: unexpected field",
            "confirmed contamination needs targeted reset requirements",
            "complete review lacks complete runtime access events",
            "success decision requires complete review",
        ])
        self.assertTrue(any("global_summary" in hint for hint in hints))
        self.assertTrue(any("baseline" in hint for hint in hints))
        self.assertTrue(any("coverage" in hint for hint in hints))
        self.assertTrue(any("success/fail" in hint for hint in hints))

    def test_retry_budget_exhaustion_is_explicit_and_terminal(self):
        self.bind_batch(["a"], max_attempts=2)
        self.stage("a", "invalid", error="Still invalid")
        self.stage("a", "invalid", index=2, error="Still invalid after retry")
        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "2"), 1)
        self.child.assert_not_called()
        record = self.summary()["results"]["a"]
        self.assertEqual(record["state"], "unresolved_after_retries")
        self.assertEqual(record["last_state"], "invalid")
        self.assertTrue(record["retry_exhausted"])
        self.assertEqual(self.summary()["state"], "completed_with_unresolved")

    def test_pause_keeps_remaining_scope_explicit(self):
        self.bind_batch()
        (self.output / "PAUSE").touch()
        self.assertEqual(self.run_cli("--resume"), 1)
        self.assertEqual(self.summary()["states"], {"not_dispatched": 2})
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
        self.assertEqual(self.summary()["state"], "awaiting_retry")
        self.assertEqual(self.summary()["finished_records"], 1)
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
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "runtime_failed": 1})
        self.assertEqual(self.summary()["finished_records"], 0)
        self.child.assert_not_called()

    def test_finalize_incomplete_submission_creates_retry_without_changing_old_attempt(self):
        self.bind_batch()
        self.stage("a")
        workspace = self.attempt("a") / "workspace"
        save(workspace / "checkpoint_plan.json", {})
        save(workspace / "global_summary.json", {})
        save(workspace / "step_judgments/step_001.json", {})
        before = {path: data for path, data in self.snapshot_attempts().items()
                  if path.startswith("runs/a/attempt_01/")}
        self.assertEqual(self.run_cli("--finalize"), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, before)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "not_dispatched": 1})
        self.assertEqual(self.summary()["results"]["a"]["attempts_used"], 2)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertIn("before all required submissions were complete", retry["errors"][0])

    def test_finalize_ready_without_local_check_triggers_retry_not_validation(self):
        self.bind_batch()
        self.stage("a")
        self.stage("b")
        self.submit("a")
        self.submit("b")
        status_before = (self.attempt("a") / "status.json").read_bytes()
        def still_ready(command, **kwargs):
            kwargs["stdout"].write(b'{"state":"validated"}\n')
            if "--finalize" in command:
                return subprocess.CompletedProcess(command, 1)
            return self.prepare(command, **kwargs)
        self.child.side_effect = still_ready
        self.assertEqual(self.run_cli("--finalize"), 1)
        self.assertEqual(self.child.call_count, 4)
        self.assertEqual(self.summary()["finished_records"], 0)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 2})
        self.assertEqual({record["attempts_used"] for record in self.summary()["results"].values()}, {2})
        self.assertEqual((self.attempt("a") / "status.json").read_bytes(), status_before)
        self.assertFalse((self.attempt("a") / "result.json").exists())

    def test_finalize_invalid_schedules_fresh_retry_and_then_validates(self):
        self.bind_batch()
        self.stage("a")
        self.submit("a")
        self.stage("b", "validated")
        before_b = {p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()}
        attempt_one = {p.name: p.read_bytes() for p in self.attempt("a").iterdir()
                       if p.is_file() and p.name != "status.json"}

        def finalize_then_prepare(command, **kwargs):
            if "--finalize" in command:
                self.assertEqual(command[command.index("--case") + 1], "a")
                self.stage("a", "invalid", error="Step assembly: Undelivered evidence source S0042")
                return subprocess.CompletedProcess(command, 1)
            return self.prepare(command, **kwargs)

        self.child.side_effect = finalize_then_prepare
        self.assertEqual(self.run_cli("--finalize"), 1)
        self.assertEqual(self.child.call_count, 2)
        self.assertEqual(self.summary()["states"], {"ready_for_flux": 1, "validated": 1})
        self.assertEqual(self.summary()["finished_records"], 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.attempt("a").iterdir()
                          if p.is_file() and p.name != "status.json"},
                         attempt_one)
        self.assertEqual(run_batch.read_json(self.attempt("a") / "status.json")["state"], "invalid")
        self.assertEqual({p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()}, before_b)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertEqual(retry["previous_state"], "invalid")
        self.assertTrue(any("Undelivered evidence source S0042" in error for error in retry["errors"]))

        self.stage("a", "validated", index=2)
        self.assertEqual(self.run_cli("--finalize"), 0)
        self.assertEqual(self.child.call_count, 2)
        self.assertEqual(self.summary()["state"], "completed")
        self.assertEqual(self.summary()["states"], {"validated": 2})

    def test_finalize_uses_real_validated_status_and_preserves_other_result(self):
        self.bind_batch()
        self.stage("a")
        self.submit("a")
        self.stage("b", "validated")
        before_b = {p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()}
        def finalize(command, **kwargs):
            self.stage("a", "validated")
            return subprocess.CompletedProcess(command, 0)
        self.child.side_effect = finalize
        self.assertEqual(self.run_cli("--finalize"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.summary()["finished_records"], 2)
        self.assertEqual(self.summary()["results"]["a"]["state"], "validated")
        self.assertEqual({p.name: p.read_bytes() for p in self.attempt("b").iterdir() if p.is_file()},
                         before_b)

    def test_finalize_changed_binding_creates_retry_without_touching_old_attempt(self):
        self.bind_batch(["a"], max_attempts=2)
        self.stage("a")
        self.submit("a")
        save(self.attempt("a") / "input_binding.json", {"case_id": "a", "manifest_sha256": "wrong"})
        original = {path: data for path, data in self.snapshot_attempts().items()
                    if path.startswith("runs/a/attempt_01/")}
        self.assertEqual(self.run_cli("--finalize", "--cases", "a", "--max-attempts", "2"), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, original)
        self.assertEqual(self.summary()["results"]["a"]["attempts_used"], 2)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertTrue(any("input binding differs" in error for error in retry["errors"]))

    def test_corrupt_status_creates_retry_instead_of_stalling(self):
        self.bind_batch(["a"])
        self.stage("a")
        (self.attempt("a") / "status.json").write_text("not JSON", encoding="utf-8")
        original = {path: data for path, data in self.snapshot_attempts().items()
                    if path.startswith("runs/a/attempt_01/")}
        self.assertEqual(self.run_cli("--resume", "--cases", "a"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, original)
        self.assertEqual(self.summary()["results"]["a"]["attempts_used"], 2)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertTrue(any("Existing status is unreadable" in error for error in retry["errors"]))

    def test_non_object_status_creates_retry_instead_of_crashing(self):
        self.bind_batch(["a"])
        self.stage("a")
        save(self.attempt("a") / "status.json", ["not", "an", "object"])
        self.assertEqual(self.run_cli("--resume", "--cases", "a"), 0)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.summary()["results"]["a"]["attempts_used"], 2)
        retry = run_batch.read_json(self.attempt("a", 2) / "retry_context.json")
        self.assertTrue(any("no valid state" in error for error in retry["errors"]))

    def test_corrupt_attempt_exhausts_budget_as_unresolved_not_awaiting_flux(self):
        self.bind_batch(["a"], max_attempts=1)
        self.stage("a")
        save(self.attempt("a") / "status.json", {"case_id": "a", "state": "mystery"})
        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "1"), 1)
        self.child.assert_not_called()
        record = self.summary()["results"]["a"]
        self.assertEqual(record["state"], "unresolved_after_retries")
        self.assertEqual(record["last_state"], "runtime_failed")
        self.assertTrue(record["retry_exhausted"])
        self.assertEqual(self.summary()["state"], "completed_with_unresolved")

    def test_launcher_timeout_before_attempt_directory_is_bounded(self):
        self.bind_batch(["a"], max_attempts=2)
        self.child.side_effect = subprocess.TimeoutExpired(["local preparation"], 1)
        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "2"), 1)
        first = {path: data for path, data in self.snapshot_attempts().items()
                 if path.startswith("runs/a/attempt_01/")}
        self.assertTrue((self.attempt("a") / "launch_failure.json").is_file())
        self.assertEqual(self.summary()["state"], "awaiting_retry")

        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "2"), 1)
        self.assertEqual(self.child.call_count, 2)
        self.assertEqual({path: data for path, data in self.snapshot_attempts().items()
                          if path.startswith("runs/a/attempt_01/")}, first)
        self.assertEqual(self.summary()["results"]["a"]["state"], "unresolved_after_retries")

        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "2"), 1)
        self.assertEqual(self.child.call_count, 2)

    def test_launcher_nonzero_without_attempt_directory_is_bounded(self):
        self.bind_batch(["a"], max_attempts=1)
        self.child.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(command, 2)
        self.assertEqual(self.run_cli("--resume", "--cases", "a", "--max-attempts", "1"), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertTrue((self.attempt("a") / "launch_failure.json").is_file())
        self.assertEqual(self.summary()["results"]["a"]["state"], "unresolved_after_retries")
        self.assertEqual(self.summary()["state"], "completed_with_unresolved")

    def test_launcher_timeout_releases_batch_lock_after_scope_attempts(self):
        self.child.side_effect = subprocess.TimeoutExpired(["local preparation"], 1)
        self.assertEqual(self.run_cli("--stop-on-invalid"), 1)
        self.assertEqual(self.child.call_count, 2)
        self.assertEqual(self.summary()["states"], {"runtime_failed": 2})
        self.assertFalse((self.output / "batch.lock").exists())


if __name__ == "__main__":
    unittest.main()
