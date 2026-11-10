"""Portable entry for the frozen v3 native Codex per-step protocol."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.dont_write_bytecode = True


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


def cli_command() -> list[str]:
    configured = os.environ.get("CODEX_BINARY")
    if configured:
        command = json.loads(configured)
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError("CODEX_BINARY must be a JSON array of command arguments")
    else:
        node = shutil.which("node")
        npm_root = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex/bin/codex.js"
        if not node or not npm_root.is_file():
            raise ValueError("Set CODEX_BINARY, or install Node and @openai/codex@0.166.0")
        command = [node, str(npm_root)]
    completed = subprocess.run(
        command + ["--version"], capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode or not re.fullmatch(r"(?:codex-cli|codex)\s+0\.166\.0", completed.stdout.strip()):
        raise ValueError("This snapshot requires Codex CLI 0.166.0")
    return command


def credentials() -> tuple[str, str]:
    from responses_transport import responses_endpoint
    base = os.environ.get("GROUND_URL", "").rstrip("/")
    key = os.environ.get("GROUND_API_KEY", "")
    if not base.startswith("https://") or not key:
        raise ValueError("Set GROUND_URL (HTTPS Responses endpoint) and GROUND_API_KEY")
    return responses_endpoint(base), key


def load_environment(path: Path | None) -> None:
    if path is not None:
        if not path.is_file():
            raise ValueError("Specified environment file does not exist")
        from dotenv import load_dotenv
        load_dotenv(path, override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", help="Batch-bound expected input manifest digest")
    parser.add_argument("--release-sha256", help="Batch-bound expected runtime manifest digest")
    parser.add_argument("--skill-manifest-sha256", help="Batch-bound expected Skill manifest digest")
    parser.add_argument("--case", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--wall", type=int, default=3600)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--preflight", action="store_true", help="No model calls or device actions")
    args = parser.parse_args()
    if args.wall <= 0:
        parser.error("--wall must be positive")
    verify_release()
    for expected, path in (
        (args.release_sha256, REPO / "RELEASE_MANIFEST.json"),
        (args.skill_manifest_sha256, ROOT / "skills/v3/manifest.json"),
    ):
        if expected and sha256(path) != expected:
            raise ValueError("Runtime or Skill changed since batch dispatch")
    load_environment(args.env_file)
    engine = load_engine()
    manifest_digest = args.manifest_sha256 or sha256(args.manifest.resolve())
    loaded = load_inputs(engine, args.manifest.resolve(), args.case, manifest_digest)
    command = cli_command()
    runtime = Path(os.environ.get(
        "CODEX_RUNTIME_DEP",
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell"),
    )).resolve()
    if not runtime.is_dir():
        raise ValueError("Set CODEX_RUNTIME_DEP to the installed Codex PowerShell runtime directory")
    if args.out.resolve().exists():
        raise ValueError("Output already exists; choose a new directory to preserve prior attempts")
    if args.preflight:
        print(json.dumps({
            "state": "preflight_passed", "case_id": args.case,
            "cli_version": "0.166.0", "skill_version": loaded[3]["skill_version"],
            "registered_sources": len(loaded[0]["inventory"]),
            "registered_images": loaded[0]["image_count"], "paid_calls": 0,
        }, ensure_ascii=False))
        return 0
    if os.name != "nt":
        raise ValueError("Native execution of this frozen mxc sandbox snapshot requires Windows")
    credentials()  # Check presence only; never print credentials.
    os.environ["CODEX_RUNTIME_DEP"] = str(runtime)
    os.environ.pop("D60_LENIENT_MERGE", None)
    engine._LENIENT = False
    engine.BINARY = command
    engine.credentials = credentials
    status = engine.run_native(*loaded, args.out.resolve(), args.wall, repair=False)
    engine.atomic(args.out.resolve() / "input_binding.json", {
        "manifest_sha256": manifest_digest,
        "runtime_manifest_sha256": sha256(REPO / "RELEASE_MANIFEST.json"),
        "skill_manifest_sha256": sha256(ROOT / "skills/v3/manifest.json"),
        "prompt_version": engine.PROMPT_VERSION,
    })
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["state"] == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
