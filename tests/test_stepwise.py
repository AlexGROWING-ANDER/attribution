"""Offline protocol tests; fixtures are synthetic, never human GT."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
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
import platform_runtime
from jsonschema import Draft202012Validator
from harness.protocol import digest
from PIL import Image
import stepwise_support_v2 as protocol
import stepwise_support_fast as prompt_builder
import framework_architecture
from normalization_audit import changes


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "test_stepwise_validator", ROOT / "stepwise/engine/validate_result.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def load_uipath_builder():
    spec = importlib.util.spec_from_file_location(
        "test_uipath_builder", ROOT / "data/build_uipath_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_architecture_builder():
    spec = importlib.util.spec_from_file_location(
        "test_architecture_builder", ROOT / "data/build_framework_architecture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseTests(unittest.TestCase):
    def test_question_answer_goal_is_not_answer_delivery(self):
        validator = load_validator()
        for requirement in (
            "在豆包对话中实际发送问题“2026年世界杯是谁夺冠”，并等待页面显示回答。",
            "提交问题并查看回答",
            "发送问题后看到页面返回答案",
        ):
            with self.subTest(requirement=requirement):
                self.assertFalse(validator.is_answer_delivery_goal({"object": "对话", "requirement": requirement}))

    def test_explicit_answer_delivery_remains_guarded(self):
        validator = load_validator()
        for requirement in ("将生成的答案发送给用户", "send the final answer to the user"):
            with self.subTest(requirement=requirement):
                self.assertTrue(validator.is_answer_delivery_goal({"object": "回复", "requirement": requirement}))

    def test_export_integrity(self):
        run_case.verify_release()

    def test_frozen_skill_hashes(self):
        base = ROOT / "stepwise/skills/v3"
        manifest = run_case.read_json(base / "manifest.json")
        self.assertEqual(manifest["skill_version"], "0.16.0")
        for relative, expected in manifest["files"].items():
            self.assertEqual(run_case.sha256(base / "gui-trajectory-attribution" / relative), expected)

    def test_bundled_validator_matches_engine_logic(self):
        engine = (ROOT / "stepwise/engine/validate_result.py").read_text(encoding="utf-8")
        bundled = (ROOT / "stepwise/skills/v3/gui-trajectory-attribution/scripts/validate_result.py").read_text(encoding="utf-8")
        engine_logic = engine.split("\ndef main():", 1)[0].replace(
            'REFERENCES = Path(__file__).resolve().parents[1] / "skills/v3/gui-trajectory-attribution/references"',
            'REFERENCES = Path(__file__).resolve().parents[1] / "references"',
        )
        bundled_logic = bundled.split("\ndef main():", 1)[0]
        self.assertEqual(engine_logic, bundled_logic)

    def test_static_framework_knowledge_configuration_is_not_shipped(self):
        self.assertFalse((ROOT / "config/framework-knowledge.json").exists())
        self.assertFalse((ROOT / "data/build_framework_knowledge_snapshot.py").exists())

    def test_framework_profiles_are_architecture_only(self):
        path = ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/framework-profiles.md"
        text = path.read_text(encoding="utf-8")
        for duplicated_definition in (
            "template_id=",
            "schema=hierarchy",
            "L1=",
            "executor/wrong_coordinate",
            "controller/coordinate_mismatch",
            "F6/",
            "function=F9",
            "Function=E2",
            "R5",
        ):
            with self.subTest(definition=duplicated_definition):
                self.assertNotIn(duplicated_definition, text)

    def test_generic_runtime_contains_no_framework_architecture_memory(self):
        paths = [
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/SKILL.md",
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/framework-profiles.md",
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/judgment-rules.md",
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/output-contract.md",
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/label-boundaries.md",
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/scripts/validate_result.py",
            ROOT / "stepwise/prompts/stepwise_en.txt",
            ROOT / "stepwise/run_case.py",
            ROOT / "stepwise/engine/native_engine.py",
            ROOT / "stepwise/engine/framework_architecture.py",
            ROOT / "stepwise/engine/validate_result.py",
        ]
        forbidden = ("Mobile-Agent", "Agent-S2", "UniPath", "UIPath", "OSWorld",
                     "mobile_agent_v3", "agent_s2", "uipath")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path.name, value=value):
                    self.assertNotIn(value, text)

    def test_all_json_schemas_are_well_formed(self):
        for path in [
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/output.schema.json",
            ROOT / "docs/examples/step_record.schema.json",
            ROOT / "docs/examples/global_summary.schema.json",
        ]:
            with self.subTest(path=path.name):
                Draft202012Validator.check_schema(run_case.read_json(path))

    def test_synthesis_is_required_in_canonical_and_global_schemas(self):
        paths = [
            ROOT / "stepwise/skills/v3/gui-trajectory-attribution/references/output.schema.json",
            ROOT / "docs/examples/global_summary.schema.json",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                schema = run_case.read_json(path)
                self.assertIn("synthesis", schema["required"])
                self.assertEqual(
                    schema["properties"]["synthesis"]["required"],
                    ["summary"],
                )

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
        with patch.dict(run_case.os.environ, {"CODEX_BINARY": '["synthetic-codex"]', "ATTRIBUTION_ENFORCE_CODEX_VERSION": "1"}), patch.object(run_case.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="codex-cli 0.166.0-beta.1\n", stderr="")):
            with self.assertRaisesRegex(ValueError, "requires Codex"):
                run_case.cli_command()

    def test_cli_version_check_is_off_by_default(self):
        with patch.dict(run_case.os.environ, {"CODEX_BINARY": '["synthetic-codex"]'}, clear=True), patch.object(run_case.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="codex-cli 9.9.9-beta.1\n", stderr="")):
            command, version = run_case.resolve_cli()
        self.assertEqual(command, ["synthetic-codex"])
        self.assertEqual(version, "9.9.9-beta.1")

    def test_cli_version_can_be_pinned_by_configuration(self):
        environment = {
            "CODEX_BINARY": '["synthetic-codex"]',
            "ATTRIBUTION_ENFORCE_CODEX_VERSION": "1",
            "ATTRIBUTION_CODEX_VERSION": "9.9.9",
        }
        with patch.dict(run_case.os.environ, environment, clear=True), patch.object(run_case.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="codex-cli 9.9.9\n", stderr="")):
            self.assertEqual(run_case.cli_command(), ["synthetic-codex"])

    def test_cli_version_configuration_rejects_invalid_boolean(self):
        with patch.dict(run_case.os.environ, {"ATTRIBUTION_ENFORCE_CODEX_VERSION": "maybe"}, clear=True), self.assertRaisesRegex(ValueError, "must be 0 or 1"):
            run_case.enforce_codex_version()

    def test_cli_symlink_is_resolved_before_sandbox_launch(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            target = base / "target" / "codex"
            target.parent.mkdir()
            target.write_text("binary", encoding="utf-8")
            link = base / "codex-link"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest("symlinks unavailable: " + str(exc))
            with patch.dict(run_case.os.environ, {"CODEX_BINARY": json.dumps([str(link)])}, clear=True), patch.object(
                run_case.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="codex-cli 9.9.9\n", stderr=""),
            ) as run:
                command, version = run_case.resolve_cli()
        self.assertEqual(command, [str(target.resolve())])
        self.assertEqual(version, "9.9.9")
        self.assertEqual(run.call_args.args[0], [str(target.resolve()), "--version"])

    def test_command_read_roots_include_symlink_and_target(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            target_dir = base / "target"
            link_dir = base / "links"
            target_dir.mkdir()
            link_dir.mkdir()
            target = target_dir / "codex"
            target.write_text("binary", encoding="utf-8")
            link = link_dir / "codex"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest("symlinks unavailable: " + str(exc))
            roots = ENGINE.command_read_roots([str(link), "--version"])
        self.assertIn(link_dir, roots)
        self.assertIn(target_dir.resolve(), roots)

    def test_model_source_defaults_to_stepwise_gateway(self):
        environment = {
            "GROUND_URL": "https://gateway.example.test/v1",
            "GROUND_API_KEY": "gateway-key",
        }
        with patch.dict(run_case.os.environ, environment, clear=True):
            resolved = run_case.resolve_model()
        self.assertEqual(resolved["source"], "stepwise")
        self.assertEqual(resolved["endpoint"], "https://gateway.example.test/v1/responses")
        self.assertEqual(resolved["model"], "deepseek-v4.1-flash")

    def test_empty_model_source_uses_default(self):
        with patch.dict(run_case.os.environ, {"ATTRIBUTION_MODEL_SOURCE": ""}, clear=True):
            self.assertEqual(run_case.model_source(), "stepwise")

    def test_model_source_can_read_codex_config(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "config.toml").write_text(
                'model = "host-model"\n'
                'model_provider = "host_provider"\n'
                'model_reasoning_effort = "high"\n'
                '[model_providers.host_provider]\n'
                'name = "Host provider"\n'
                'base_url = "https://host.example.test/v1"\n'
                'wire_api = "responses"\n'
                'env_key = "HOST_PROVIDER_KEY"\n'
                'requires_openai_auth = false\n',
                encoding="utf-8",
            )
            environment = {
                "ATTRIBUTION_MODEL_SOURCE": "codex",
                "ATTRIBUTION_CODEX_HOME": str(home),
                "ATTRIBUTION_CODEX_MODEL": "override-model",
                "HOST_PROVIDER_KEY": "host-key",
            }
            with patch.dict(run_case.os.environ, environment, clear=True):
                resolved = run_case.resolve_model()
        self.assertEqual(resolved["source"], "codex")
        self.assertEqual(resolved["endpoint"], "https://host.example.test/v1/responses")
        self.assertEqual(resolved["model"], "override-model")
        self.assertEqual(resolved["provider"], "host_provider")
        self.assertEqual(resolved["reasoning_effort"], "high")

    def test_codex_model_source_reports_missing_provider_key(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "config.toml").write_text(
                'model = "host-model"\n'
                'model_provider = "host_provider"\n'
                '[model_providers.host_provider]\n'
                'base_url = "https://host.example.test/v1"\n'
                'wire_api = "responses"\n'
                'env_key = "HOST_PROVIDER_KEY"\n',
                encoding="utf-8",
            )
            with patch.dict(run_case.os.environ, {
                "ATTRIBUTION_MODEL_SOURCE": "codex",
                "ATTRIBUTION_CODEX_HOME": str(home),
            }, clear=True), self.assertRaisesRegex(ValueError, "HOST_PROVIDER_KEY"):
                run_case.resolve_model()

    def test_unknown_model_source_is_rejected(self):
        with patch.dict(run_case.os.environ, {"ATTRIBUTION_MODEL_SOURCE": "unknown"}, clear=True), self.assertRaisesRegex(ValueError, "ATTRIBUTION_MODEL_SOURCE"):
            run_case.model_source()

    def test_validator_cli_finds_default_references(self):
        with tempfile.TemporaryDirectory() as root:
            result = Path(root) / "result.json"
            save(result, {})
            completed = subprocess.run([sys.executable, str(ROOT / "stepwise/engine/validate_result.py"), str(result)], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn("INVALID", completed.stdout)


class UIPathManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "qwen_uipath_teacher_share_20261105_final"
        self.evidence = self.package / "01_QWEN_TRAJECTORIES/reports/annotation_evidence"
        self.case_id = "com.synthetic.app__test_1"
        case_dir = self.evidence / "full_130" / self.case_id
        tail_dir = self.evidence / "tail_130" / self.case_id
        case_dir.mkdir(parents=True)
        tail_dir.mkdir(parents=True)
        Image.new("RGB", (1000, 2988), "white").save(case_dir / "contact_sheet_01.png")
        Image.new("RGB", (20, 40), "white").save(tail_dir / "tail_sheet.png")
        (case_dir / "trajectory_digest.md").write_text(
            "# Synthetic / test_1\n\n"
            f"- case_id: `{self.case_id}`\n"
            "- intent: 打开合成页面并确认结果\n"
            "- batch_status: `completed`\n"
            "- screenshot_count: 2\n\n"
            "## Step 1\n- review: 合成画面一\n- thought: 合成判断一\n"
            "- action: `{\"action_type\": \"CLICK\", \"x\": 1, \"y\": 2}`\n- reward: `0`; done: `False`\n\n"
            "## Step 2\n- review: 合成画面二\n- thought: 合成判断二\n"
            "- action: `{\"action_type\": \"FINISH\"}`\n- reward: `1`; done: `True`\n",
            encoding="utf-8",
        )
        (self.root / "02_ATTRIBUTION_RESULTS").mkdir()
        (self.root / "02_ATTRIBUTION_RESULTS/uipath_annotations.json").write_text(
            "not valid json; the builder must never read GT", encoding="utf-8"
        )
        self.manifest = self.root / "uipath_manifest.json"
        self.cache = self.root / "image_cache"
        self.evidence_out = self.root / "evidence_out"
        self.builder = load_uipath_builder()

    def tearDown(self):
        self.temp.cleanup()

    def build(self, case_ids=None):
        return self.builder.build(
            self.package, self.manifest, self.cache, self.evidence_out,
            "synthetic/uipath", case_ids,
        )

    def test_builds_digest_first_manifest_without_gt(self):
        stats = self.build()
        self.assertEqual(stats["cases"], 1)
        self.assertEqual(stats["images"], 3)
        self.assertEqual(stats["steps"], 2)
        case, manifest, *_ = run_case.load_inputs(ENGINE, self.manifest, self.case_id)
        self.assertEqual(case["identity"]["framework"], "uipath")
        self.assertEqual(case["identity"]["template_id"], "uipath")
        self.assertEqual(case["identity"]["task_id"], "com.synthetic.app/test_1")
        self.assertEqual(case["identity"]["instruction"], "打开合成页面并确认结果")
        self.assertEqual(case["identity"]["dataset"], "synthetic/uipath")
        self.assertEqual(case["available_steps"], [1, 2])
        self.assertEqual(case["image_count"], 3)
        self.assertEqual([image["source"] for image in case["images"]], [
            f"{self.case_id}/step_01.png",
            f"{self.case_id}/step_02.png",
            f"{self.case_id}/tail_sheet.png",
        ])
        sources = {entry["source"]: entry for entry in case["inventory"]}
        self.assertEqual(sources[f"{self.case_id}/step_01.png"]["step"], 1)
        self.assertEqual(sources[f"{self.case_id}/step_01.png"]["derivation"]["box"], [0, 0, 200, 498])
        self.assertEqual(sources[f"{self.case_id}/trajectory_digest.md"]["steps"], [1, 2])
        self.assertEqual(sources[f"{self.case_id}/tail_sheet.png"]["phase"], "final_observation")
        self.assertNotIn("initial_reason", json.dumps(manifest, ensure_ascii=False))

    def test_multistep_sources_are_indexed_for_every_step(self):
        self.build()
        case, _, _, _, output_protocol, _ = run_case.load_inputs(
            ENGINE, self.manifest, self.case_id
        )
        workspace = self.root / "workspace"
        workspace.mkdir()
        pages = [{"path": "logs/page_001.txt", "members": [{
            "entry": next(e for e in case["inventory"] if e["kind"] == "log"),
            "text": "synthetic digest page",
        }]}]
        index = protocol.stage_contract(case, output_protocol, pages, workspace)
        by_source = {e["source"]: e["source_id"] for e in case["inventory"]}
        step_one_id = by_source[f"{self.case_id}/step_01.png"]
        step_two_id = by_source[f"{self.case_id}/step_02.png"]
        digest_id = by_source[f"{self.case_id}/trajectory_digest.md"]
        tail_id = by_source[f"{self.case_id}/tail_sheet.png"]
        self.assertEqual(set(index["steps"][0]["source_ids"]), {step_one_id, digest_id})
        self.assertEqual(index["steps"][0]["log_pages"], ["logs/page_001.txt"])
        self.assertEqual(set(index["steps"][1]["source_ids"]), {step_two_id, digest_id, tail_id})
        self.assertEqual(index["other_log_pages"], [])
        self.assertEqual(index["checkpoint_plan"]["path"], "checkpoint_plan.json")
        self.assertEqual(index["checkpoint_plan"]["schema_path"], "checkpoint_plan.schema.json")
        Draft202012Validator.check_schema(run_case.read_json(workspace / "checkpoint_plan.schema.json"))

    def test_screenshot_count_mismatch_is_rejected(self):
        digest = self.evidence / "full_130" / self.case_id / "trajectory_digest.md"
        digest.write_text(
            digest.read_text(encoding="utf-8").replace("screenshot_count: 2", "screenshot_count: 3"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "screenshot_count"):
            self.build()

    def test_requested_case_must_exist(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            self.build(["com.synthetic.app__missing"])


class FrameworkInputSelectionTests(unittest.TestCase):
    def test_architecture_excludes_legacy_knowledge(self):
        class FakeEngine:
            def __init__(self):
                self.knowledge_calls = 0

            def select_framework_binding(self, registry, framework):
                return registry[framework]

            def resolve_framework_architecture(self, binding, base, identity):
                return {"mode": "generated"}

            def resolve_framework_knowledge(self, binding, base, identity):
                self.knowledge_calls += 1
                return {"mode": "legacy"}

        engine = FakeEngine()
        knowledge, architecture = run_case.resolve_framework_inputs(
            engine,
            {
                "framework_architectures": {"mobile_agent_v3": {"schema": "architecture"}},
                "framework_knowledge": {"schema": "legacy"},
            },
            Path("/tmp"),
            {"framework": "mobile_agent_v3"},
        )
        self.assertIsNone(knowledge)
        self.assertEqual(architecture["mode"], "generated")
        self.assertEqual(engine.knowledge_calls, 0)

    def test_legacy_knowledge_is_used_only_without_architecture(self):
        class FakeEngine:
            def __init__(self):
                self.knowledge_calls = 0

            def select_framework_binding(self, registry, framework):
                self.assertions = (registry, framework)
                return None

            def resolve_framework_architecture(self, binding, base, identity):
                return None

            def resolve_framework_knowledge(self, binding, base, identity):
                self.knowledge_calls += 1
                return {"mode": "legacy"}

        engine = FakeEngine()
        knowledge, architecture = run_case.resolve_framework_inputs(
            engine,
            {"framework_knowledge": {"schema": "legacy"}},
            Path("/tmp"),
            {"framework": "mobile_agent_v3"},
        )
        self.assertEqual(knowledge["mode"], "legacy")
        self.assertIsNone(architecture)
        self.assertEqual(engine.knowledge_calls, 1)
        self.assertEqual(engine.assertions, (None, "mobile_agent_v3"))


class FrameworkKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "snapshot"
        source = self.root / "source/pkg/a.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
        self.files = {"pkg/a.py": run_case.sha256(source)}
        snapshot = {
            "schema": "framework-knowledge-snapshot-v1",
            "framework": "mobile_agent_v3",
            "mode": "hybrid",
            "repo_path": "synthetic",
            "repo_revision": "deadbeef",
            "source_dirty": False,
            "tree_sha256": "tree",
            "files": self.files,
            "source_file_count": 1,
        }
        save(self.root / "snapshot.json", snapshot)
        save(self.root / "framework_index.json", {
            "schema": "framework-source-index-v1",
            "framework": "mobile_agent_v3",
            "mode": "hybrid",
            "repo_revision": "deadbeef",
            "tree_sha256": "tree",
        })
        (self.root / "framework_knowledge_contract.md").write_text("synthetic contract\n", encoding="utf-8")
        self.binding = {
            "schema": "framework-knowledge-binding-v1",
            "framework": "mobile_agent_v3",
            "mode": "hybrid",
            "snapshot_root": str(self.root),
            "repo_revision": "deadbeef",
            "tree_sha256": "tree",
            "snapshot_manifest_sha256": run_case.sha256(self.root / "snapshot.json"),
            "index_sha256": run_case.sha256(self.root / "framework_index.json"),
            "contract_sha256": run_case.sha256(self.root / "framework_knowledge_contract.md"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self):
        return ENGINE.resolve_framework_knowledge(self.binding, self.root.parent, {"framework": "mobile_agent_v3"})

    def test_absent_binding_keeps_legacy_behavior(self):
        self.assertIsNone(ENGINE.resolve_framework_knowledge(None, self.root.parent, {"framework": "mobile_agent_v3"}))

    def test_valid_snapshot_is_accepted(self):
        resolved = self.resolve()
        self.assertEqual(resolved["tree_sha256"], "tree")
        self.assertEqual(resolved["source_file_count"], 1)

    def test_resolved_snapshot_installs_into_case_workspace(self):
        workspace = Path(self.temp.name) / "workspace"
        out = Path(self.temp.name) / "out"
        workspace.mkdir()
        out.mkdir()
        installed = ENGINE.install_framework_knowledge(
            {"framework_knowledge": self.resolve()}, workspace, out
        )
        self.assertTrue((workspace / ".framework-knowledge/source/pkg/a.py").is_file())
        self.assertEqual(installed["index_sha256"], self.binding["index_sha256"])
        self.assertEqual(run_case.read_json(out / "framework_knowledge_binding.json")["contract_sha256"], self.binding["contract_sha256"])

    def test_tampered_index_is_rejected(self):
        index = run_case.read_json(self.root / "framework_index.json")
        index["usage"] = {"authority": "tampered"}
        save(self.root / "framework_index.json", index)
        with self.assertRaisesRegex(ValueError, "index changed"):
            self.resolve()

    def test_unregistered_snapshot_file_is_rejected(self):
        (self.root / "unexpected.md").write_text("synthetic\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            self.resolve()

    def test_unhashed_binding_is_rejected(self):
        self.binding.pop("index_sha256")
        with self.assertRaisesRegex(ValueError, "index/contract hashes"):
            self.resolve()

    def test_architecture_installation_suppresses_legacy_knowledge(self):
        with patch.object(
            ENGINE, "install_framework_architecture", return_value={"mode": "generated"}
        ), patch.object(ENGINE, "install_framework_knowledge") as legacy:
            knowledge, architecture = ENGINE.install_framework_inputs(
                {"framework_architecture": {}, "framework_knowledge": {}}, Path("."), Path(".")
            )
        self.assertIsNone(knowledge)
        self.assertEqual(architecture["mode"], "generated")
        legacy.assert_not_called()

    def test_legacy_knowledge_installs_when_architecture_is_absent(self):
        with patch.object(
            ENGINE, "install_framework_architecture", return_value=None
        ), patch.object(
            ENGINE, "install_framework_knowledge", return_value={"mode": "legacy"}
        ) as legacy:
            knowledge, architecture = ENGINE.install_framework_inputs(
                {"framework_knowledge": {}}, Path("."), Path(".")
            )
        self.assertEqual(knowledge["mode"], "legacy")
        self.assertIsNone(architecture)
        legacy.assert_called_once()


class FrameworkArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        source = self.repo / "pkg/core.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            '"""Synthetic architecture fixture."""\n\n'
            "from dataclasses import dataclass, field\n\n"
            "@dataclass\n"
            "class SharedState:\n"
            "    action_history: list = field(default_factory=list)\n"
            "    plan: str = ''\n\n"
            "class Runner:\n"
            '    """Coordinates synthetic execution."""\n\n'
            "    def __init__(self, state: SharedState) -> None:\n"
            "        self.state: SharedState = state\n"
            "        self.retries: int = 2\n"
            '        self.last_action = ""\n'
            "        self.first, self.second = 1, 2\n\n"
            "    def run(self) -> str:\n"
            "        self.retries += 1\n"
            '        self.last_action = "click"\n'
            "        return self.state.plan\n\n"
            "class Planner:\n"
            "    def plan(self, state: SharedState) -> dict:\n"
            "        return {'plan': state.plan}\n\n"
            "class Executor:\n"
            "    def execute(self, state: SharedState) -> str:\n"
            "        return 'click'\n\n"
            "def run_task(state: SharedState) -> dict:\n"
            "    planner = Planner()\n"
            "    executor = Executor()\n"
            "    return {'plan': planner.plan(state), 'action': executor.execute(state)}\n",
            encoding="utf-8",
        )
        source.parent.joinpath("actions.py").write_text(
            "class ActionCatalog:\n"
            "    action_pool = ['click']\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "pkg"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
             "commit", "-qm", "synthetic architecture"],
            cwd=self.repo, check=True,
        )
        self.revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def compile(self, **kwargs):
        return framework_architecture.compile_repository(
            self.repo, "synthetic_framework", revision=self.revision, **kwargs
        )

    def test_compiler_extracts_shared_state_entrypoint_and_contracts(self):
        compiled = self.compile()
        names = {node["name"] for node in compiled["nodes"]}
        self.assertTrue({"SharedState", "Runner", "Planner", "Executor", "ActionCatalog", "run_task"} <= names)
        self.assertIn("pkg.core:run_task", compiled["entrypoints"])
        state = next(node for node in compiled["nodes"] if node["name"] == "SharedState")
        self.assertEqual({field["name"] for field in state["fields"]}, {"action_history", "plan"})
        self.assertIn("pkg.core:SharedState", compiled["shared_state_candidates"])
        runner = next(node for node in compiled["nodes"] if node["name"] == "Runner")
        runner_fields = {field["name"]: field for field in runner["fields"]}
        self.assertEqual(
            set(runner_fields), {"state", "retries", "last_action", "first", "second"}
        )
        self.assertEqual(runner_fields["retries"]["value"], 2)
        self.assertIn("__init__", runner_fields["retries"]["written_in"])
        self.assertIn("run", runner_fields["retries"]["written_in"])
        self.assertIn("run", runner_fields["retries"]["read_in"])
        self.assertIn("run", runner_fields["state"]["read_in"])
        self.assertEqual(runner_fields["last_action"]["value"], "")
        self.assertIsNone(runner_fields["first"]["value"])
        self.assertIsNone(runner_fields["second"]["value"])
        self.assertEqual(compiled["generator"]["version"], "3")
        modules = {module["path"]: module for module in compiled["modules"]}
        self.assertEqual(modules["pkg/core.py"]["summary"], "Synthetic architecture fixture.")
        self.assertEqual(modules["pkg/core.py"]["summary_source"], "docstring")
        self.assertIn("dataclasses", modules["pkg/core.py"]["imports"])
        self.assertEqual(modules["pkg/actions.py"]["summary"], "Defines 1 top-level component.")
        self.assertEqual(modules["pkg/actions.py"]["summary_source"], "component_count")
        self.assertIn("pkg.actions:ActionCatalog", compiled["action_components"])
        self.assertNotIn("pkg.core:Runner", compiled["action_components"])
        self.assertNotIn("pkg.core:SharedState", compiled["action_components"])

    def test_revision_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            framework_architecture.compile_repository(
                self.repo, "synthetic_framework", revision="0" * 40
            )

    def test_dirty_selected_source_is_rejected(self):
        (self.repo / "pkg/core.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            self.compile()

    def test_generated_artifact_round_trip_and_install(self):
        compiled = self.compile()
        artifact_root = self.root / "artifact"
        artifact = framework_architecture.write_artifact(artifact_root, compiled)
        binding = framework_architecture.bind_artifact(artifact_root, artifact)
        resolved = ENGINE.resolve_framework_architecture(
            binding, self.root, {"framework": "synthetic_framework"}
        )
        self.assertEqual(resolved["mode"], "generated")
        self.assertEqual(resolved["repo_revision"], self.revision)
        workspace = self.root / "workspace"
        out = self.root / "out"
        workspace.mkdir()
        out.mkdir()
        installed = ENGINE.install_framework_architecture(
            {"framework_architecture": resolved}, workspace, out
        )
        self.assertTrue((workspace / ".framework-architecture/architecture.md").is_file())
        self.assertEqual(installed["source_tree_sha256"], binding["source_tree_sha256"])
        persisted = run_case.read_json(out / "framework_architecture_binding.json")
        self.assertEqual(persisted["artifact_sha256"], binding["artifact_sha256"])

    def test_manual_overview_uses_the_same_resolver(self):
        compiled = framework_architecture.compile_manual(
            "synthetic_framework", "Synthetic manual architecture overview."
        )
        artifact_root = self.root / "manual"
        artifact = framework_architecture.write_artifact(artifact_root, compiled)
        binding = framework_architecture.bind_artifact(artifact_root, artifact)
        resolved = ENGINE.resolve_framework_architecture(
            binding, self.root, {"framework": "synthetic_framework"}
        )
        self.assertEqual(resolved["mode"], "manual")
        self.assertIsNone(resolved["repo_revision"])

    def test_tampered_architecture_is_rejected(self):
        artifact_root = self.root / "artifact"
        artifact = framework_architecture.write_artifact(artifact_root, self.compile())
        binding = framework_architecture.bind_artifact(artifact_root, artifact)
        save(artifact_root / "architecture.json", {"schema": "tampered"})
        with self.assertRaisesRegex(ValueError, "JSON changed"):
            ENGINE.resolve_framework_architecture(
                binding, self.root, {"framework": "synthetic_framework"}
            )

    def test_unregistered_artifact_file_is_rejected(self):
        artifact_root = self.root / "artifact"
        artifact = framework_architecture.write_artifact(artifact_root, self.compile())
        binding = framework_architecture.bind_artifact(artifact_root, artifact)
        (artifact_root / "unexpected.txt").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            ENGINE.resolve_framework_architecture(
                binding, self.root, {"framework": "synthetic_framework"}
            )

    def test_symlink_in_artifact_is_rejected(self):
        artifact_root = self.root / "artifact"
        artifact = framework_architecture.write_artifact(artifact_root, self.compile())
        binding = framework_architecture.bind_artifact(artifact_root, artifact)
        (artifact_root / "linked.md").symlink_to(self.repo / "pkg/core.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            ENGINE.resolve_framework_architecture(
                binding, self.root, {"framework": "synthetic_framework"}
            )

    def test_binding_registry_selects_only_the_current_framework(self):
        binding = {"schema": framework_architecture.BINDING_SCHEMA,
                   "framework": "synthetic_framework", "artifact_root": "."}
        self.assertIs(ENGINE.select_framework_binding(
            {"synthetic_framework": binding}, "synthetic_framework"), binding)
        with self.assertRaisesRegex(ValueError, "No framework architecture binding"):
            ENGINE.select_framework_binding({"other": binding}, "synthetic_framework")


class FrameworkArchitectureBuilderTests(unittest.TestCase):
    def test_cross_drive_relative_path_falls_back_to_absolute(self):
        builder = load_architecture_builder()
        with patch.object(builder.os.path, "relpath", side_effect=ValueError("different drives")):
            value = builder.relative_path(Path("D:/artifact"), Path("C:/manifest"))
        self.assertEqual(value, "D:/artifact")


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
        self.case = {"case_id": "synthetic", "identity": {"id": "synthetic"}, "available_steps": [1], "inventory": [
            {"source_id": "S0001", "source": "step_1/manager.json", "kind": "log", "step": 1},
            {"source_id": "S0002", "source": "images/frame.png", "kind": "image", "step": None},
        ]}
        self.plan = {
            "schema": "case-checkpoint-plan-v1", "case_id": "synthetic",
            "basis": ["instruction", "identity", "task_policy"],
            "checkpoints": [{
                "id": "CP1", "type": "goal", "object": "Synthetic object",
                "requirement": "Synthetic requirement is satisfied", "required": True,
                "depends_on": [], "acceptance": "Direct screenshot supports the state",
                "evidence_expectation": ["screenshot"],
            }],
        }
        save(self.workspace / "checkpoint_plan.json", self.plan)
        checkpoint_sha = hashlib.sha256((self.workspace / "checkpoint_plan.json").read_bytes()).hexdigest()
        save(self.workspace / "io_events.jsonl", {
            "at": 1, "mode": "checkpoint", "accepted": True,
            "detail": "Saved checkpoint plan sha256=" + checkpoint_sha,
        })
        self.observation = {"source_id": "S0001", "phase": "decision", "locator": "response",
                            "observation": "Synthetic module output"}
        self.record = {"step": 1, "verdict": "uncertain", "summary": "Synthetic local observation",
                       "evidence": [self.observation]}
        save(self.workspace / "step_judgments/step_001.json", self.record)
        self.synthesis = {"summary": "Synthetic cross-step attribution summary"}
        save(self.workspace / "global_summary.json", {"synthesis": self.synthesis, "goals": [{
            "id": "CP1", "object": "Synthetic object", "requirement": "Synthetic requirement is satisfied",
            "first_seen_step": 1, "evidence_ids": ["E01_1"],
        }]})
        self.receipt = {"events": [
            {"source": entry["source"], "success": True, "content_complete": True, "truncated": False}
            for entry in self.case["inventory"]
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def test_assembly_binds_evidence_and_coverage(self):
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual(result["evidence"][0]["id"], "E01_1")
        self.assertEqual(result["goals"][0]["steps"], [1])
        self.assertEqual(result["goals"][0]["step_range"], [1, 1])
        self.assertTrue(result["coverage"]["scan_complete"])
        self.assertEqual(result["coverage"]["screenshots_viewed"], ["S0002"])
        self.assertEqual(result["synthesis"], self.synthesis)
        audit = run_case.read_json(self.workspace.parent / "stepwise_assembly_audit.json")
        self.assertEqual(audit["checkpoint_step_ranges"], {"CP1": [1, 1]})

    def test_multistep_source_inherits_the_current_judgment_step(self):
        self.case["available_steps"] = [1, 2]
        self.case["inventory"][1]["steps"] = [1, 2]
        self.record = {**self.record, "evidence": [{
            "source_id": "S0002", "phase": "before_action",
            "locator": "synthetic frame", "observation": "Synthetic visible frame",
        }]}
        save(self.workspace / "step_judgments/step_001.json", self.record)
        save(self.workspace / "step_judgments/step_002.json", {**self.record, "step": 2})
        save(self.workspace / "global_summary.json", {"synthesis": self.synthesis, "goals": [{
            "id": "CP1", "object": "Synthetic object", "requirement": "Synthetic requirement is satisfied",
            "first_seen_step": 1, "evidence_ids": ["E01_1", "E02_1"],
        }]})
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual([entry["step"] for entry in result["evidence"]], [1, 2])
        self.assertEqual(result["goals"][0]["steps"], [1, 2])
        self.assertEqual(result["goals"][0]["step_range"], [1, 2])

    def test_missing_step_is_rejected(self):
        (self.workspace / "step_judgments/step_001.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_missing_checkpoint_plan_is_rejected(self):
        (self.workspace / "checkpoint_plan.json").unlink()
        with self.assertRaisesRegex(ValueError, "checkpoint_plan.json is missing"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_required_checkpoint_must_match_final_goal(self):
        save(self.workspace / "global_summary.json", {"synthesis": self.synthesis, "goals": [{
            "id": "CP1", "object": "Synthetic object", "requirement": "Changed requirement",
        }]})
        with self.assertRaisesRegex(ValueError, "preserve the checkpoint"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_checkpoint_plan_cannot_change_after_submission(self):
        self.plan["checkpoints"][0]["acceptance"] = "Changed after submission"
        save(self.workspace / "checkpoint_plan.json", self.plan)
        with self.assertRaisesRegex(ValueError, "changed after"):
            protocol.assemble(self.workspace, self.case, self.receipt)

    def test_goal_steps_are_derived_from_evidence(self):
        save(self.workspace / "global_summary.json", {"synthesis": self.synthesis, "goals": [{
            "id": "CP1", "object": "Synthetic object", "requirement": "Synthetic requirement is satisfied",
            "first_seen_step": 1, "evidence_ids": ["E01_1"], "steps": [99],
        }]})
        result = protocol.assemble(self.workspace, self.case, self.receipt)
        self.assertEqual(result["goals"][0]["steps"], [1])
        self.assertEqual(result["goals"][0]["step_range"], [1, 1])

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
            self.assertEqual(run_case.read_json(workspace / "checkpoint_plan.schema.json"), protocol.CHECKPOINT_SCHEMA)
            step_index = run_case.read_json(workspace / "step_index.json")
            self.assertEqual(step_index["checkpoint_plan"]["submission_mode"], "checkpoint")
            helper = prompt_builder.HELPER_NAME
            self.assertEqual((workspace / helper).read_bytes(), (ROOT / "stepwise/engine" / helper).read_bytes())
            self.assertEqual((workspace / "submission_protocol.md").read_bytes(), prompt_builder.RUNTIME.submission_protocol_bytes())

    def test_startup_routes_to_skill_and_staged_protocol(self):
        prompt = protocol.render_prompt({"id": "synthetic"})
        self.assertIn("$gui-trajectory-attribution", prompt)
        self.assertIn("submission_protocol.md", prompt)
        self.assertIn("checkpoint_plan.schema.json", prompt)
        self.assertIn("checkpoint mode", prompt)
        self.assertNotIn("MAIN causal failure", prompt)
        self.assertNotIn("PowerShell", prompt)

    def test_staged_schema_excludes_caller_owned_goal_steps(self):
        class FakeProtocol:
            def wire_schema(self):
                return {"type": "object", "properties": {
                    "coverage": {}, "evidence": {}, "process": {}, "goals": {"items": {
                        "type": "object", "properties": {"steps": {}, "step_range": {}, "id": {}},
                        "required": ["steps", "id"],
                    }},
                }, "required": ["coverage", "evidence", "process", "goals"]}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            protocol.stage_contract({"identity": {"id": "synthetic"}, "available_steps": [1], "inventory": []},
                                    FakeProtocol(), [], workspace)
            goal = run_case.read_json(workspace / "global_summary.schema.json")["properties"]["goals"]["items"]
            self.assertEqual(goal["properties"], {"id": {}})
            self.assertEqual(goal["required"], ["id"])


class PlatformRuntimeTests(unittest.TestCase):
    def test_windows_keeps_mxc_and_powershell_contract(self):
        runtime = platform_runtime.WindowsRuntime()
        options = runtime.sandbox_options("/tmp/work", "/tmp/home", "/tmp/runtime")
        self.assertEqual(runtime.name, "windows")
        self.assertEqual(runtime.helper_name, "io.ps1")
        self.assertEqual(runtime.protocol_path.name, "submission_protocol.md")
        self.assertEqual(options["windows.sandbox"], "mxc")
        self.assertEqual(options["permissions.d60-workspace.filesystem"]["/tmp/runtime"], "read")
        self.assertEqual(runtime.approval_args(), ["--approve-for-me"])
        self.assertEqual(runtime.approval_options(), {})
        self.assertEqual(runtime.submission_protocol_bytes(), runtime.protocol_path.read_bytes())

    def test_macos_uses_seatbelt_profile_without_windows_sandbox(self):
        runtime = platform_runtime.MacOSRuntime()
        options = runtime.sandbox_options("/tmp/work", "/tmp/home", None, ["/opt/python"])
        self.assertEqual(runtime.name, "macos")
        self.assertEqual(runtime.helper_name, "io.py")
        self.assertEqual(runtime.protocol_path.name, "submission_protocol_posix.md")
        self.assertNotIn("windows.sandbox", options)
        self.assertEqual(options["default_permissions"], "d60-workspace")
        self.assertEqual(options["permissions.d60-workspace.filesystem"][":root"], "deny")
        self.assertEqual(options["permissions.d60-workspace.filesystem"]["/opt/python"], "read")
        self.assertEqual(runtime.approval_args(), [])
        self.assertEqual(runtime.approval_options()["approvals_reviewer"], "auto_review")
        protocol = runtime.submission_protocol_bytes().decode("utf-8")
        interpreter = shlex.quote(str(Path(sys.executable).resolve()))
        self.assertIn(f"{interpreter} io.py", protocol)
        self.assertNotIn("python3 io.py", protocol)



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
    def test_checkpoint_gate_is_present_in_windows_helper(self):
        text = (ROOT / "stepwise/engine/io.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("'checkpoint','step','global','check'", text)
        self.assertIn("checkpoint_plan.json", text)
        self.assertIn("Required checkpoint", text)

    def test_submit_and_reject_duplicate(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell not installed")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            shutil.copyfile(ROOT / "stepwise/engine/io.ps1", work / "io.ps1")
            save(work / "checkpoint_plan.schema.json", protocol.CHECKPOINT_SCHEMA)
            save(work / "checkpoint_plan.json", {
                "schema": "case-checkpoint-plan-v1", "case_id": "synthetic",
                "basis": ["instruction", "identity", "task_policy"],
                "checkpoints": [{
                    "id": "CP1", "type": "goal", "object": "Synthetic object",
                    "requirement": "Synthetic requirement", "required": True, "depends_on": [],
                    "acceptance": "Direct screenshot", "evidence_expectation": ["screenshot"],
                }],
            })
            plan_sha = hashlib.sha256((work / "checkpoint_plan.json").read_bytes()).hexdigest()
            (work / "io_events.jsonl").write_text(json.dumps({
                "at": 1, "mode": "checkpoint", "accepted": True,
                "detail": "Saved checkpoint plan sha256=" + plan_sha,
            }) + "\n", encoding="utf-8")
            save(work / "input_index.json", {
                "identity": {"id": "synthetic"}, "available_steps": [1], "sources": [{"source_id": "S0001"}],
            })
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
            def project(self, result): return copy.deepcopy(result)
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
