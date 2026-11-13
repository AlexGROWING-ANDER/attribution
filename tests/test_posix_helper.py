"""Exercise the cross-platform Python submission helper used by macOS/Linux."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stepwise/engine"))
import native_engine as engine
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

    def test_known_goal_rejects_module_output_only(self):
        default = {"decision": "pending", "goals": []}
        for status in ("satisfied", "unsatisfied"):
            with self.subTest(status=status):
                save(self.workspace / "global_summary.json", default)
                invalid = {"decision": "pending", "goals": [
                    {"id": "G1", "status": status, "evidence_ids": ["E01_1"]},
                ]}
                result = self.invoke("global", invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("known status needs screenshot evidence", result.stderr)
                self.assertIn("use unknown", result.stderr)
                self.assertEqual(json.loads((self.workspace / "global_summary.json").read_text()), default)

                save(self.workspace / "global_summary.json", invalid)
                result = self.invoke("check")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("known status needs screenshot evidence", result.stderr)
                self.assertEqual(json.loads((self.workspace / "global_summary.json").read_text()), invalid)

    def test_known_goal_accepts_screenshot_evidence(self):
        payload = {"decision": "pending", "goals": [
            {"id": "G1", "status": "unsatisfied", "evidence_ids": ["E01_1", "E01_2"]},
        ]}
        for mode, value in (("global", payload), ("check", None)):
            result = self.invoke(mode, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_goal_accepts_module_output_only(self):
        payload = {"decision": "pending", "goals": [
            {"id": "G1", "status": "unknown", "evidence_ids": ["E01_1"]},
        ]}
        for mode, value in (("global", payload), ("check", None)):
            result = self.invoke(mode, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_revision_screenshot_can_support_known_goal(self):
        log_only = {**self.original, "evidence": [self.original["evidence"][0]]}
        save(self.base, log_only)
        save(self.revision, {**log_only, "evidence": self.original["evidence"]})
        payload = {"decision": "pending", "goals": [
            {"id": "G1", "status": "satisfied", "evidence_ids": ["E01_2"]},
        ]}
        for mode, value in (("global", payload), ("check", None)):
            result = self.invoke(mode, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PythonDerivedViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        shutil.copyfile(ROOT / "stepwise/engine/io.py", self.workspace / "io.py")
        parent = self.workspace / "images/frame.png"
        parent.parent.mkdir(parents=True)
        image = Image.new("RGB", (120, 80), (10, 20, 30))
        image.paste((200, 100, 50), (20, 10, 90, 60))
        image.save(parent)
        log = self.workspace / "step_1/manager.json"
        save(log, {"response": "Synthetic module output"})
        self.sources = [
            {"source_id": "S0001", "source": "step_1/manager.json", "kind": "log", "step": 1,
             "sha256": hashlib.sha256(log.read_bytes()).hexdigest()},
            {"source_id": "S0002", "source": "images/frame.png", "kind": "image", "step": None,
             "sha256": hashlib.sha256(parent.read_bytes()).hexdigest()},
        ]
        save(self.workspace / "input_index.json", {"available_steps": [1], "sources": self.sources})
        self.by_id = {entry["source_id"]: entry for entry in self.sources}
        self.by_id["S0002"] = {**self.by_id["S0002"], "delivery_sha256": self.by_id["S0002"]["sha256"]}

    def tearDown(self):
        self.temp.cleanup()

    def derive(self, source_id="S0002", box="20,10,90,60", scale="2", output="derived_views/detail.png"):
        return subprocess.run(
            [sys.executable, str(self.workspace / "io.py"), "derive", "--source-id", source_id,
             "--box", box, "--scale", scale, "--out", output],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )

    def original_entry(self):
        parent = self.workspace / "images/frame.png"
        return {**self.by_id["S0002"], "delivery_path": str(parent)}

    def test_valid_derive_is_registered_and_deterministically_validated(self):
        index_before = (self.workspace / "input_index.json").read_bytes()
        result = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.workspace / "derived_views/detail.png"
        metadata_path = self.workspace / "derived_views/detail.png.json"
        self.assertTrue(output.is_file())
        self.assertTrue(metadata_path.is_file())
        self.assertEqual((self.workspace / "input_index.json").read_bytes(), index_before)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["source_id"], "S0002")
        self.assertEqual(metadata["box"], [20, 10, 90, 60])
        self.assertEqual(metadata["resampling"], "lanczos")
        self.assertEqual(metadata["output_dimensions"], [140, 100])
        self.assertEqual(metadata["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        with Image.open(output) as image:
            self.assertEqual(image.size, (140, 100))
        loaded, raw = engine.load_derived_view(self.workspace, output, self.by_id)
        self.assertEqual(loaded["derived_view_id"], "derived_views/detail.png")
        self.assertEqual(raw, output.read_bytes())
        kind, payload, reason = engine.resolve_native_image(output, self.workspace, {}, self.by_id)
        self.assertEqual((kind, reason), ("derived", None))
        self.assertEqual(payload[0]["source_id"], "S0002")

    def test_non_image_parent_is_rejected(self):
        result = self.derive(source_id="S0001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registered gallery image", result.stderr)

    def test_crop_only_view_is_accepted_without_resampling(self):
        result = self.derive(scale="1", output="derived_views/crop.png")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.workspace / "derived_views/crop.png"
        metadata = json.loads(output.with_name(output.name + ".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["resampling"], "none")
        self.assertEqual(metadata["output_dimensions"], [70, 50])
        engine.load_derived_view(self.workspace, output, self.by_id)

    def test_out_of_bounds_crop_is_rejected(self):
        result = self.derive(box="20,10,121,60")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--box must satisfy", result.stderr)
        self.assertFalse((self.workspace / "derived_views/detail.png").exists())

    def test_output_path_escape_is_rejected(self):
        for output in ("../escape.png", str(self.workspace / "absolute.png")):
            with self.subTest(output=output):
                result = self.derive(output=output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--out", result.stderr)

    def test_tampered_derived_bytes_are_rejected(self):
        result = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.workspace / "derived_views/detail.png"
        with Image.open(output) as image:
            image.putpixel((0, 0), (255, 255, 255))
            tampered = image.copy()
        tampered.save(output)
        with self.assertRaises(engine.DerivedViewError):
            engine.load_derived_view(self.workspace, output, self.by_id)

    def test_changed_parent_delivery_is_rejected(self):
        result = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parent = self.workspace / "images/frame.png"
        with Image.open(parent) as image:
            image.putpixel((0, 0), (255, 255, 255))
            changed = image.copy()
        changed.save(parent)
        with self.assertRaises(engine.DerivedViewError):
            engine.load_derived_view(self.workspace, self.workspace / "derived_views/detail.png", self.by_id)

    def test_unregistered_derived_view_returns_recoverable_rejection(self):
        output = self.workspace / "derived_views/forged.png"
        output.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), (1, 2, 3)).save(output)
        kind, payload, reason = engine.resolve_native_image(output, self.workspace, {}, self.by_id)
        self.assertEqual(kind, "rejected")
        self.assertIsNone(payload)
        self.assertIn("metadata", reason)
        message = engine.image_rejection_message(reason)
        self.assertIn("io.py derive", message)

    def test_malformed_registration_returns_recoverable_rejection(self):
        result = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata_path = self.workspace / "derived_views/detail.png.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["source_id"] = []
        save(metadata_path, metadata)
        kind, payload, reason = engine.resolve_native_image(
            self.workspace / "derived_views/detail.png", self.workspace, {}, self.by_id,
        )
        self.assertEqual((kind, payload), ("rejected", None))
        self.assertIn("source ID is invalid", reason)

    def test_rejection_message_uses_runtime_interpreter(self):
        message = engine.image_rejection_message("detail")
        self.assertIn(str(Path(sys.executable).resolve()), message)
        self.assertIn("io.py derive", message)

    def test_malformed_view_image_calls_are_rejected_without_raising(self):
        output = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        for call in ({}, {"arguments": "not json"}, {"arguments": json.dumps({})},
                     {"arguments": json.dumps({"path": 3})}):
            with self.subTest(call=call):
                kind, payload, reason, path, images = engine.classify_posix_view_image(
                    call, output, self.workspace, {}, self.by_id,
                )
                self.assertEqual((kind, payload, path, images), ("rejected", None, None, []))
                self.assertIn("view_image", reason)

    def test_malformed_view_image_outputs_are_rejected_without_raising(self):
        call = {"arguments": json.dumps({"path": "derived_views/missing.png"})}
        for output in ([1], [{"type": "input_image"}],
                       [{"type": "input_image", "image_url": None}]):
            with self.subTest(output=output):
                kind, payload, reason, _, _ = engine.classify_posix_view_image(
                    call, output, self.workspace, {}, self.by_id,
                )
                self.assertEqual((kind, payload), ("rejected", None))
                self.assertIn("view_image", reason)

    def test_unregistered_view_image_is_recoverable_through_posix_classifier(self):
        output = self.workspace / "derived_views/forged.png"
        output.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), (1, 2, 3)).save(output)
        call = {"arguments": json.dumps({"path": "derived_views/forged.png"})}
        kind, payload, reason, path, images = engine.classify_posix_view_image(
            call, [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
            self.workspace, {}, self.by_id,
        )
        self.assertEqual((kind, payload), ("rejected", None))
        self.assertEqual(path, output)
        self.assertEqual(len(images), 1)
        self.assertIn("metadata", reason)

    def test_changed_original_delivery_bytes_are_rejected(self):
        parent = self.workspace / "images/frame.png"
        entry = {"delivery_path": str(parent), "delivery_sha256": hashlib.sha256(parent.read_bytes()).hexdigest()}
        self.assertEqual(engine.verified_delivery_bytes(entry), parent.read_bytes())
        with Image.open(parent) as image:
            image.putpixel((0, 0), (255, 255, 255))
            changed = image.copy()
        changed.save(parent)
        with self.assertRaises(ValueError):
            engine.verified_delivery_bytes(entry)

    def test_bridge_rejection_does_not_displace_original_delivery(self):
        forged = self.workspace / "derived_views/forged.png"
        forged.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), (1, 2, 3)).save(forged)
        original = self.original_entry()
        image_paths = {str(Path(original["delivery_path"]).resolve()).casefold(): original}
        image_output = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        body = {"input": [
            {"type": "function_call", "call_id": "bad", "name": "view_image",
             "arguments": json.dumps({"path": "derived_views/forged.png"})},
            {"type": "function_call_output", "call_id": "bad", "output": copy.deepcopy(image_output)},
            {"type": "function_call", "call_id": "good", "name": "view_image",
             "arguments": json.dumps({"path": "images/frame.png"})},
            {"type": "function_call_output", "call_id": "good", "output": copy.deepcopy(image_output)},
        ]}
        restores, newimgs, newpages, derived, rejected = engine.restore_and_audit(
            body, self.workspace, [], image_paths, self.by_id,
            engine.platform_runtime.MacOSRuntime(), set(),
        )
        self.assertEqual([entry["source"] for entry, _ in newimgs], ["images/frame.png"])
        self.assertEqual(len(restores), 1)
        self.assertEqual((newpages, derived), ([], []))
        self.assertEqual(len(rejected), 1)
        self.assertIn("Image rejected", body["input"][1]["output"])
        self.assertTrue(body["input"][3]["output"][0]["image_url"].startswith("data:image/png;base64,"))

    def test_bridge_malformed_view_image_is_recoverable(self):
        body = {"input": [
            {"type": "function_call", "call_id": "bad", "name": "view_image", "arguments": "not json"},
            {"type": "function_call_output", "call_id": "bad", "output": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]},
        ]}
        restores, newimgs, newpages, derived, rejected = engine.restore_and_audit(
            body, self.workspace, [], {}, self.by_id,
            engine.platform_runtime.MacOSRuntime(), set(),
        )
        self.assertEqual((restores, newimgs, newpages, derived), ([], [], [], []))
        self.assertEqual(len(rejected), 1)
        self.assertIn("Image rejected", body["input"][1]["output"])

    def test_bridge_delivers_registered_derived_view_without_original_credit(self):
        result = self.derive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = {"input": [
            {"type": "function_call", "call_id": "detail", "name": "view_image",
             "arguments": json.dumps({"path": "derived_views/detail.png"})},
            {"type": "function_call_output", "call_id": "detail", "output": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]},
        ]}
        restores, newimgs, newpages, derived, rejected = engine.restore_and_audit(
            body, self.workspace, [], {}, self.by_id,
            engine.platform_runtime.MacOSRuntime(), set(),
        )
        self.assertEqual((restores, newimgs, newpages, rejected), ([], [], [], []))
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["source_id"], "S0002")
        self.assertTrue(body["input"][1]["output"][0]["image_url"].startswith("data:image/png;base64,"))

    def test_windows_bridge_contract_is_unchanged(self):
        runtime = engine.platform_runtime.WindowsRuntime()
        original = self.original_entry()
        image_paths = {str(Path(original["delivery_path"]).resolve()).casefold(): original}
        image_output = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        good = {"input": [
            {"type": "function_call", "call_id": "good", "name": "view_image",
             "arguments": json.dumps({"path": "images/frame.png"})},
            {"type": "function_call_output", "call_id": "good", "output": copy.deepcopy(image_output)},
        ]}
        restores, newimgs, _, _, rejected = engine.restore_and_audit(
            good, self.workspace, [], image_paths, self.by_id, runtime, set(),
        )
        self.assertEqual([entry["source"] for entry, _ in newimgs], ["images/frame.png"])
        self.assertEqual((len(restores), rejected), (1, []))
        bad = {"input": [
            {"type": "function_call", "call_id": "bad", "name": "view_image",
             "arguments": json.dumps({"path": "derived_views/forged.png"})},
            {"type": "function_call_output", "call_id": "bad", "output": copy.deepcopy(image_output)},
        ]}
        with self.assertRaises(ValueError):
            engine.restore_and_audit(bad, self.workspace, [], image_paths, self.by_id, runtime, set())


if __name__ == "__main__":
    unittest.main()
