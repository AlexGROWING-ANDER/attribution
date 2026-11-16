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

try:
    import tomllib
except ImportError:  # Python 3.10 fallback
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

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


DEFAULT_CODEX_VERSION = "0.166.0"


def enforce_codex_version() -> bool:
    value = os.environ.get("ATTRIBUTION_ENFORCE_CODEX_VERSION", "").strip().lower() or "0"
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    raise ValueError("ATTRIBUTION_ENFORCE_CODEX_VERSION must be 0 or 1")


def expected_codex_version() -> str:
    return os.environ.get("ATTRIBUTION_CODEX_VERSION", "").strip() or DEFAULT_CODEX_VERSION


def cli_version(command: list[str]) -> str:
    completed = subprocess.run(
        command + ["--version"], capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        raise ValueError("Codex CLI --version failed: " + (completed.stderr or completed.stdout).strip())
    output = completed.stdout.strip()
    match = re.search(r"(?:codex-cli|codex)\s+(\S+)", output, re.IGNORECASE)
    return match.group(1) if match else output


def canonical_cli_command(command: list[str]) -> list[str]:
    """Use the real executable path so Seatbelt need not traverse CLI symlinks."""
    resolved = list(command)
    executable = Path(resolved[0]).expanduser()
    if executable.is_absolute() and executable.is_file():
        resolved[0] = str(executable.resolve())
    return resolved


def resolve_cli() -> tuple[list[str], str]:
    configured = os.environ.get("CODEX_BINARY")
    if configured:
        command = json.loads(configured)
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError("CODEX_BINARY must be a JSON array of command arguments")
    else:
        if os.name == "nt":
            node = shutil.which("node")
            npm_root = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex/bin/codex.js"
            if not node or not npm_root.is_file():
                raise ValueError("Set CODEX_BINARY, or install Node and @openai/codex")
            command = [node, str(npm_root)]
        else:
            codex = shutil.which("codex")
            if codex:
                command = [codex]
            else:
                node, npm = shutil.which("node"), shutil.which("npm")
                npm_root = None
                if node and npm:
                    try:
                        completed_npm = subprocess.run(
                            [npm, "root", "-g"], capture_output=True, text=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        if completed_npm.returncode == 0 and completed_npm.stdout.strip():
                            npm_root = Path(completed_npm.stdout.strip()) / "@openai/codex/bin/codex.js"
                    except (OSError, subprocess.SubprocessError):
                        npm_root = None
                if not node or npm_root is None or not npm_root.is_file():
                    raise ValueError("Set CODEX_BINARY, or install Codex CLI")
                command = [node, str(npm_root)]
    command = canonical_cli_command(command)
    detected = cli_version(command)
    if enforce_codex_version() and detected != expected_codex_version():
        raise ValueError("This snapshot requires Codex CLI " + expected_codex_version())
    return command, detected


def cli_command() -> list[str]:
    return resolve_cli()[0]


def credentials() -> tuple[str, str]:
    from responses_transport import responses_endpoint
    base = os.environ.get("GROUND_URL", "").rstrip("/")
    key = os.environ.get("GROUND_API_KEY", "")
    if not base.startswith("https://") or not key:
        raise ValueError("Set GROUND_URL (HTTPS Responses endpoint) and GROUND_API_KEY")
    return responses_endpoint(base), key


def model_source() -> str:
    value = os.environ.get("ATTRIBUTION_MODEL_SOURCE", "").strip().lower() or "stepwise"
    if value in ("gateway", "stepwise"):
        return "stepwise"
    if value == "codex":
        return "codex"
    raise ValueError("ATTRIBUTION_MODEL_SOURCE must be 'stepwise' (default) or 'codex'")


def codex_config_home() -> Path:
    configured = os.environ.get("ATTRIBUTION_CODEX_HOME") or os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def resolve_model() -> dict:
    """Resolve the recorder upstream without changing the default gateway contract."""
    source = model_source()
    if source == "stepwise":
        endpoint, key = credentials()
        model = os.environ.get("ATTRIBUTION_MODEL") or "deepseek-v4.1-flash"
        return {
            "source": source, "endpoint": endpoint, "key": key,
            "model": model, "provider": None, "reasoning_effort": None,
            "config_home": None,
        }
    if tomllib is None:
        raise ValueError("ATTRIBUTION_MODEL_SOURCE=codex requires tomllib or tomli")
    config_path = codex_config_home() / "config.toml"
    if not config_path.is_file():
        raise ValueError("Codex config.toml not found: " + str(config_path))
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read Codex config.toml: " + str(exc)) from exc
    provider_name = config.get("model_provider")
    providers = config.get("model_providers") or {}
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ValueError("Codex config selects an unknown model provider: " + str(provider_name))
    wire_api = str(provider.get("wire_api") or "responses").lower()
    if wire_api != "responses":
        raise ValueError("ATTRIBUTION_MODEL_SOURCE=codex requires a Responses provider, got " + wire_api)
    base = str(provider.get("base_url") or "").strip()
    env_key = str(provider.get("env_key") or "OPENAI_API_KEY")
    key = os.environ.get(env_key, "")
    if not base:
        raise ValueError("Codex provider requires base_url: " + str(provider_name))
    if not key:
        raise ValueError("Codex provider environment variable is unset: " + env_key)
    model = (os.environ.get("ATTRIBUTION_CODEX_MODEL") or config.get("model") or "").strip()
    if not model:
        raise ValueError("Codex config has no model; set ATTRIBUTION_CODEX_MODEL")
    from responses_transport import responses_endpoint
    reasoning_effort = config.get("model_reasoning_effort")
    return {
        "source": source,
        "endpoint": responses_endpoint(base),
        "key": key,
        "model": model,
        "provider": provider_name,
        "reasoning_effort": reasoning_effort,
        "config_home": str(config_path.parent),
    }


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
    platform_runtime = engine.current_runtime()
    manifest_digest = args.manifest_sha256 or sha256(args.manifest.resolve())
    loaded = load_inputs(engine, args.manifest.resolve(), args.case, manifest_digest)
    command, detected_cli_version = resolve_cli()
    if platform_runtime.requires_runtime_dep():
        runtime_dep = platform_runtime.resolve_runtime_dep(os.environ.get("CODEX_RUNTIME_DEP"))
        os.environ["CODEX_RUNTIME_DEP"] = str(runtime_dep)
    if args.out.resolve().exists():
        raise ValueError("Output already exists; choose a new directory to preserve prior attempts")
    selected_model_source = model_source()
    if args.preflight:
        print(json.dumps({
            "state": "preflight_passed", "case_id": args.case,
            "platform": platform_runtime.name, "submission_helper": platform_runtime.helper_name,
            "cli_version": detected_cli_version, "skill_version": loaded[3]["skill_version"],
            "registered_sources": len(loaded[0]["inventory"]),
            "registered_images": loaded[0]["image_count"],
            "framework_architecture": (loaded[0].get("framework_architecture") or {}).get("mode"),
            "model_source": selected_model_source,
            "paid_calls": 0,
        }, ensure_ascii=False))
        return 0
    if platform_runtime.name not in ("windows", "macos", "linux"):
        raise ValueError("Native execution supports Windows, macOS, and Linux")
    model = resolve_model()
    case, _, skill, skill_manifest, protocol, store = loaded
    status = engine.run_native(
        case, skill, skill_manifest, protocol, store, args.out.resolve(), args.wall,
        command=command, endpoint=model["endpoint"], key=model["key"],
        model=model["model"], model_source=model["source"],
        model_provider=model["provider"], reasoning_effort=model["reasoning_effort"],
    )
    engine.atomic(args.out.resolve() / "input_binding.json", {
        "manifest_sha256": manifest_digest,
        "runtime_manifest_sha256": sha256(REPO / "RELEASE_MANIFEST.json"),
        "skill_manifest_sha256": sha256(ROOT / "skills/v3/manifest.json"),
        "platform": platform_runtime.name,
        "submission_helper": platform_runtime.helper_name,
        "cli_version": detected_cli_version,
        "codex_version_enforced": enforce_codex_version(),
        "prompt_version": engine.PROMPT_VERSION,
        "model_source": model["source"],
        "model": model["model"],
        "model_provider": model["provider"],
        "model_config_home": model["config_home"],
    })
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["state"] == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
