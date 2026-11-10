"""Exercise revision checks through the actual PowerShell submission helper."""
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

PWSH = shutil.which("pwsh")


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@unittest.skipUnless(PWSH, "PowerShell is required for submission helper integration tests")
class RevisionPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        shutil.copyfile(ROOT / "stepwise/engine/io.ps1", self.workspace / "io.ps1")
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
        save(self.workspace / "global_summary.schema.json", {"required": ["decision"]})
        save(self.workspace / "global_summary.json", {"decision": "pending"})
        self.receipt = {"events": [
            {"source": entry["source"], "success": True, "content_complete": True, "truncated": False}
            for entry in self.case["inventory"]
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(self.workspace / "io.ps1"),
             "-Mode", mode, "-Json", '{"decision":"pending"}'],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def assert_rejected(self, revised: dict, message: str) -> None:
        save(self.revision, revised)
        original_bytes = self.base.read_bytes()
        global_bytes = (self.workspace / "global_summary.json").read_bytes()
        for mode in ("global", "check"):
            with self.subTest(mode=mode):
                result = self.invoke(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertEqual((self.workspace / "global_summary.json").read_bytes(), global_bytes)
        self.assertEqual(self.base.read_bytes(), original_bytes)
        events = [json.loads(line) for line in (self.workspace / "io_events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["accepted"] is False for event in events))
        with self.assertRaises(Exception):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_without_revision_still_passes(self):
        for mode in ("global", "check"):
            result = self.invoke(mode)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(protocol.assemble(self.workspace, self.case, self.receipt)["evidence"]), 2)

    def test_addition_preserves_ids_and_allows_json_key_reordering(self):
        revised = copy.deepcopy(self.original)
        revised["summary"] = "Rechecked observation"
        revised["evidence"][0] = dict(reversed(list(revised["evidence"][0].items())))
        revised["evidence"].append({**revised["evidence"][1], "observation": "A new direct observation"})
        save(self.revision, revised)
        for mode in ("global", "check"):
            result = self.invoke(mode)
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

    def test_reordered_original_evidence_is_rejected(self):
        revised = copy.deepcopy(self.original)
        revised["evidence"].reverse()
        self.assert_rejected(revised, "Original evidence E01_1 differs")

    def test_verdict_change_is_rejected(self):
        self.assert_rejected({**self.original, "verdict": "error"}, "cannot change the original verdict")

    def test_revision_step_mismatch_is_rejected(self):
        self.assert_rejected({**self.original, "step": 2}, "Revision step mismatch")

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


if __name__ == "__main__":
    unittest.main()
