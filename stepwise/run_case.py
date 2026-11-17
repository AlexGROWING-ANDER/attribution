"""Prepare and validate the frozen per-step protocol for native Flux execution."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "engine"))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_path(value: str, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else relative_to / path).resolve()


def validate_case_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("Case ID must be a plain directory name without path separators")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
    if value.endswith(".") or value.split(".", 1)[0].upper() in reserved:
        raise ValueError("Case ID is not a valid unambiguous Windows directory name")
    return value


def verify_release() -> None:
    """Check exported runtime files as well as the original Skill snapshot."""
    record = read_json(REPO / "RELEASE_MANIFEST.json")
    for relative, expected in record["files"].items():
        path = resolve_path(relative, REPO)
        if not path.is_relative_to(REPO) or sha256(path) != expected:
            raise ValueError(f"Release file changed: {relative}")


def load_engine():
    spec = importlib.util.spec_from_file_location(
        "stepwise_native_engine", ROOT / "engine" / "native_engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_framework_inputs(engine, manifest: dict, base: Path, identity: dict):
    """Resolve exactly one framework reference source, preferring architecture."""
    architecture_registry = manifest.get("framework_architecture")
    if architecture_registry is None:
        architecture_registry = manifest.get("framework_architectures")
    architecture_binding = engine.select_framework_binding(
        architecture_registry, identity["framework"]
    )
    architecture = engine.resolve_framework_architecture(
        architecture_binding, base, identity
    )
    knowledge = None
    if architecture is None:
        knowledge = engine.resolve_framework_knowledge(
            manifest.get("framework_knowledge"), base, identity
        )
    return knowledge, architecture


def load_inputs(engine, manifest_path: Path, case_id: str, expected_manifest_sha256: str | None = None):
    validate_case_id(case_id)
    payload = manifest_path.read_bytes()
    if expected_manifest_sha256 and hashlib.sha256(payload).hexdigest() != expected_manifest_sha256:
        raise ValueError("Input manifest changed since batch dispatch")
    manifest = json.loads(payload.decode("utf-8-sig"))
    cases = [case for case in manifest["cases"] if case["case_id"] == case_id]
    if len(cases) != 1:
        raise ValueError("Case ID must occur exactly once in the manifest")
    case = json.loads(json.dumps(cases[0]))
    case["evidence_root"] = str(resolve_path(case["evidence_root"], manifest_path.parent))
    for image in case["images"]:
        image["delivery_path"] = str(resolve_path(image["delivery_path"], manifest_path.parent))
    delivery = dict(manifest["image_delivery"])
    if delivery.get("cache_root"):
        delivery["cache_root"] = str(resolve_path(delivery["cache_root"], manifest_path.parent))
    manifest["image_delivery"] = delivery
    skill_root = ROOT / "skills" / "v3"
    skill = skill_root / "gui-trajectory-attribution"
    skill_manifest = read_json(skill_root / "manifest.json")
    for relative, expected in skill_manifest["files"].items():
        path = resolve_path(relative, skill)
        if not path.is_relative_to(skill) or sha256(path) != expected:
            raise ValueError(f"Skill file changed: {relative}")
    schema = read_json(skill / "references" / "output.schema.json")
    engine.Draft202012Validator(schema["properties"]["case"]).validate(case["identity"])
    if case["identity"]["id"] != case_id:
        raise ValueError("Case identity does not match selected case ID")
    case["framework_knowledge"], case["framework_architecture"] = resolve_framework_inputs(
        engine, manifest, manifest_path.parent, case["identity"]
    )
    registry = read_json(skill / "references" / "template-registry.json")
    template = registry["templates"].get(case["identity"]["template_id"])
    if (not template or template["framework"] != case["identity"]["framework"]
            or template["sha256"] != case["identity"]["template_sha256"]):
        raise ValueError("Case template identity does not match the frozen registry")
    catalog = engine.SourceCatalog(case["inventory"])
    for entry in case["inventory"]:
        if catalog.by_path[entry["source"]]["source_id"] != entry["source_id"]:
            raise ValueError("Source IDs do not match the canonical input catalog")
    expected_images = {entry["source_id"] for entry in catalog.entries if entry["kind"] == "image"}
    image_ids = [entry["source_id"] for entry in case["images"]]
    if (type(case["image_count"]) is not int or case["image_count"] != len(expected_images)
            or len(image_ids) != len(set(image_ids)) or set(image_ids) != expected_images):
        raise ValueError("Image count or delivery image list differs from the registered gallery")
    for image in case["images"]:
        source = catalog.by_id[image["source_id"]]
        if image["source"] != source["source"] or image["sha256"] != source["sha256"]:
            raise ValueError("Image delivery source does not match its registered source")
    protocol = engine.OutputProtocol(
        schema, case["identity"], skill_manifest["skill_version"], catalog,
        case["available_steps"],
    )
    store = engine.EvidenceStore(
        Path(case["evidence_root"]), case["inventory"], case["identity"]["attempt"],
        delivery, case["image_scope"],
    )
    # Validate the exact registered source bytes before any model request.
    for entry in case["inventory"]:
        path = resolve_path(entry["source"], Path(case["evidence_root"]))
        if not path.is_relative_to(Path(case["evidence_root"])):
            raise ValueError("Evidence escapes its attempt")
        if sha256(path) != entry["sha256"]:
            raise ValueError("Evidence hash changed: " + entry["source_id"])
    for image in case["images"]:
        if sha256(Path(image["delivery_path"])) != image["delivery_sha256"]:
            raise ValueError("Image delivery cache changed")
    return case, manifest, skill, skill_manifest, protocol, store


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or finalize native Flux attribution")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--release-sha256")
    parser.add_argument("--skill-manifest-sha256")
    parser.add_argument("--case", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--wall", type=int, default=3600, help="Native Flux task time budget")
    parser.add_argument("--retry-context", type=Path,
                        help="Validation feedback from the previous attempt for a fresh retry")
    parser.add_argument("--preflight", action="store_true", help="No inference or device actions")
    parser.add_argument("--finalize", action="store_true", help="Validate completed Flux submissions")
    args = parser.parse_args()
    if args.wall <= 0:
        parser.error("--wall must be positive")
    if args.preflight and args.finalize:
        parser.error("--preflight and --finalize are mutually exclusive")
    if args.retry_context and (args.preflight or args.finalize):
        parser.error("--retry-context is only valid while preparing a fresh attempt")
    verify_release()
    for expected, path in (
        (args.release_sha256, REPO / "RELEASE_MANIFEST.json"),
        (args.skill_manifest_sha256, ROOT / "skills/v3/manifest.json"),
    ):
        if expected and sha256(path) != expected:
            raise ValueError("Runtime or Skill changed since batch dispatch")
    engine = load_engine()
    manifest_path = args.manifest.resolve()
    manifest_digest = args.manifest_sha256 or sha256(manifest_path)
    loaded = load_inputs(engine, manifest_path, args.case, manifest_digest)
    if args.preflight:
        print(json.dumps({"state": "preflight_passed", "case_id": args.case,
                          "execution_backend": "flux_native",
                          "registered_sources": len(loaded[0]["inventory"]),
                          "registered_images": loaded[0]["image_count"],
                          "inference_started": False}, ensure_ascii=False))
        return 0
    from flux_execution import prepare_case, finalize_case, read_json as read_flux_json
    if args.finalize:
        binding = read_flux_json(args.out.resolve() / "input_binding.json")
        if (binding["case_id"] != args.case or binding["manifest_sha256"] != manifest_digest
                or Path(binding["manifest_path"]).resolve() != manifest_path):
            raise ValueError("Finalize inputs differ from the dispatched case")
        status = finalize_case(args.out.resolve())
    else:
        retry_context = read_json(args.retry_context.resolve()) if args.retry_context else None
        status = prepare_case(loaded, args.out.resolve(), {
            "manifest_path": str(manifest_path), "manifest_sha256": manifest_digest,
            "runtime_manifest_sha256": sha256(REPO / "RELEASE_MANIFEST.json"),
            "skill_manifest_sha256": sha256(ROOT / "skills/v3/manifest.json"),
            "wall_seconds": args.wall,
        }, retry_context)
    print(json.dumps(status, ensure_ascii=False))
    if args.finalize:
        return 0 if status["state"] == "validated" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
