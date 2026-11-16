"""Offline tests for the two-path attribution bootstrap layer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "test_bootstrap_module", ROOT / "data/bootstrap_attribution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bootstrap = load_bootstrap()
        self.adapters = self.bootstrap.load_adapter_config()

    def tearDown(self):
        self.temp.cleanup()

    def package(self, group: str, package: str, cases: str, annotation: str,
                *, write_annotation: bool = True) -> Path:
        root = self.root / "benchmark" / group / package
        (root / cases / "case_001").mkdir(parents=True)
        (root / cases / "case_002").mkdir(parents=True)
        if write_annotation:
            annotation_path = root / annotation
            annotation_path.parent.mkdir(parents=True, exist_ok=True)
            annotation_path.write_text("{}", encoding="utf-8")
        return root

    def test_detects_layouts_without_annotation_files(self):
        packages = [
            ("Agent_S2", "gpt5.6-sol", "cases/agents",
             "annotations/Agent-S2_完整标注.json", "agent_s2"),
            ("Mobile_Agent_v3", "gpt5.6", "cases/converted_121cases",
             "annotations/mobileagent_121/映射表.csv", "mobile_agent_v3"),
            ("uipath", "gpt5.6", "cases/unipath_130",
             "annotations/UniPath_完整标注.json", "uipath"),
        ]
        for group, package, cases, annotation, framework in packages:
            with self.subTest(framework=framework):
                actual = self.package(
                    group, package, cases, annotation, write_annotation=False,
                )
                layout = self.bootstrap.detect_layout(actual / cases, self.adapters)
                self.assertEqual(layout["framework"], framework)
                self.assertFalse(Path(layout["annotation"]).exists())

    def test_detects_registered_layout_from_cases_and_dataset_root(self):
        mobile = self.package(
            "mobile_Agent_v3", "gpt5.6", "cases/converted_121cases",
            "annotations/mobileagent_121/映射表.csv",
        )
        cases_root = mobile / "cases/converted_121cases"
        direct = self.bootstrap.detect_layout(cases_root, self.adapters)
        self.assertEqual(direct["framework"], "mobile_agent_v3")
        self.assertEqual(direct["case_directories"], 2)

        dataset_base = self.root / "benchmark"
        parent = self.bootstrap.detect_layout(dataset_base, self.adapters)
        self.assertEqual(parent["framework"], "mobile_agent_v3")
        self.assertEqual(Path(parent["dataset_base"]), dataset_base.resolve())

    def test_detects_agent_and_uipath_layouts(self):
        agent = self.package(
            "agent_s2", "gpt5.6-sol", "cases/agents",
            "annotations/Agent-S2_完整标注.json",
        )
        uipath = self.package(
            "ui_path", "gpt5.6", "cases/unipath_130",
            "annotations/UniPath_完整标注.json",
        )
        self.assertEqual(
            self.bootstrap.detect_layout(agent / "cases/agents", self.adapters)["framework"],
            "agent_s2",
        )
        self.assertEqual(
            self.bootstrap.detect_layout(uipath / "cases/unipath_130", self.adapters)["framework"],
            "uipath",
        )

    def test_unknown_and_ambiguous_layouts_are_rejected(self):
        unknown = self.root / "unknown/cases"
        unknown.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "No supported dataset layout"):
            self.bootstrap.detect_layout(unknown, self.adapters)

        self.package("mobile_Agent_v3", "gpt5.6", "cases/converted_121cases", "annotations/mobileagent_121/映射表.csv")
        self.package("ui_path", "gpt5.6", "cases/unipath_130", "annotations/UniPath_完整标注.json")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.bootstrap.detect_layout(self.root / "benchmark", self.adapters)

    def test_actual_directory_names_and_package_case_are_preserved(self):
        packages = [
            ("Agent_S2", "GPT5.6-SOL", "cases/agents",
             "annotations/Agent-S2_完整标注.json", "agent_s2"),
            ("Mobile_Agent_v3", "gpt5.6", "cases/converted_121cases",
             "annotations/mobileagent_121/映射表.csv", "mobile_agent_v3"),
            ("uipath", "gpt5.6", "cases/unipath_130",
             "annotations/UniPath_完整标注.json", "uipath"),
        ]
        builder = self.bootstrap.load_manifest_builder()
        for group, package, cases, annotation, framework in packages:
            with self.subTest(framework=framework):
                actual = self.package(group, package, cases, annotation)
                direct = self.bootstrap.detect_layout(actual / cases, self.adapters)
                dataset = self.bootstrap.detect_layout(
                    self.root / "benchmark", self.adapters, framework,
                )
                self.assertEqual(direct["framework"], framework)
                self.assertEqual(Path(dataset["package_root"]), actual.resolve())
                self.assertEqual(
                    builder.dataset_package(self.root / "benchmark", framework, None),
                    actual.resolve(),
                )

    def test_directory_alias_collision_is_rejected(self):
        self.package("uipath", "gpt5.6", "cases/unipath_130",
                     "annotations/UniPath_完整标注.json")
        self.package("ui_path", "gpt5.6", "cases/unipath_130",
                     "annotations/UniPath_完整标注.json")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.bootstrap.detect_layout(self.root / "benchmark", self.adapters, "uipath")
        builder = self.bootstrap.load_manifest_builder()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            builder.dataset_package(self.root / "benchmark", "uipath", None)

    def test_manifest_builder_receives_the_discovered_package_path(self):
        actual = self.package(
            "Agent_S2", "gpt5.6-sol", "cases/agents",
            "annotations/Agent-S2_完整标注.json",
        )
        layout = self.bootstrap.detect_layout(actual, self.adapters)
        builder = self.bootstrap.load_manifest_builder()
        manifest = {"cases": [{"case_id": "case_001"}]}
        cache = self.root / "cache"
        with patch.object(self.bootstrap, "load_manifest_builder", return_value=builder), patch.object(
            builder, "build_agent_s2", return_value=manifest,
        ) as build:
            self.assertEqual(self.bootstrap.build_manifest(layout, cache), manifest)
        self.assertEqual(build.call_args.kwargs["package_root"], actual.resolve())

    def test_manifest_builder_includes_success_but_skips_explicit_exclusions(self):
        builder = self.bootstrap.load_manifest_builder()
        rows = [
            {"case_id": "failed", "status": "fail"},
            {"case_id": "successful", "status": "success"},
            {"case_id": "blocked", "status": "precondition_blocked"},
            {"case_id": "excluded", "status": "excluded", "excluded": True},
        ]
        self.assertEqual(
            [row["case_id"] for row in builder.select_benchmark_rows(rows)],
            ["failed", "successful", "blocked"],
        )

    def test_manifest_builder_derives_instructions_without_annotations(self):
        builder = self.bootstrap.load_manifest_builder()
        agent = self.root / "agent/com.example_test_1"
        agent.mkdir(parents=True)
        (agent / "original_console.log").write_text(
            "[2026-01-01 00:00:00,000 INFO] Intent: 打开应用并完成任务\n",
            encoding="utf-8",
        )
        descriptor = builder.agent_case_descriptor(agent)
        self.assertEqual(descriptor["case_key"], "com.example::test_1")
        self.assertEqual(descriptor["intent"], "打开应用并完成任务")

        mobile = self.root / "mobile/case_001"
        (mobile / "step_1").mkdir(parents=True)
        (mobile / "step_1/manager.json").write_text(json.dumps({
            "messages": [{"content": [{
                "type": "text",
                "text": "### User Request ###\n搜索并打开目标页面\n\n---\nFollow the plan.",
            }]}],
        }, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(builder.mobile_instruction(mobile), "搜索并打开目标页面")

    def test_code_path_disambiguates_multi_framework_dataset_root(self):
        self.package("mobile_Agent_v3", "gpt5.6", "cases/converted_121cases", "annotations/mobileagent_121/映射表.csv")
        self.package("ui_path", "gpt5.6", "cases/unipath_130", "annotations/UniPath_完整标注.json")
        repository = self.root / "codes/Mobile-Agent-v3"
        (repository / "mobile_v3").mkdir(parents=True)
        layout = self.bootstrap.resolve_layout(
            self.root / "benchmark", self.adapters, None, self.root / "codes",
        )
        self.assertEqual(layout["framework"], "mobile_agent_v3")

    def test_manifest_case_selection_is_strict(self):
        manifest = {"cases": [{"case_id": "case_001"}, {"case_id": "case_002"}]}
        selected = self.bootstrap.select_manifest_cases(manifest, ["case_002"])
        self.assertEqual([case["case_id"] for case in selected["cases"]], ["case_002"])
        with self.assertRaisesRegex(ValueError, "absent"):
            self.bootstrap.select_manifest_cases(manifest, ["missing"])
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.bootstrap.select_manifest_cases(manifest, ["case_001", "case_001"])


    def test_resolves_repository_inside_wrapper_without_framework_names(self):
        wrapper = self.root / "codes"
        repository = wrapper / "wrapped-repository"
        (repository / "mobile_v3").mkdir(parents=True)
        self.assertEqual(
            self.bootstrap.resolve_source_repo(wrapper, ["mobile_v3"]),
            repository.resolve(),
        )

    def test_prepare_builds_bound_manifest_and_record(self):
        mobile = self.package(
            "mobile_Agent_v3", "gpt5.6", "cases/converted_121cases",
            "annotations/mobileagent_121/映射表.csv",
        )
        repository = self.root / "codes/Mobile-Agent-v3"
        (repository / "mobile_v3").mkdir(parents=True)
        output = self.root / "prepared"
        manifest = {
            "cases": [
                {"case_id": "case_001", "identity": {"framework": "mobile_agent_v3"}},
                {"case_id": "case_002", "identity": {"framework": "mobile_agent_v3"}},
            ],
            "image_delivery": {"mode": "synthetic", "cache_root": str(self.root / "cache")},
        }

        def fake_architecture(manifest_path, manifest_out, architecture_out, framework, **kwargs):
            value = self.bootstrap.read_json(manifest_path)
            value["framework_architecture"] = {"framework": framework, "artifact_root": "../architecture"}
            self.bootstrap.write_json(manifest_out, value)
            return {
                "bindings": value["framework_architecture"],
                "mode": "generated",
                "repo_revision": "synthetic",
            }

        args = SimpleNamespace(
            cases=mobile / "cases/converted_121cases",
            code=self.root / "codes",
            out=output,
            framework=None,
            adapters=None,
            cache_root=self.root / "cache",
            case_ids=["case_001"],
            mode=None,
            pack=None,
            manual_overview=None,
            revision=None,
            source_roots=None,
            preflight=False,
            env_file=None,
            jobs=8,
            wall=3600,
        )
        def fake_manifest(layout, cache_root, case_ids=None):
            return self.bootstrap.select_manifest_cases(manifest, case_ids)

        with patch.object(self.bootstrap, "build_manifest", side_effect=fake_manifest), patch.object(
            self.bootstrap, "build_architecture", side_effect=fake_architecture
        ):
            record = self.bootstrap.prepare(args)

        bound = output / "inputs/run_manifest.json"
        self.assertEqual(record["status"], "prepared")
        self.assertEqual(record["manifest"]["cases"], 1)
        self.assertEqual(record["manifest"]["sha256"], hashlib.sha256(bound.read_bytes()).hexdigest())
        self.assertEqual(
            [case["case_id"] for case in self.bootstrap.read_json(bound)["cases"]],
            ["case_001"],
        )
        self.assertTrue((output / "bootstrap.json").is_file())

    def test_run_checks_manifest_digest_and_dispatches_batch(self):
        prepared = self.root / "prepared"
        manifest = prepared / "inputs/run_manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"cases": []}\n', encoding="utf-8")
        record = {
            "schema": self.bootstrap.SCHEMA,
            "status": "prepared",
            "manifest": {
                "path": str(manifest),
                "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            },
            "options": {"env_file": None},
        }
        self.bootstrap.atomic_json(prepared / "bootstrap.json", record)
        args = SimpleNamespace(
            prepared=prepared,
            batch_out=self.root / "batch",
            env_file=None,
            case_ids=["case_001"],
            jobs=2,
            wall=60,
            resume=False,
            stop_on_invalid=False,
        )
        completed = SimpleNamespace(returncode=0)
        with patch.object(self.bootstrap.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.bootstrap.run_prepared(args), 0)
        command = run.call_args.args[0]
        self.assertIn(str(self.bootstrap.RUN_BATCH), command)
        self.assertIn(str(manifest.resolve()), command)
        self.assertIn("--cases", command)

        manifest.write_text('{"cases": [1]}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.bootstrap.run_prepared(args)


if __name__ == "__main__":
    unittest.main()
