"""Offline protocol tests; fixtures are synthetic, never human GT."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stepwise"))
import run_case
import run_batch

ENGINE = run_case.load_engine()
from jsonschema import Draft202012Validator
from harness.protocol import digest
import stepwise_support_v2 as protocol
import stepwise_support_fast as prompt_builder
import candidate_bridge
from normalization_audit import changes


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ReleaseTests(unittest.TestCase):
    def test_export_integrity(self):
        run_case.verify_release()

    def test_frozen_skill_hashes(self):
        base = ROOT / "stepwise/skills/v3"
        manifest = run_case.read_json(base / "manifest.json")
        self.assertEqual(manifest["skill_version"], "0.15.2")
        for relative, expected in manifest["files"].items():
            self.assertEqual(run_case.sha256(base / "gui-trajectory-attribution" / relative), expected)

    def test_all_json_schemas_are_well_formed(self):
        for path in [
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/output.schema.json",
            ROOT / "docs/examples/step_record.schema.json",
            ROOT / "docs/examples/global_summary.schema.json",
        ]:
            Draft202012Validator.check_schema(run_case.read_json(path))

    def test_selection_rejects_duplicate_ids(self):
        manifest = {"cases": [{"case_id": "a"}, {"case_id": "b"}]}
        self.assertEqual(run_batch.selected_cases(manifest, None), ["a", "b"])
        with self.assertRaises(ValueError):
            run_batch.selected_cases(manifest, ["a", "a"])
        with self.assertRaises(ValueError):
            run_batch.selected_cases(manifest, ["missing"])

    def test_selection_rejects_path_case_ids(self):
        for case_id in ("../outside", "a/b", "a\\b", ".", "..", "C:outside", "NUL", "CON.txt", "case_001."):
            with self.subTest(case_id=case_id), self.assertRaises(ValueError):
                run_batch.selected_cases({"cases": [{"case_id": case_id}]}, None)

    def test_windows_case_aliases_are_rejected(self):
        with self.assertRaises(ValueError):
            run_batch.selected_cases({"cases": [{"case_id": "Case_1"}, {"case_id": "case_1"}]}, None)

    def test_missing_explicit_env_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "environment file"):
                run_case.load_environment(Path(root) / "missing.env")

    def test_cli_prerelease_is_not_accepted_as_pinned_release(self):
        with patch.dict(run_case.os.environ, {"CODEX_BINARY": '["synthetic-codex"]'}), patch.object(run_case.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="codex-cli 0.166.0-beta.1\n", stderr="")):
            with self.assertRaisesRegex(ValueError, "requires Codex"):
                run_case.cli_command()

    def test_validator_cli_finds_default_references(self):
        with tempfile.TemporaryDirectory() as root:
            result = Path(root) / "result.json"
            save(result, {})
            completed = subprocess.run([sys.executable, str(ROOT / "stepwise/engine/validate_result.py"), str(result)], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn("INVALID", completed.stdout)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        evidence = root / "evidence"
        image = evidence / "images/frame.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"synthetic fixture bytes; never sent to a model")
        log = evidence / "step_1/manager.json"
        save(log, {"response": "Synthetic module output"})
        catalog = ENGINE.SourceCatalog([
            {"source": "images/frame.png", "kind": "image", "step": None, "sha256": run_case.sha256(image)},
            {"source": "step_1/manager.json", "kind": "log", "step": 1, "sha256": run_case.sha256(log)},
        ])
        ordered = [{"display_index": 0, "source": "images/frame.png", "sha256": run_case.sha256(image)}]
        registry = run_case.read_json(ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/template-registry.json")
        identity = {"id": "synthetic", "framework": "mobile_agent_v3", "dataset": "Synthetic",
            "task_id": "synthetic::1", "instruction": "Inspect synthetic fixture", "attempt": "fixture",
            "template_id": "mobile_agent_v3", "template_sha256": registry["templates"]["mobile_agent_v3"]["sha256"],
            "policy_id": registry["policy_id"], "adapter_version": "unknown"}
        self.case = {"case_id": "synthetic", "identity": identity, "evidence_root": "evidence",
            "inventory": catalog.entries, "available_steps": [1], "image_count": 1,
            "images": [{**catalog.by_path["images/frame.png"], "delivery_path": "evidence/images/frame.png", "delivery_sha256": run_case.sha256(image)}],
            "image_scope": {"mode": "human_visible_images", "ordered_images": ordered, "sha256": digest(ordered)}}
        self.manifest = {"cases": [self.case], "image_delivery": {"mode": "q85_webp_cas", "cache_root": "."}}
        self.path = root / "manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        save(self.path, self.manifest)
        return run_case.load_inputs(ENGINE, self.path, "synthetic")

    def test_relative_evidence_paths(self):
        self.assertEqual(self.load()[0]["image_count"], 1)

    def test_false_image_count_is_rejected(self):
        self.case["image_count"] = 0
        with self.assertRaisesRegex(ValueError, "Image count"):
            self.load()

    def test_missing_delivery_image_is_rejected(self):
        self.case["images"] = []
        with self.assertRaisesRegex(ValueError, "Image count"):
            self.load()

    def test_template_mismatch_is_rejected(self):
        self.case["identity"]["template_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "template identity"):
            self.load()

    def test_noncanonical_source_id_is_rejected(self):
        self.case["inventory"][0]["source_id"] = "S9999"
        with self.assertRaisesRegex(ValueError, "Source IDs"):
            self.load()

    def test_changed_dispatch_manifest_is_rejected(self):
        save(self.path, self.manifest)
        old = run_case.sha256(self.path)
        self.case["identity"]["instruction"] = "Changed synthetic task"
        save(self.path, self.manifest)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            run_case.load_inputs(ENGINE, self.path, "synthetic", old)


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.case = {"available_steps": [1], "inventory": [
            {"source_id": "S0001", "source": "step_1/manager.json", "kind": "log", "step": 1},
            {"source_id": "S0002", "source": "images/frame.png", "kind": "image", "step": None},
        ]}
        self.observation = {"source_id": "S0001", "phase": "decision", "locator": "response",
                            "observation": "Synthetic module output"}
        self.record = {"step": 1, "verdict": "uncertain", "summary": "Synthetic local observation",
                       "evidence": [self.observation]}
        save(self.workspace / "step_judgments/step_001.json", self.record)
        save(self.workspace / "global_summary.json", {})
        self.receipt = {"events": [
            {"source": entry["source"], "success": True, "content_complete": True, "truncated": False}
            for entry in self.case["inventory"]
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def test_assembly_binds_evidence_and_coverage(self):
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual(result["evidence"][0]["id"], "E01_1")
        self.assertTrue(result["coverage"]["scan_complete"])
        self.assertEqual(result["coverage"]["screenshots_viewed"], ["S0002"])

    def test_missing_step_is_rejected(self):
        (self.workspace / "step_judgments/step_001.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_undelivered_citation_is_rejected(self):
        self.receipt["events"] = self.receipt["events"][1:]
        with self.assertRaisesRegex(ValueError, "Undelivered"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_unviewed_image_keeps_coverage_incomplete(self):
        self.receipt["events"] = self.receipt["events"][:1]
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertFalse(result["coverage"]["scan_complete"])
        self.assertEqual(result["coverage"]["missing"], ["images/frame.png"])

    def test_additive_revision_preserves_prior_ids(self):
        revised = copy.deepcopy(self.record)
        revised["evidence"].append({**self.observation, "source_id": "S0002", "phase": "unknown"})
        save(self.workspace / "step_judgments/revision_001.json", revised)
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual([e["id"] for e in result["evidence"]], ["E01_1", "E01_2"])
        self.assertIsNone(result["evidence"][1]["step"])

    def test_revision_cannot_change_local_verdict(self):
        revised = {**self.record, "verdict": "error"}
        save(self.workspace / "step_judgments/revision_001.json", revised)
        with self.assertRaisesRegex(ValueError, "verdict"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_schema_rejects_string_step(self):
        with self.assertRaises(Exception):
            Draft202012Validator(protocol.STEP_SCHEMA).validate({**self.record, "step": "1"})


class PromptTests(unittest.TestCase):
    def test_identity_quotes_and_newlines_remain_json_data(self):
        identity = {"id": "synthetic", "instruction": 'Inspect "a"\n__TASK_IDENTITY_JSON__'}
        prompt = protocol.render_prompt(identity)
        line = next(line for line in prompt.splitlines() if line.startswith("TASK IDENTITY: "))
        self.assertEqual(json.loads(line.removeprefix("TASK IDENTITY: ")), identity)

    def test_missing_or_duplicate_identity_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.txt"
            for template in ("no marker", "__TASK_IDENTITY_JSON__ __TASK_IDENTITY_JSON__"):
                path.write_text(template, encoding="utf-8")
                with patch.object(protocol, "PROMPT_PATH", path), self.assertRaises(ValueError):
                    protocol.render_prompt({"id": "synthetic"})

    def test_builder_stages_the_same_contract_and_helper_without_extra_prompt(self):
        identity = {"id": "synthetic"}
        case = {"identity": identity, "available_steps": [1], "inventory": [{"kind": "log", "step": 1, "source_id": "S0001"}]}
        class FakeProtocol:
            def wire_schema(self):
                return {"type": "object", "properties": {"coverage": {}, "evidence": {}, "process": {}, "outcome": {"type": "object"}}, "required": ["coverage", "evidence", "process", "outcome"]}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            generated = prompt_builder.build_prompt(case, FakeProtocol(), [], workspace)
            self.assertEqual(generated, protocol.render_prompt(identity))
            self.assertEqual(run_case.read_json(workspace / "step_record.schema.json"), protocol.STEP_SCHEMA)
            self.assertEqual(run_case.read_json(workspace / "global_summary.schema.json")["required"], ["outcome"])
            self.assertEqual((workspace / "io.ps1").read_bytes(), (ROOT / "stepwise/engine/io.ps1").read_bytes())
            self.assertEqual((workspace / "submission_protocol.md").read_bytes(), prompt_builder.SUBMISSION_PROTOCOL_PATH.read_bytes())

    def test_startup_routes_to_skill_and_staged_protocol(self):
        prompt = protocol.render_prompt({"id": "synthetic"})
        self.assertIn("$gui-trajectory-attribution", prompt)
        self.assertIn("submission_protocol.md", prompt)
        self.assertNotIn("MAIN causal failure", prompt)
        self.assertNotIn("PowerShell", prompt)


class BridgeTests(unittest.TestCase):
    def test_main_route_is_deepseek(self):
        request, route = candidate_bridge.convert({
            "model": "deepseek-v4.1-flash", "input": "Synthetic test",
        })
        self.assertEqual(request["model"], "deepseek-v4.1-flash")
        self.assertEqual(route["purpose"], "main_agent")

    def test_approval_requires_original_schema(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            candidate_bridge.convert({"model": "codex-auto-review", "input": "Synthetic approval"})

    def test_alias_round_trip_preserves_tool_identity(self):
        _, route = candidate_bridge.convert({
            "model": "deepseek-v4.1-flash", "input": "Synthetic test",
            "tools": [{"type": "namespace", "name": "functions", "tools": [
                {"type": "function", "name": "view_image", "parameters": {"type": "object"}}
            ]}],
        })
        events = candidate_bridge.response_events({"tool_calls": [{
            "id": "call_1", "type": "function", "function": {
                "name": "functions__view_image", "arguments": '{"path":"images/frame.png"}'
            }}]}, "deepseek-v4.1-flash", None, route["tool_aliases"])
        done = [event for event in events if event["type"] == "response.output_item.done"][0]["item"]
        self.assertEqual((done["namespace"], done["name"]), ("functions", "view_image"))


class BatchTests(unittest.TestCase):
    def test_resume_rejects_different_runtime_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}]})
            output = root / "batch"
            identity = run_batch.batch_identity(manifest, ["a"], 3600)
            identity["runtime_manifest_sha256"] = "0" * 64
            save(output / "batch_identity.json", identity)
            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(output), "--resume"]), patch.object(run_batch.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(ValueError, "identical input"):
                    run_batch.main()
                spawn.assert_not_called()
    def test_parallel_dispatch_stays_within_limit_and_marks_fresh_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": c} for c in ("a", "b", "c", "d")]})
            output = root / "batch"
            lock = threading.Lock()
            counts = {"active": 0, "peak": 0}

            class FakeChild:
                def __init__(self, command, **kwargs):
                    self.case = command[command.index("--case") + 1]
                    self.out = Path(command[command.index("--out") + 1])
                def wait(self, timeout):
                    with lock:
                        counts["active"] += 1
                        counts["peak"] = max(counts["peak"], counts["active"])
                    try:
                        time.sleep(0.05)
                        save(self.out / "status.json", {"case_id": self.case, "state": "validated"})
                        save(self.out / "result.json", {"case": {"id": self.case}})
                    finally:
                        with lock:
                            counts["active"] -= 1
                    return 0

            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(output), "--jobs", "2"]), patch.object(run_batch.subprocess, "Popen", side_effect=FakeChild):
                self.assertEqual(run_batch.main(), 0)
            self.assertEqual(counts["peak"], 2)
            state = run_case.read_json(output / "batch_state.json")
            self.assertEqual(state["states"], {"validated": 4})
            self.assertTrue(all(not r["preserved_existing"] for r in state["results"].values()))

    def test_new_invalid_stops_remaining_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": c} for c in ("a", "b", "c", "d")]})
            output = root / "batch"
            launched = []

            class FakeChild:
                def __init__(self, command, **kwargs):
                    self.case = command[command.index("--case") + 1]
                    self.out = Path(command[command.index("--out") + 1])
                    launched.append(self.case)
                def wait(self, timeout):
                    time.sleep(0.01 if self.case == "a" else 0.1)
                    state = "invalid" if self.case == "a" else "validated"
                    save(self.out / "status.json", {"case_id": self.case, "state": state})
                    if state == "validated":
                        save(self.out / "result.json", {"case": {"id": self.case}})
                    return 1 if state == "invalid" else 0

            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(output), "--jobs", "2", "--stop-on-invalid"]), patch.object(run_batch.subprocess, "Popen", side_effect=FakeChild):
                self.assertEqual(run_batch.main(), 1)
            self.assertIn("a", launched)
            self.assertTrue(set(launched) <= {"a", "b"})
            state = run_case.read_json(output / "batch_state.json")
            self.assertEqual(state["results"]["c"]["state"], "not_dispatched")
            self.assertEqual(state["results"]["d"]["state"], "not_dispatched")

    def test_resume_preserves_valid_and_invalid_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            manifest = work / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}, {"case_id": "b"}]})
            output = work / "batch"
            identity = run_batch.batch_identity(manifest, ["a", "b"], 3600)
            save(output / "batch_identity.json", identity)
            for case, state in [("a", "validated"), ("b", "invalid")]:
                save(output / "runs" / case / "attempt_01/status.json", {"state": state})
                if state == "validated":
                    save(output / "runs" / case / "attempt_01/result.json", {"case": {"id": case}})
            argv = ["run_batch.py", "--manifest", str(manifest), "--out", str(output), "--resume", "--jobs", "2"]
            with patch.object(sys, "argv", argv), patch.object(run_batch.subprocess, "Popen") as spawn:
                self.assertEqual(run_batch.main(), 1)
                spawn.assert_not_called()
            self.assertFalse((output / "batch.lock").exists())
            summary = run_case.read_json(output / "batch_state.json")
            self.assertEqual(summary["states"], {"validated": 1, "invalid": 1})
            self.assertTrue(summary["results"]["b"]["preserved_existing"])

    def test_resume_does_not_overwrite_interrupted_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            manifest = work / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}]})
            output = work / "batch"
            save(output / "batch_identity.json", run_batch.batch_identity(manifest, ["a"], 3600))
            (output / "runs/a/attempt_01").mkdir(parents=True)
            argv = ["run_batch.py", "--manifest", str(manifest), "--out", str(output), "--resume"]
            with patch.object(sys, "argv", argv), patch.object(run_batch.subprocess, "Popen") as spawn:
                self.assertEqual(run_batch.main(), 1)
                spawn.assert_not_called()
            summary = run_case.read_json(output / "batch_state.json")
            self.assertEqual(summary["states"], {"interrupted": 1})

    def test_pause_prevents_first_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}]})
            out = root / "batch"
            save(out / "batch_identity.json", run_batch.batch_identity(manifest, ["a"], 3600))
            (out / "PAUSE").touch()
            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(out), "--resume"]), patch.object(run_batch.subprocess, "Popen") as spawn:
                self.assertEqual(run_batch.main(), 1)
                spawn.assert_not_called()
            self.assertEqual(run_case.read_json(out / "batch_state.json")["states"], {"not_dispatched": 1})

    def test_known_invalid_stops_resume_before_new_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}, {"case_id": "b"}]})
            out = root / "batch"
            save(out / "batch_identity.json", run_batch.batch_identity(manifest, ["a", "b"], 3600))
            save(out / "runs/a/attempt_01/status.json", {"case_id": "a", "state": "invalid"})
            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(out), "--resume", "--stop-on-invalid"]), patch.object(run_batch.subprocess, "Popen") as spawn:
                self.assertEqual(run_batch.main(), 1)
                spawn.assert_not_called()
            self.assertEqual(run_case.read_json(out / "batch_state.json")["states"], {"invalid": 1, "not_dispatched": 1})

    def test_validated_without_result_is_not_counted_as_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            save(manifest, {"cases": [{"case_id": "a"}]})
            out = root / "batch"
            save(out / "batch_identity.json", run_batch.batch_identity(manifest, ["a"], 3600))
            save(out / "runs/a/attempt_01/status.json", {"case_id": "a", "state": "validated"})
            with patch.object(sys, "argv", ["batch", "--manifest", str(manifest), "--out", str(out), "--resume"]), patch.object(run_batch.subprocess, "Popen") as spawn:
                self.assertEqual(run_batch.main(), 1)
                spawn.assert_not_called()
            self.assertEqual(run_case.read_json(out / "batch_state.json")["states"], {"runtime_failed": 1})


class PowerShellHelperTests(unittest.TestCase):
    def test_submit_and_reject_duplicate(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell not installed")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            shutil.copyfile(ROOT / "stepwise/engine/io.ps1", work / "io.ps1")
            save(work / "input_index.json", {"available_steps": [1], "sources": [{"source_id": "S0001"}]})
            (work / "step_judgments").mkdir()
            payload = json.dumps({"step": 1, "verdict": "uncertain", "summary": "Synthetic observation",
                "evidence": [{"source_id": "S0001", "phase": "decision", "locator": "response", "observation": "Synthetic text"}]})
            command = [shell, "-NoProfile", "-File", str(work / "io.ps1"), "-Mode", "step", "-Json", payload]
            first = subprocess.run(command, capture_output=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr.decode(errors="replace"))
            second = subprocess.run(command, capture_output=True, timeout=20)
            self.assertNotEqual(second.returncode, 0)
            self.assertTrue((work / "step_judgments/step_001.json").is_file())


class NormalizationAuditTests(unittest.TestCase):
    def test_change_log_distinguishes_bool_from_integer(self):
        self.assertTrue(changes({"x": 0}, {"x": False}))

    def test_normalization_preserves_original_prediction_and_records_edits(self):
        before = {"evidence": [{"id": "E1"}], "goals": [{"evidence_ids": ["E1", "missing"]}],
                  "outcome": {"decision": "success"}, "recovery_opportunity": {"exists": "yes", "attempted": "yes", "outcome": "succeeded"}}
        class FakeProtocol:
            def compile(self, prediction): return copy.deepcopy(prediction)
            def project_legacy(self, result): return copy.deepcopy(result)
            def wire_schema(self): return {"type": "object"}
        class FakeValidator:
            def validate_result(self, *args, **kwargs): return []
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            with patch.object(ENGINE, "validator", return_value=FakeValidator()):
                status = ENGINE.finish(out, {"inventory": []}, Path(root), FakeProtocol(), json.dumps(before), {}, {})
            original = run_case.read_json(out / "first_prediction.json")
            audit = run_case.read_json(out / "normalization_audit.json")
            self.assertEqual(original, before)
            self.assertTrue(status["normalization_changed"])
            self.assertIn("$.goals[0].evidence_ids", {entry["path"] for entry in audit["deterministic_edits"]})
            self.assertIn("$.recovery_opportunity.exists", {entry["path"] for entry in audit["deterministic_edits"]})


if __name__ == "__main__":
    unittest.main()
