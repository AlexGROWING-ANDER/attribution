"""Exercise the cross-platform Python submission helper used by macOS/Linux."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stepwise/engine"))
import stepwise_support_v2 as protocol


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class PythonRevisionPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        shutil.copyfile(ROOT / "stepwise/engine/io.py", self.workspace / "io.py")
        self.case = {"available_steps": [1], "inventory": [
            {"source_id": "S0001", "source": "step_1/manager.json", "kind": "log", "step": 1},
            {"source_id": "S0002", "source": "images/frame.png", "kind": "image", "step": None},
        ]}
        self.original = {"step": 1, "verdict": "uncertain", "summary": "Initial observation", "evidence": [
            {"source_id": "S0001", "phase": "decision", "locator": "response", "observation": "A log entry"},
            {"source_id": "S0002", "phase": "after_action", "locator": "full image", "observation": "页面含‘收藏’按钮"},
        ]}
        self.base = self.workspace / "step_judgments/step_001.json"
        self.revision = self.workspace / "step_judgments/revision_001.json"
        save(self.base, self.original)
        save(self.workspace / "input_index.json", {"available_steps": [1], "sources": self.case["inventory"]})
        save(self.workspace / "global_summary.schema.json", {"required": ["decision", "goals"]})
        save(self.workspace / "global_summary.json", {"decision": "pending", "goals": []})
        self.receipt = {"events": [
            {"source": entry["source"], "success": True, "content_complete": True, "truncated": False}
            for entry in self.case["inventory"]
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, mode: str, payload: dict | None = None) -> subprocess.CompletedProcess:
        command = [sys.executable, str(self.workspace / "io.py"), mode]
        if payload is not None:
            command += ["--json", json.dumps(payload, ensure_ascii=False)]
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def assert_rejected(self, revised: dict, message: str) -> None:
        save(self.revision, revised)
        original_bytes = self.base.read_bytes()
        global_bytes = (self.workspace / "global_summary.json").read_bytes()
        for mode in ("global", "check"):
            with self.subTest(mode=mode):
                payload = {"decision": "pending", "goals": []} if mode == "global" else None
                result = self.invoke(mode, payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertEqual((self.workspace / "global_summary.json").read_bytes(), global_bytes)
        self.assertEqual(self.base.read_bytes(), original_bytes)
        events = [json.loads(line) for line in (self.workspace / "io_events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["accepted"] is False for event in events))
        with self.assertRaises(Exception):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_step_submit_and_reject_duplicate(self):
        self.base.unlink()
        result = self.invoke("step", self.original)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.base.is_file())
        second = self.invoke("step", self.original)
        self.assertNotEqual(second.returncode, 0)

    def test_step_order_is_enforced(self):
        save(self.workspace / "input_index.json", {"available_steps": [1, 2], "sources": self.case["inventory"]})
        self.base.unlink()
        result = self.invoke("step", {**self.original, "step": 2})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Current required step is 1", result.stderr)

    def test_without_revision_global_and_check_pass(self):
        for mode, payload in (("global", {"decision": "pending", "goals": []}), ("check", None)):
            result = self.invoke(mode, payload)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(protocol.assemble(self.workspace, self.case, self.receipt)["evidence"]), 2)

    def test_addition_preserves_ids_and_allows_json_key_reordering(self):
        revised = copy.deepcopy(self.original)
        revised["summary"] = "Rechecked observation"
        revised["evidence"][0] = dict(reversed(list(revised["evidence"][0].items())))
        revised["evidence"].append({**revised["evidence"][1], "observation": "A new direct observation"})
        save(self.revision, revised)
        for mode, payload in (("global", {"decision": "pending", "goals": []}), ("check", None)):
            result = self.invoke(mode, payload)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual([item["id"] for item in result["evidence"]], ["E01_1", "E01_2", "E01_3"])

    def test_modified_evidence_is_rejected_with_precise_feedback(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"][1]["observation"] = "Reinterpreted prior observation"
        revised["evidence"].append(copy.deepcopy(self.original["evidence"][1]))
        self.assert_rejected(revised, "Original evidence E01_2 differs at observation")

    def test_case_only_text_change_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"][0]["observation"] = "A LOG ENTRY"
        self.assert_rejected(revised, "Original evidence E01_1 differs")

    def test_removed_original_evidence_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].pop()
        self.assert_rejected(revised, "removed original evidence")

    def test_verdict_change_is_rejected(self):
        self.assert_rejected({**self.original, "verdict": "error"}, "cannot change the original verdict")

    def test_string_step_is_rejected(self):
        self.assert_rejected({**self.original, "step": "1"}, "Revision step mismatch")

    def test_unexpected_evidence_field_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].append({**self.original["evidence"][0], "extra": True})
        self.assert_rejected(revised, "Unexpected field extra")

    def test_invalid_appended_phase_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].append({**self.original["evidence"][0], "phase": "invalid"})
        self.assert_rejected(revised, "Invalid revision phase")

    def test_unknown_appended_source_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].append({**self.original["evidence"][0], "source_id": "S9999"})
        self.assert_rejected(revised, "Unknown revision source_id")

    def test_non_string_observation_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].append({**self.original["evidence"][0], "observation": 123})
        self.assert_rejected(revised, "must be a nonempty string")

    def test_unexpected_revision_file_is_rejected(self):
        self.revision = self.workspace / "step_judgments/revision_999.json"
        self.assert_rejected(self.original, "Unexpected revision file")

    def test_unknown_global_evidence_ref_is_rejected(self):
        result = self.invoke("check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        save(self.workspace / "global_summary.json", {"decision": "pending", "goals": [{"evidence_ids": ["E01_9"]}]})
        result = self.invoke("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown evidence refs: E01_9", result.stderr)

    def test_global_rejects_unexpected_top_level_field(self):
        result = self.invoke("global", {"decision": "pending", "goals": [], "extra": True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected field extra", result.stderr)


if __name__ == "__main__":
    unittest.main()
