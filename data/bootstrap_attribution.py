"""Prepare and launch a GUI attribution batch from two input roots.

The bootstrap layer only orchestrates existing adapters, architecture
compilation, preflight, and the bounded batch runner. It never modifies source
cases or guesses missing dataset metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stepwise/engine"))
from dataset_paths import group_names, matching_directories  # noqa: E402

DEFAULT_ADAPTERS = REPO / "config/datasets/failure_attribution_benchmark.json"
DEFAULT_CACHE = REPO / "data/gpt56_image_cache"
ARCHITECTURE_BUILDER = REPO / "data/build_framework_architecture.py"
MANIFEST_BUILDER = REPO / "data/build_benchmark_manifests.py"
RUN_CASE = REPO / "stepwise/run_case.py"
RUN_BATCH = REPO / "stepwise/run_batch.py"
SCHEMA = "attribution-bootstrap-v1"


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_capture(command: list[str]) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment,
    )


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_adapter_config(path: Path | None = None) -> list[dict]:
    config = read_json(path or DEFAULT_ADAPTERS)
    if config.get("schema") != "attribution-dataset-adapters-v1":
        raise ValueError("Unsupported dataset adapter configuration schema")
    adapters = config.get("adapters")
    if not isinstance(adapters, list) or not adapters:
        raise ValueError("Dataset adapter configuration contains no adapters")
    required = {"id", "framework", "group", "package", "cases", "annotation", "builder"}
    seen = set()
    for adapter in adapters:
        if not isinstance(adapter, dict) or not required.issubset(adapter):
            raise ValueError("Dataset adapter entry is incomplete")
        if adapter["id"] in seen:
            raise ValueError("Duplicate dataset adapter id: " + str(adapter["id"]))
        group_names(adapter)
        seen.add(adapter["id"])
    return adapters


def _package_candidates(cases: Path, adapters: list[dict], framework: str | None):
    resolved = Path(cases).expanduser().resolve()
    candidates = []
    search = [resolved, *resolved.parents]
    for base in search:
        for adapter in adapters:
            if framework and adapter["framework"] != framework:
                continue
            packages = (
                [base] if base.name.casefold() == adapter["package"].casefold()
                else matching_directories(base, (adapter["package"],))
            )
            accepted_groups = {name.casefold() for name in group_names(adapter)}
            for package in packages:
                group = package.parent
                annotation = package / adapter["annotation"]
                case_root = package / adapter["cases"]
                relevant = (
                    resolved == base or resolved == package or resolved == group
                    or resolved == case_root or resolved == case_root.parent
                    or resolved.is_relative_to(case_root)
                )
                if (relevant and group.name.casefold() in accepted_groups
                        and case_root.is_dir()):
                    candidates.append((adapter, group.parent, package, case_root, annotation))
    # Also accept a dataset root containing <group>/<package>.
    for adapter in adapters:
        if framework and adapter["framework"] != framework:
            continue
        for group in matching_directories(resolved, group_names(adapter)):
            for package in matching_directories(group, (adapter["package"],)):
                annotation = package / adapter["annotation"]
                case_root = package / adapter["cases"]
                if case_root.is_dir():
                    candidates.append((adapter, resolved, package, case_root, annotation))

    unique = []
    seen = set()
    for candidate in candidates:
        key = (candidate[0]["id"], str(candidate[2].resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def discover_layouts(cases: Path, adapters: list[dict], framework: str | None = None) -> list[dict]:
    candidates = _package_candidates(cases, adapters, framework)
    if not candidates:
        known = ", ".join(
            f"{item['framework']} ({item['group']}/{item['package']}/{item['cases']})"
            for item in adapters if not framework or item["framework"] == framework
        )
        raise ValueError(
            "No supported dataset layout was recognized. Provide the cases directory "
            "inside a registered layout, or add a dataset adapter. Known layouts: " + known
        )
    layouts = []
    for adapter, dataset_base, package, case_root, annotation in candidates:
        case_dirs = sorted(
            path for path in case_root.iterdir() if path.is_dir() and not path.name.startswith(".")
        )
        layouts.append({
            "adapter": adapter["id"],
            "framework": adapter["framework"],
            "builder": adapter["builder"],
            "cases_root": str(case_root.resolve()),
            "package_root": str(package.resolve()),
            "dataset_base": str(dataset_base.resolve()),
            "annotation": str(annotation.resolve()),
            "case_directories": len(case_dirs),
        })
    return layouts


def _ambiguous_layout_error(layouts: list[dict]) -> ValueError:
    labels = ", ".join(f"{layout['adapter']}={layout['package_root']}" for layout in layouts)
    return ValueError("Dataset layout is ambiguous: " + labels)


def detect_layout(cases: Path, adapters: list[dict], framework: str | None = None) -> dict:
    layouts = discover_layouts(cases, adapters, framework)
    if len(layouts) > 1:
        raise _ambiguous_layout_error(layouts)
    return layouts[0]


def resolve_layout(cases: Path, adapters: list[dict], framework: str | None,
                   code: Path | None) -> dict:
    """Use the code pack to disambiguate a multi-framework dataset root."""
    layouts = discover_layouts(cases, adapters, framework)
    if len(layouts) == 1:
        return layouts[0]
    if code is None:
        raise _ambiguous_layout_error(layouts)
    matches = []
    failures = []
    for layout in layouts:
        try:
            resolve_architecture_repo(code, layout["framework"])
        except (OSError, ValueError) as exc:
            failures.append(f"{layout['framework']}: {exc}")
        else:
            matches.append(layout)
    if len(matches) == 1:
        return matches[0]
    labels = ", ".join(layout["framework"] for layout in matches or layouts)
    detail = "; ".join(failures[-3:])
    if not matches:
        raise ValueError(
            "Dataset has multiple supported layouts, but the code path matches none of their packs: "
            + ", ".join(layout["framework"] for layout in layouts) + ("; " + detail if detail else "")
        )
    raise ValueError("Dataset layout remains ambiguous after code-path matching: " + labels + ("; " + detail if detail else ""))


def select_manifest_cases(manifest: dict, case_ids: list[str] | None) -> dict:
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Generated manifest contains no cases")
    available = [case.get("case_id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in available):
        raise ValueError("Generated manifest contains an invalid case_id")
    if len(set(available)) != len(available):
        raise ValueError("Generated manifest contains duplicate case IDs")
    if case_ids is None:
        return manifest
    requested = list(case_ids)
    if len(set(requested)) != len(requested):
        raise ValueError("Requested case IDs contain duplicates")
    missing = sorted(set(requested) - set(available))
    if missing:
        raise ValueError("Requested cases are absent from the generated manifest: " + ", ".join(missing))
    selected = set(requested)
    result = dict(manifest)
    result["cases"] = [case for case in cases if case["case_id"] in selected]
    return result


def load_manifest_builder():
    spec = importlib.util.spec_from_file_location("bootstrap_manifest_builder", MANIFEST_BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load benchmark manifest builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_manifest(layout: dict, cache_root: Path, case_ids: list[str] | None = None) -> dict:
    module = load_manifest_builder()
    registry = read_json(
        REPO / "stepwise/skills/v3/gui-trajectory-attribution/references/template-registry.json"
    )
    builders = {
        "mobile": module.build_mobile,
        "agent_s2": module.build_agent_s2,
        "uipath": module.build_uipath,
    }
    builder = builders.get(layout["builder"])
    if builder is None:
        raise ValueError("Unsupported manifest builder: " + str(layout["builder"]))
    manifest = builder(
        Path(layout["dataset_base"]), cache_root.resolve(), registry,
        package_root=Path(layout["package_root"]),
    )
    return select_manifest_cases(manifest, case_ids)


def resolve_source_repo(code: Path, source_roots: list[str]) -> Path:
    """Find the repository root in a wrapper directory without using framework names."""
    code = Path(code).expanduser().resolve()
    if not code.is_dir():
        raise ValueError("Code repository path is not a directory: " + str(code))
    roots = [Path(value) for value in source_roots if value]
    if not roots:
        return code

    def matches(directory: Path) -> bool:
        return any((directory / root).is_dir() for root in roots)

    if matches(code):
        return code
    candidates = []
    for child in sorted(code.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name == "node_modules":
            continue
        if matches(child):
            candidates.append(child)
            continue
        for grandchild in sorted(child.iterdir()):
            if (grandchild.is_dir() and not grandchild.name.startswith(".")
                    and grandchild.name != "node_modules" and matches(grandchild)):
                candidates.append(grandchild)
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise ValueError(
            "Cannot find the framework repository root under the supplied code path. "
            "Expected one of these source roots: " + ", ".join(source_roots)
        )
    raise ValueError(
        "Code path contains multiple possible repository roots: "
        + ", ".join(str(path) for path in candidates)
    )


def resolve_architecture_repo(code: Path, framework: str, pack: Path | None = None,
                              source_roots: list[str] | None = None) -> tuple[Path, Path]:
    pack_path = (pack or REPO / "config/frameworks" / framework / "pack.json").resolve()
    configuration = read_json(pack_path)
    architecture = configuration.get("architecture") or {}
    roots = source_roots or architecture.get("source_roots") or []
    return resolve_source_repo(code, roots), pack_path


def build_architecture(manifest: Path, manifest_out: Path, architecture_out: Path,
                       framework: str, *, code: Path | None, pack: Path | None, mode: str | None,
                       manual_overview: Path | None, revision: str | None,
                       source_roots: list[str] | None) -> dict:
    command = [
        sys.executable,
        str(ARCHITECTURE_BUILDER),
        "--framework", framework,
        "--out", str(architecture_out),
        "--manifest", str(manifest),
        "--manifest-out", str(manifest_out),
    ]
    if mode:
        command += ["--mode", mode]
    if pack:
        command += ["--pack", str(pack)]
    if code:
        command += ["--repo", str(code)]
    if manual_overview:
        command += ["--manual-overview", str(manual_overview)]
    if revision:
        command += ["--revision", revision]
    for source_root in source_roots or []:
        command += ["--source-root", source_root]
    completed = run_capture(command)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError("Architecture compilation failed: " + detail[-2000:])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Architecture compiler returned unexpected output") from exc


def _require_empty_output(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError("Output path exists and is not a directory: " + str(path))
        if any(path.iterdir()):
            raise ValueError("Output directory is not empty; use a new directory: " + str(path))
    path.mkdir(parents=True, exist_ok=True)


def prepare(args) -> dict:
    adapters = load_adapter_config(args.adapters)
    layout = resolve_layout(args.cases, adapters, args.framework, args.code)
    output = args.out.expanduser().resolve()
    pack = (args.pack or REPO / "config/frameworks" / layout["framework"] / "pack.json").resolve()
    configuration = read_json(pack) if pack.is_file() else {"architecture": {}}
    configured_mode = (configuration.get("architecture") or {}).get("mode", "generated")
    mode = args.mode or configured_mode
    code = None
    if mode == "generated":
        if args.code is None:
            raise ValueError("--code is required unless --mode manual is selected")
        code, pack = resolve_architecture_repo(
            args.code, layout["framework"], pack, args.source_roots,
        )
    elif mode == "manual" and args.code is not None:
        code = args.code.expanduser().resolve()
    if mode == "manual" and args.manual_overview is None:
        default_overview = pack.parent / "manual-overview.md"
        if not default_overview.is_file():
            raise ValueError("--manual-overview is required for manual architecture mode")

    _require_empty_output(output)
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        layout, args.cache_root.expanduser().resolve(), args.case_ids,
    )
    manifest_path = inputs / "prediction_manifest.json"
    bound_manifest_path = inputs / "run_manifest.json"
    write_json(manifest_path, manifest)

    architecture_dir = output / "architecture" / layout["framework"]
    architecture = build_architecture(
        manifest_path, bound_manifest_path, architecture_dir, layout["framework"],
        code=code, pack=pack, mode=mode,
        manual_overview=args.manual_overview.expanduser().resolve() if args.manual_overview else None,
        revision=args.revision, source_roots=args.source_roots,
    )
    bound_manifest = read_json(bound_manifest_path)
    cases = [case["case_id"] for case in bound_manifest["cases"]]
    binding = bound_manifest.get("framework_architecture")
    if binding is None:
        binding = (bound_manifest.get("framework_architectures") or {}).get(layout["framework"])
    record = {
        "schema": SCHEMA,
        "status": "prepared",
        "framework": layout["framework"],
        "adapter": layout["adapter"],
        "input": {
            "cases": str(args.cases.expanduser().resolve()),
            "code": str(args.code.expanduser().resolve()) if args.code else None,
            "code_root": str(code) if code else None,
            "case_scope": cases,
        },
        "layout": layout,
        "manifest": {
            "path": str(bound_manifest_path.resolve()),
            "sha256": sha256(bound_manifest_path),
            "cases": len(cases),
            "prediction_manifest": str(manifest_path.resolve()),
        },
        "architecture": {
            "path": str(architecture_dir.resolve()),
            "binding": binding,
            "mode": architecture.get("mode"),
            "repo_revision": architecture.get("repo_revision"),
        },
        "options": {
            "jobs": args.jobs,
            "wall_seconds": args.wall,
            "execution_backend": "flux_native",
        },
        "notes": [
            "The manifest contains evidence and image references, not human GT.",
            "The architecture is a frozen framework-understanding input, not attempt evidence.",
            "A prepared session can be resumed only by using the same manifest digest.",
        ],
    }
    if args.preflight:
        if not cases:
            raise ValueError("Cannot preflight an empty case scope")
        preflight_out = output / "preflight" / cases[0]
        command = [
            sys.executable, str(RUN_CASE), "--manifest", str(bound_manifest_path),
            "--case", cases[0], "--out", str(preflight_out), "--preflight",
        ]
        completed = run_capture(command)
        record["preflight"] = {
            "case_id": cases[0],
            "output": str(preflight_out.resolve()),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        }
        if completed.returncode != 0:
            record["status"] = "preflight_failed"
            atomic_json(output / "bootstrap.json", record)
            raise RuntimeError("Preflight failed: " + (completed.stderr or completed.stdout).strip()[-2000:])

    atomic_json(output / "bootstrap.json", record)
    return record


def inspect(args) -> dict:
    layout = resolve_layout(
        args.cases, load_adapter_config(args.adapters), args.framework, args.code,
    )
    result = {
        "schema": "attribution-bootstrap-inspection-v1",
        "status": "ready",
        "layout": layout,
        "selection": "The registered adapter determines case scope; generated manifests never receive human GT.",
        "scope_note": "layout.case_directories counts directories; prepare reports the exact adapter-selected manifest scope.",
        "next": "Run the prepare subcommand with --code and --out.",
    }
    if args.code is not None:
        repo, pack = resolve_architecture_repo(args.code, layout["framework"], args.pack)
        result["code"] = {
            "input": str(args.code.expanduser().resolve()),
            "repo_root": str(repo),
            "pack": str(pack),
        }
    return result


def run_prepared(args) -> int:
    prepared = args.prepared.expanduser().resolve()
    record = read_json(prepared / "bootstrap.json")
    if record.get("schema") != SCHEMA or record.get("status") != "prepared":
        raise ValueError("Prepared session is not in a runnable state")
    manifest = Path(record["manifest"]["path"]).resolve()
    if sha256(manifest) != record["manifest"]["sha256"]:
        raise ValueError("Prepared manifest changed after bootstrap")
    batch_out = (args.batch_out or prepared / "batch").expanduser().resolve()
    command = [
        sys.executable, str(RUN_BATCH), "--manifest", str(manifest),
        "--out", str(batch_out), "--jobs", str(args.jobs), "--wall", str(args.wall),
        "--max-attempts", str(getattr(args, "max_attempts", 3)),
    ]
    if args.case_ids:
        command += ["--cases", *args.case_ids]
    if args.resume:
        command += ["--resume"]
    if args.stop_on_invalid:
        command += ["--stop-on-invalid"]
    return subprocess.run(command).returncode


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Recognize an input layout without writing files")
    inspect_parser.add_argument("--cases", required=True, type=Path)
    inspect_parser.add_argument("--code", type=Path)
    inspect_parser.add_argument("--framework", choices=("mobile_agent_v3", "agent_s2", "uipath"))
    inspect_parser.add_argument("--adapters", type=Path)
    inspect_parser.add_argument("--pack", type=Path)
    inspect_parser.set_defaults(handler=inspect)

    prepare_parser = subparsers.add_parser("prepare", help="Build a manifest, architecture binding, and optional preflight")
    prepare_parser.add_argument("--cases", required=True, type=Path)
    prepare_parser.add_argument("--code", type=Path)
    prepare_parser.add_argument("--out", required=True, type=Path)
    prepare_parser.add_argument("--framework", choices=("mobile_agent_v3", "agent_s2", "uipath"))
    prepare_parser.add_argument("--adapters", type=Path)
    prepare_parser.add_argument("--pack", type=Path)
    prepare_parser.add_argument("--mode", choices=("generated", "manual"))
    prepare_parser.add_argument("--manual-overview", type=Path)
    prepare_parser.add_argument("--revision")
    prepare_parser.add_argument("--source-root", dest="source_roots", action="append")
    prepare_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    prepare_parser.add_argument("--case-id", dest="case_ids", action="append")
    prepare_parser.add_argument("--preflight", action="store_true")
    prepare_parser.add_argument("--jobs", type=int, choices=range(1, 6), default=1)
    prepare_parser.add_argument("--wall", type=int, default=3600)
    prepare_parser.set_defaults(handler=prepare)

    run_parser = subparsers.add_parser("run", help="Run a prepared session with the bounded batch runner")
    run_parser.add_argument("--prepared", required=True, type=Path)
    run_parser.add_argument("--batch-out", type=Path)
    run_parser.add_argument("--case-id", dest="case_ids", action="append")
    run_parser.add_argument("--jobs", type=int, choices=range(1, 6), default=1)
    run_parser.add_argument("--wall", type=int, default=3600)
    run_parser.add_argument("--max-attempts", type=int, choices=range(1, 11), default=3)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--stop-on-invalid", action="store_true")
    run_parser.set_defaults(handler=run_prepared)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "run":
        return args.handler(args)
    print(json.dumps(args.handler(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
