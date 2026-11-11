"""Bounded parallel dispatch; one fresh attempt per case, no model repair loop."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
from harness.storage import atomic_json
import platform_runtime
from run_case import validate_case_id, verify_release, sha256


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--jobs", type=int, choices=range(1, 17), default=8)
    parser.add_argument("--wall", type=int, default=3600)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-on-invalid", action="store_true")
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
        if not args.resume or read_json(output / "batch_identity.json") != identity:
            raise ValueError("Use a new output directory, or --resume with the identical input scope")
    else:
        output.mkdir(parents=True)
        atomic_json(output / "batch_identity.json", identity)
    # One process owns a batch. An abandoned lock requires checking the old PID first.
    lock = output / "batch.lock"
    with lock.open("x", encoding="utf-8") as handle:
        import os
        handle.write(str(os.getpid()))
    stopped = threading.Event()
    results = {}
    runtime = platform_runtime.current_runtime()

    def existing_record(case_id: str, preserved: bool = True) -> dict | None:
        directory = output / "runs" / case_id / "attempt_01"
        if not directory.exists():
            return None
        status = directory / "status.json"
        if not status.is_file():
            return {"case_id": case_id, "state": "interrupted", "error": "Existing attempt has no final status; retained without overwrite"}
        try:
            record = read_json(status)
        except (ValueError, OSError):
            return {"case_id": case_id, "state": "runtime_failed", "error": "Existing status is unreadable; retained for inspection"}
        if not isinstance(record, dict) or not isinstance(record.get("state"), str):
            return {"case_id": case_id, "state": "runtime_failed", "error": "Existing status has no valid state"}
        if record.get("case_id", case_id) != case_id:
            return {"case_id": case_id, "state": "runtime_failed", "error": "Existing status belongs to another case"}
        if record.get("state") == "validated":
            result_path = directory / "result.json"
            if not result_path.is_file():
                return {"case_id": case_id, "state": "runtime_failed", "error": "Validated status has no result.json"}
            try:
                result = read_json(result_path)
            except (ValueError, OSError):
                return {"case_id": case_id, "state": "runtime_failed", "error": "Existing result is unreadable; retained for inspection"}
            if not isinstance(result, dict) or not isinstance(result.get("case"), dict):
                return {"case_id": case_id, "state": "runtime_failed", "error": "Existing result has no case identity"}
            expected = case_map[case_id].get("identity")
            actual = result.get("case", {})
            if actual.get("id") != case_id or (expected is not None and actual != expected):
                return {"case_id": case_id, "state": "runtime_failed", "error": "Existing result identity differs from the selected attempt"}
        return {**record, "case_id": case_id, "preserved_existing": preserved}

    def run_one(case_id: str) -> dict:
        directory = output / "runs" / case_id / "attempt_01"
        prior = existing_record(case_id)
        if prior is not None:
            return prior
        if stopped.is_set() or (output / "PAUSE").exists():
            return {"case_id": case_id, "state": "not_dispatched"}
        directory.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT / "run_case.py"), "--manifest", str(manifest_path),
                   "--manifest-sha256", identity["manifest_sha256"],
                   "--release-sha256", identity["runtime_manifest_sha256"],
                   "--skill-manifest-sha256", identity["skill_manifest_sha256"],
                   "--case", case_id, "--out", str(directory), "--wall", str(args.wall)]
        if args.env_file:
            command += ["--env-file", str(args.env_file.resolve())]
        # Stream logs to disk rather than keeping all concurrent output in RAM.
        with (directory.parent / "launcher.log").open("ab") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT,
                **runtime.popen_kwargs(),
            )
            try:
                returncode = process.wait(timeout=args.wall + 180)
            except subprocess.TimeoutExpired:
                runtime.kill_process_tree(process, timeout=30)
                process.wait(timeout=30)
                return {"case_id": case_id, "state": "runtime_failed", "error": "Launcher exceeded wall deadline"}
        status_path = directory / "status.json"
        if status_path.is_file():
            return existing_record(case_id, preserved=False)
        return {"case_id": case_id, "state": "runtime_failed", "returncode": returncode,
                "error": "No final status; inspect this case's launcher.log"}

    def save() -> None:
        states = {}
        for record in results.values():
            state = record["state"]
            states[state] = states.get(state, 0) + 1
        atomic_json(output / "batch_state.json", {
            "updated_at": time.time(), "jobs": args.jobs, "selected": len(scope),
            "finished_records": len(results), "states": states, "results": results,
            "independent_attempts_per_case": 1, "post_validator_repair": False,
        })

    try:
        for case_id in scope:
            prior = existing_record(case_id)
            if prior is not None:
                results[case_id] = prior
        if args.stop_on_invalid and any(record["state"] != "validated" for record in results.values()):
            stopped.set()
        save()
        iterator = iter(case_id for case_id in scope if case_id not in results)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            active = {}
            for _ in range(args.jobs):
                if stopped.is_set() or (output / "PAUSE").exists():
                    break
                case_id = next(iterator, None)
                if case_id is None:
                    break
                active[pool.submit(run_one, case_id)] = case_id
            while active:
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    case_id = active.pop(future)
                    try:
                        results[case_id] = future.result()
                    except Exception as exc:
                        results[case_id] = {"case_id": case_id, "state": "runtime_failed", "error": str(exc)}
                    print(json.dumps({"case_id": case_id, "state": results[case_id]["state"]}), flush=True)
                    if args.stop_on_invalid and results[case_id]["state"] != "validated":
                        stopped.set()
                    save()
                for _ in finished:
                    if stopped.is_set() or (output / "PAUSE").exists():
                        continue
                    case_id = next(iterator, None)
                    if case_id is not None:
                        active[pool.submit(run_one, case_id)] = case_id
        # Remaining scope stays explicit instead of being silently omitted.
        for case_id in scope:
            results.setdefault(case_id, {"case_id": case_id, "state": "not_dispatched"})
        save()
    finally:
        lock.unlink(missing_ok=True)
    return 0 if all(record["state"] == "validated" for record in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
