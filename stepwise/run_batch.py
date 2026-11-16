"""Prepare native Flux tasks sequentially, or collect/finalize existing attempts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
from harness.storage import atomic_json
from run_case import validate_case_id, verify_release, sha256

FINAL_STATES = frozenset({"validated", "invalid", "runtime_failed", "infra_failed", "interrupted"})
FAILURE_STATES = FINAL_STATES - {"validated"}
BINDING_KEYS = ("manifest_sha256", "runtime_manifest_sha256", "skill_manifest_sha256")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def selected_cases(manifest: dict, requested: list[str] | None) -> list[str]:
    available = [case["case_id"] for case in manifest["cases"]]
    for case_id in available:
        validate_case_id(case_id)
    if len({case_id.casefold() for case_id in available}) != len(available):
        raise ValueError("Duplicate case IDs in manifest")
    selected = requested if requested is not None else available
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(available):
        raise ValueError("Case selection must contain unique registered IDs")
    return selected


def batch_identity(manifest_path: Path, scope: list[str], wall: int) -> dict:
    return {
        "manifest_sha256": sha256(manifest_path), "cases": scope, "wall_seconds": wall,
        "runtime_manifest_sha256": sha256(ROOT.parent / "RELEASE_MANIFEST.json"),
        "skill_manifest_sha256": sha256(ROOT / "skills/v3/manifest.json"),
    }


def existing_record(directory: Path, case: dict, identity: dict) -> dict | None:
    """Read durable state without modifying an attempt or trusting a launcher exit."""
    case_id = case["case_id"]
    if not directory.exists():
        return None

    def failed(message: str) -> dict:
        return {"case_id": case_id, "state": "runtime_failed", "error": message,
                "preserved_existing": True}

    binding_path = directory / "input_binding.json"
    if binding_path.is_file():
        try:
            binding = read_json(binding_path)
        except (ValueError, OSError):
            return failed("Existing input binding is unreadable; retained without overwrite")
        if (not isinstance(binding, dict)
                or any(binding.get(key) != identity[key] for key in BINDING_KEYS)
                or binding.get("case_id", case_id) != case_id):
            return failed("Existing input binding differs from this batch")
    else:
        # Partial preparation is retained, not retried in the same attempt directory.
        return {"case_id": case_id, "state": "pending", "preserved_existing": True,
                "error": "Existing attempt has no input binding; retained without overwrite"}

    status = directory / "status.json"
    if not status.is_file():
        return {"case_id": case_id,
                "state": "ready_for_flux" if (directory / "flux_task.md").is_file() else "pending",
                "preserved_existing": True}
    try:
        record = read_json(status)
    except (ValueError, OSError):
        return failed("Existing status is unreadable; retained for inspection")
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        return failed("Existing status has no valid state")
    if record.get("case_id", case_id) != case_id:
        return failed("Existing status belongs to another case")
    if record["state"] == "validated":
        try:
            result = read_json(directory / "result.json")
        except (ValueError, OSError):
            return failed("Validated status has no readable result.json")
        if not isinstance(result, dict) or not isinstance(result.get("case"), dict):
            return failed("Existing result has no case identity")
        actual, expected = result["case"], case.get("identity")
        if actual.get("id") != case_id or (expected is not None and actual != expected):
            return failed("Existing result identity differs from the selected attempt")
    if record["state"] == "ready_for_flux" and not (directory / "flux_task.md").is_file():
        return {"case_id": case_id, "state": "pending", "preserved_existing": True,
                "error": "Prepared status has no task prompt; retained without overwrite"}
    return {**record, "case_id": case_id, "preserved_existing": True}


def submissions_complete(directory: Path, case: dict) -> bool:
    """Only gate on submitted files; run_case owns receipts and semantic validation."""
    workspace = directory / "workspace"
    return (
        (workspace / "checkpoint_plan.json").is_file()
        and (workspace / "global_summary.json").is_file()
        and all((workspace / "step_judgments" / f"step_{step:03d}.json").is_file()
                for step in case.get("available_steps", []))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--jobs", type=int, choices=range(1, 6), default=1,
                        help="Maximum native Flux agent concurrency hint; preparation is sequential")
    parser.add_argument("--wall", type=int, default=3600)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-on-invalid", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collect", action="store_true", help="Read existing statuses without launching cases")
    mode.add_argument("--finalize", action="store_true", help="Finalize only complete existing submissions")
    args = parser.parse_args()
    if args.wall <= 0:
        parser.error("--wall must be positive")
    verify_release()
    manifest_path, output = args.manifest.resolve(), args.out.resolve()
    manifest_payload = manifest_path.read_bytes()
    manifest = json.loads(manifest_payload.decode("utf-8-sig"))
    scope = selected_cases(manifest, args.cases)
    case_map = {case["case_id"]: case for case in manifest["cases"]}
    identity = batch_identity(manifest_path, scope, args.wall)
    if identity["manifest_sha256"] != hashlib.sha256(manifest_payload).hexdigest():
        raise ValueError("Input manifest changed during batch initialization")
    if output.exists():
        if (not (args.resume or args.collect or args.finalize)
                or read_json(output / "batch_identity.json") != identity):
            raise ValueError("Use a new output directory, or --resume with the identical input scope")
    else:
        if args.collect or args.finalize:
            raise ValueError("Collection and finalization require an existing bound batch")
        output.mkdir(parents=True)
        atomic_json(output / "batch_identity.json", identity)
    # One process owns a batch. An abandoned lock requires checking the old PID first.
    lock = output / "batch.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    results = {}

    def attempt_directory(case_id: str) -> Path:
        directory = output / "runs" / case_id / "attempt_01"
        if not directory.resolve().is_relative_to(output):
            raise ValueError("Attempt directory escapes the batch output")
        return directory

    def launch(case_id: str, *, finalize: bool = False) -> dict:
        directory = attempt_directory(case_id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT / "run_case.py"), "--manifest", str(manifest_path),
                   "--manifest-sha256", identity["manifest_sha256"],
                   "--release-sha256", identity["runtime_manifest_sha256"],
                   "--skill-manifest-sha256", identity["skill_manifest_sha256"],
                   "--case", case_id, "--out", str(directory), "--wall", str(args.wall)]
        if finalize:
            command.append("--finalize")
        # run_case is local preparation/validation only. No agent/model launcher here.
        with (directory.parent / "launcher.log").open("ab") as log:
            try:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=args.wall + 180, check=False)
            except subprocess.TimeoutExpired:
                return {"case_id": case_id, "state": "runtime_failed", "error": "Launcher exceeded wall deadline"}
        record = existing_record(directory, case_map[case_id], identity)
        pending_finalize = finalize and record is not None and record["state"] in {"ready_for_flux", "pending"}
        if record is None or (completed.returncode and record["state"] not in FAILURE_STATES
                              and not pending_finalize):
            return {"case_id": case_id, "state": "runtime_failed", "returncode": completed.returncode,
                    "error": "Launcher failed or produced no bound attempt; inspect launcher.log"}
        # A zero exit from --finalize cannot turn ready/pending into validated.
        return {**record, "preserved_existing": finalize}

    def save() -> None:
        states = {}
        for record in results.values():
            state = record["state"]
            states[state] = states.get(state, 0) + 1
        all_finished = len(results) == len(scope) and all(
            record["state"] in FINAL_STATES for record in results.values())
        prepared = (not args.collect and not args.finalize and len(results) == len(scope)
                    and all(record["state"] in FINAL_STATES | {"ready_for_flux"}
                            for record in results.values()))
        state = "completed" if all_finished else "prepared" if prepared else "awaiting_flux"
        atomic_json(output / "batch_state.json", {
            "state": state,
            "updated_at": time.time(), "jobs": args.jobs, "selected": len(scope),
            "finished_records": sum(record["state"] in FINAL_STATES for record in results.values()),
            "states": states, "results": results,
            "independent_attempts_per_case": 1, "post_validator_repair": False,
        })
        atomic_json(output / "dispatch.json", {
            "state": state, "jobs": args.jobs, "batch_identity": identity,
            "tasks": [{"case_id": case_id, "out": str(attempt_directory(case_id)),
                       "task_path": str(attempt_directory(case_id) / "flux_task.md"),
                       "state": results.get(case_id, {}).get("state", "not_dispatched")}
                      for case_id in scope],
        })

    try:
        for case_id in scope:
            prior = existing_record(attempt_directory(case_id), case_map[case_id], identity)
            results[case_id] = prior or {"case_id": case_id, "state": "not_dispatched"}
        stopped = args.stop_on_invalid and any(
            record["state"] in FAILURE_STATES for record in results.values())
        save()
        for case_id in scope:
            directory = attempt_directory(case_id)
            if args.collect or stopped or (not args.finalize and (output / "PAUSE").exists()):
                continue
            record = results[case_id]
            if args.finalize:
                if (record["state"] not in {"ready_for_flux", "pending"}
                        or not (directory / "input_binding.json").is_file()
                        or not (directory / "flux_task.md").is_file()
                        or not submissions_complete(directory, case_map[case_id])):
                    continue
            elif directory.exists():
                continue  # --resume never prepares over any existing attempt.
            try:
                results[case_id] = launch(case_id, finalize=args.finalize)
            except (OSError, ValueError) as exc:
                results[case_id] = {"case_id": case_id, "state": "runtime_failed", "error": str(exc)}
            print(json.dumps({"case_id": case_id, "state": results[case_id]["state"]}), flush=True)
            if args.stop_on_invalid and results[case_id]["state"] in FAILURE_STATES:
                stopped = True
            save()
        save()
    finally:
        lock.unlink(missing_ok=True)
    successful = {"validated"} if args.collect or args.finalize else {"validated", "ready_for_flux"}
    return 0 if all(record["state"] in successful for record in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
