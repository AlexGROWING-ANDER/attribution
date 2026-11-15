#!/usr/bin/env python3
"""Compile a framework architecture artifact and optionally bind it to a manifest."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "stepwise" / "engine"
sys.path.insert(0, str(ENGINE))

import framework_architecture as architecture  # noqa: E402


DEFAULT_PACKS = REPO / "config" / "frameworks"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def pack_path(framework: str) -> Path:
    return DEFAULT_PACKS / framework / "pack.json"


def load_pack(path: Path, framework: str) -> dict:
    if not path.is_file():
        return {"schema": "framework-pack-v1", "framework": framework, "architecture": {}}
    pack = read_json(path)
    if pack.get("schema") != "framework-pack-v1" or pack.get("framework") != framework:
        raise ValueError("Framework pack identity mismatch")
    if not isinstance(pack.get("architecture"), dict):
        raise ValueError("Framework pack has no architecture configuration")
    return pack


def configured_repo(pack: dict, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    config = pack["architecture"]
    env_name = config.get("repo_env")
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser().resolve()
    if config.get("repo"):
        return Path(config["repo"]).expanduser().resolve()
    raise ValueError("Set --repo or the framework pack's repository environment variable")


def configured_overview(pack: dict, explicit: Path | None, pack_file: Path) -> Path:
    if explicit is not None:
        return explicit.resolve()
    value = pack.get("manual_overview")
    if value:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else pack_file.parent / path).resolve()
    return pack_file.parent / "manual-overview.md"


def relative_path(path: Path, base: Path) -> str:
    """Return a portable relative path, or an absolute path across Windows drives."""
    try:
        value = os.path.relpath(path, base)
    except ValueError:
        value = os.fspath(path)
    return value.replace("\\", "/")


def attach_binding(manifest_path: Path, manifest_out: Path, binding: dict) -> dict:
    manifest = read_json(manifest_path)
    if not isinstance(manifest.get("cases"), list):
        raise ValueError("Input manifest has no cases")
    frameworks = {
        case.get("identity", {}).get("framework")
        for case in manifest["cases"]
        if isinstance(case, dict)
    }
    if binding["framework"] not in frameworks:
        raise ValueError("Manifest contains no case for framework: " + binding["framework"])
    relative_root = relative_path(Path(binding["artifact_root"]), manifest_out.parent)
    value = {**binding, "artifact_root": relative_root}
    if frameworks == {binding["framework"]}:
        manifest["framework_architecture"] = value
    else:
        registry = manifest.setdefault("framework_architectures", {})
        if not isinstance(registry, dict):
            raise ValueError("Manifest architecture registry is not an object")
        registry[binding["framework"]] = value
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    write_json(manifest_out, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", required=True)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mode", choices=("generated", "manual"))
    parser.add_argument("--manual-overview", type=Path)
    parser.add_argument("--source-root", action="append")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-out", type=Path)
    args = parser.parse_args()
    if bool(args.manifest) != bool(args.manifest_out):
        parser.error("--manifest and --manifest-out must be supplied together")

    pack_file = (args.pack or pack_path(args.framework)).resolve()
    pack = load_pack(pack_file, args.framework)
    config = pack["architecture"]
    mode = args.mode or config.get("mode", "generated")
    if mode == "generated":
        repo = configured_repo(pack, args.repo)
        roots = args.source_root or config.get("source_roots") or []
        compiled = architecture.compile_repository(
            repo,
            args.framework,
            revision=args.revision or config.get("revision"),
            source_roots=roots,
            include=config.get("include") or ["**/*.py"],
            exclude=config.get("exclude") or [],
        )
    else:
        overview = configured_overview(pack, args.manual_overview, pack_file)
        if not overview.is_file():
            raise ValueError("Manual architecture overview does not exist: " + str(overview))
        compiled = architecture.compile_manual(
            args.framework,
            overview.read_text(encoding="utf-8-sig"),
            repo_revision=args.revision or config.get("revision"),
        )
    artifact = architecture.write_artifact(args.out.resolve(), compiled)
    binding = architecture.bind_artifact(args.out.resolve(), artifact)
    if args.manifest:
        attach_binding(args.manifest.resolve(), args.manifest_out.resolve(), binding)
    print(json.dumps({
        "framework": binding["framework"],
        "mode": binding["mode"],
        "artifact_root": binding["artifact_root"],
        "repo_revision": binding["repo_revision"],
        "source_tree_sha256": binding["source_tree_sha256"],
        "selected_nodes": compiled.get("extraction", {}).get("selected_node_count"),
        "bindings": binding,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
