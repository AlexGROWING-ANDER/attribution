"""Offline state, integrity and delivery checks for native Flux execution."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stepwise"))
import run_case
import flux_execution as flux
import test_stepwise as fixtures


class FluxExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.loaded = self.fixture.load()
        self.out = self.fixture.path.parent / "flux"
        self.binding = {
            "manifest_path": str(self.fixture.path),
            "manifest_sha256": run_case.sha256(self.fixture.path),
            "runtime_manifest_sha256": run_case.sha256(ROOT / "RELEASE_MANIFEST.json"),
            "skill_manifest_sha256": run_case.sha256(ROOT / "stepwise/skills/v3/manifest.json"),
        }
        flux.prepare_case(self.loaded, self.out, self.binding)

    def checkpoint(self):
        helper = flux.load_helper(self.out / "workspace")
        index = flux.read_json(self.out / "workspace/input_index.json")
        helper.submit_checkpoint(index, {
            "schema": "case-checkpoint-plan-v1", "case_id": "synthetic",
            "basis": ["instruction"], "checkpoints": [{
                "id": "CP1", "type": "goal", "object": "fixture",
                "requirement": "Inspect synthetic fixture", "required": True,
                "depends_on": [], "acceptance": "Visible fixture observed",
                "evidence_expectation": ["screenshot"],
            }],
        })

    def test_staging_does_not_run_inference_or_count_delivery(self):
        session, loaded = flux.validate_session(self.out)
        receipt = flux.delivery_receipt(self.out, session, loaded)
        self.assertEqual(receipt["events"], [])
        self.assertEqual(receipt["producer"], "harness")
        self.assertEqual(receipt["confirmation_producer"], "flux_agent")
        self.assertEqual(flux.read_json(self.out / "status.json")["state"], "ready_for_flux")
        self.assertNotIn("model_provider", flux.read_json(self.out / "input_binding.json"))
        self.assertFalse((self.out / "pid.json").exists())

    def test_read_is_gated_by_checkpoint(self):
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            flux.offer_delivery(self.out, "logs/page_001.txt")

    def test_offer_is_not_a_delivery_confirmation(self):
        self.checkpoint()
        offer = flux.offer_delivery(self.out, "logs/page_001.txt")
        self.assertIn("Synthetic module output", offer["text"])
        session, loaded = flux.validate_session(self.out)
        self.assertEqual(flux.delivery_receipt(self.out, session, loaded)["events"], [])

    def test_confirmation_requires_the_correct_native_tool(self):
        self.checkpoint()
        offer = flux.offer_delivery(self.out, "logs/page_001.txt")
        with self.assertRaisesRegex(ValueError, "confirmation differs"):
            flux.acknowledge_delivery(self.out, offer["ticket"], "view_image", "unit-test")
        flux.acknowledge_delivery(self.out, offer["ticket"], "exec_command", "unit-test-text-completion")
        session, loaded = flux.validate_session(self.out)
        event = flux.delivery_receipt(self.out, session, loaded)["events"][0]
        self.assertTrue(event["content_complete"])
        self.assertEqual(event["operation"], "read_text")

    def test_image_path_offer_does_not_count_as_viewed(self):
        self.checkpoint()
        source = next(e["source_id"] for e in self.loaded[0]["inventory"] if e["kind"] == "image")
        offer = flux.offer_delivery(self.out, source)
        self.assertEqual(Path(offer["path"]).read_bytes(), Path(self.loaded[0]["images"][0]["delivery_path"]).read_bytes())
        session, loaded = flux.validate_session(self.out)
        self.assertEqual(flux.delivery_receipt(self.out, session, loaded)["events"], [])
        with self.assertRaisesRegex(ValueError, "confirmation differs"):
            flux.acknowledge_delivery(self.out, offer["ticket"], "exec_command", "unit-test")

    def test_duplicate_confirmation_is_idempotent_and_bound(self):
        self.checkpoint()
        offer = flux.offer_delivery(self.out, "logs/page_001.txt")
        flux.acknowledge_delivery(self.out, offer["ticket"], "exec_command", "unit-test")
        self.assertTrue(flux.acknowledge_delivery(self.out, offer["ticket"], "exec_command", "unit-test")["preserved_existing"])
        with self.assertRaisesRegex(ValueError, "different completion"):
            flux.acknowledge_delivery(self.out, offer["ticket"], "exec_command", "other")

    def test_unregistered_resource_and_ticket_traversal_are_rejected(self):
        self.checkpoint()
        with self.assertRaisesRegex(ValueError, "registered"):
            flux.offer_delivery(self.out, "../../outside")
        with self.assertRaisesRegex(ValueError, "Invalid delivery ticket"):
            flux.acknowledge_delivery(self.out, "../../outside", "exec_command", "unit-test")

    def test_staged_input_tampering_is_rejected(self):
        (self.out / "workspace/logs/page_001.txt").write_text("altered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen Flux input changed"):
            flux.validate_session(self.out)

    def test_finalize_does_not_validate_an_unfinished_task(self):
        with patch.object(flux.engine, "finish", side_effect=AssertionError("must not validate")):
            self.assertEqual(flux.finalize_case(self.out)["state"], "ready_for_flux")
        self.assertFalse((self.out / "result.json").exists())

    def test_new_entry_never_resolves_a_model_or_launches_a_model_process(self):
        args = ["run_case.py", "--manifest", str(self.fixture.path), "--case", "synthetic",
                "--out", str(self.fixture.path.parent / "second")]
        with patch.object(sys, "argv", args), patch("subprocess.Popen", side_effect=AssertionError("model process")):
            self.assertEqual(run_case.main(), 0)

    def test_flux_receipt_passes_the_unchanged_access_validator(self):
        self.checkpoint()
        session, loaded = flux.validate_session(self.out)
        for resource, item in session["resources"].items():
            offered = flux.offer_delivery(self.out, resource)
            tool = "view_image" if item["kind"] == "image" else "exec_command"
            flux.acknowledge_delivery(self.out, offered["ticket"], tool, "unit-test-completion")
        receipt = flux.delivery_receipt(self.out, session, loaded)
        path = self.out / "access_receipt.json"
        flux.atomic_json(path, receipt)
        images = [e["source"] for e in loaded[0]["inventory"] if e["kind"] == "image"]
        result = {"case": loaded[0]["identity"], "evidence": [], "coverage": {
            "screenshots_viewed": images, "screenshot_sources_available": images,
            "log_steps_read": [1], "available_steps": [1], "scan_complete": True,
        }}
        validator = flux.engine.validator(loaded[2])
        self.assertEqual(validator.validate_access_receipt(result, path, loaded[0]["evidence_root"]), [])
        receipt["producer"] = "unsupported-producer"
        flux.atomic_json(path, receipt)
        self.assertIn("access receipt producer/attempt mismatch",
                      validator.validate_access_receipt(result, path, loaded[0]["evidence_root"]))


if __name__ == "__main__":
    unittest.main()
