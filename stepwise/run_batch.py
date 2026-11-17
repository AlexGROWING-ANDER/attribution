"""Prepare native Flux tasks sequentially, or collect/finalize existing attempts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
from harness.storage import atomic_json
from run_case import validate_case_id, verify_release, sha256

FINAL_STATES = frozenset({
    "validated", "invalid", "runtime_failed", "infra_failed", "interrupted",
    "unresolved_after_retries",
})
TERMINAL_STATES = frozenset({"validated", "interrupted", "unresolved_after_retries"})
RETRYABLE_STATES = frozenset({"invalid", "runtime_failed", "infra_failed"})
FAILURE_STATES = FINAL_STATES - {"validated"}
KNOWN_STATES = FINAL_STATES | {"ready_for_flux", "pending"}
BINDING_KEYS = ("manifest_sha256", "runtime_manifest_sha256", "skill_manifest_sha256")
ATTEMPT_PATTERN = re.compile(r"attempt_(\d{2,})")


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


def batch_identity(manifest_path: Path, scope: list[str], wall: int,
                   max_attempts: int = 3) -> dict:
    return {
        "manifest_sha256": sha256(manifest_path), "cases": scope, "wall_seconds": wall,
        "max_attempts": max_attempts,
        "runtime_manifest_sha256": sha256(ROOT.parent / "RELEASE_MANIFEST.json"),
        "skill_manifest_sha256": sha256(ROOT / "skills/v3/manifest.json"),
    }


def attempt_paths(case_root: Path) -> list[Path]:
    if not case_root.is_dir():
        return []
    paths = []
    for path in case_root.iterdir():
        match = ATTEMPT_PATTERN.fullmatch(path.name)
        if match and path.is_dir():
            paths.append((int(match.group(1)), path))
    return [path for _, path in sorted(paths)]


def attempt_index(path: Path) -> int:
    match = ATTEMPT_PATTERN.fullmatch(path.name)
    return int(match.group(1)) if match else 0


def is_terminal(record: dict) -> bool:
    return record.get("state") in TERMINAL_STATES


def write_launch_failure(directory: Path, case_id: str, message: str,
                         returncode: int | None = None) -> None:
    """Persist a pre-binding launch failure so it consumes this attempt's budget."""
    if (directory / "input_binding.json").is_file():
        return
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "launch_failure.json", {
        "case_id": case_id,
        "state": "runtime_failed",
        "error": message,
        "returncode": returncode,
        "attempt_index": attempt_index(directory),
        "preserved_existing": True,
    })


def existing_record(directory: Path, case: dict, identity: dict,
                    index: int | None = None) -> dict | None:
    """Read durable state without modifying an attempt or trusting a launcher exit."""
    case_id = case["case_id"]
    index = index or attempt_index(directory) or 1
    if not directory.exists():
        return None

    def failed(message: str, *, retryable: bool = True) -> dict:
        return {"case_id": case_id, "state": "runtime_failed", "error": message,
                "attempt_index": index, "retryable": retryable,
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
        context_sha = binding.get("retry_context_sha256")
        if context_sha:
            context_path = directory / "retry_context.json"
            if not context_path.is_file() or sha256(context_path) != context_sha:
                return failed("Existing retry context changed; retained for inspection")
    else:
        # A process may have stopped during preparation. Keep this attempt intact
        # and let the bounded retry state machine create the next attempt.
        message = "Existing attempt has no input binding; preparation was incomplete"
        try:
            launch_failure = read_json(directory / "launch_failure.json")
        except (ValueError, OSError):
            launch_failure = {}
        if isinstance(launch_failure, dict) and isinstance(launch_failure.get("error"), str):
            message = launch_failure["error"]
        return failed(message, retryable=True)

    status = directory / "status.json"
    if not status.is_file():
        return {"case_id": case_id,
                "state": "ready_for_flux" if (directory / "flux_task.md").is_file() else "pending",
                "attempt_index": index, "preserved_existing": True}
    try:
        record = read_json(status)
    except (ValueError, OSError):
        return failed("Existing status is unreadable; retained for inspection")
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        return failed("Existing status has no valid state")
    if record["state"] not in KNOWN_STATES:
        return failed("Existing status has an unknown state; retained for inspection")
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
        return failed("Prepared status has no task prompt; retained without overwrite",
                      retryable=True)
    return {**record, "case_id": case_id, "attempt_index": index,
            "retryable": record["state"] in RETRYABLE_STATES,
            "preserved_existing": True}


def submissions_complete(directory: Path, case: dict) -> bool:
    """Only gate on submitted files; run_case owns receipts and semantic validation."""
    workspace = directory / "workspace"
    return (
        (workspace / "checkpoint_plan.json").is_file()
        and (workspace / "global_summary.json").is_file()
        and all((workspace / "step_judgments" / f"step_{step:03d}.json").is_file()
                for step in case.get("available_steps", []))
    )


def _error_lines(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_error_lines(item))
        return result
    if isinstance(value, dict):
        path = value.get("path")
        message = value.get("message")
        if isinstance(message, str):
            return [f"{path}: {message}" if path else message]
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    return [str(value)]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def repair_hints(errors: list[str]) -> list[str]:
    text = "\n".join(errors).casefold()
    hints = []
    if "undelivered evidence source" in text or "undelivered cited images" in text:
        hints.append("只引用已登记送达的来源；重新 read 该来源并确认 ticket，不要把仅列出的路径当作已送达。")
    if "gallery images not delivered" in text:
        hints.append("逐张查看全部图库原图并确认 image ticket；派生视图不能替代原图覆盖。")
    if ("additional properties are not allowed" in text
            or "unexpected field" in text):
        hints.append("global_summary 只写 global_summary.schema.json 允许的字段；删除额外字段，evidence ID 只能放在对应 evidence_ids 字段。")
    if "illegal hierarchy path" in text:
        hints.append("l1/l2/l3 必须来自当前框架 taxonomy 的合法路径，不要使用语义相近但未注册的标签。")
    if "actor/stage conflicts" in text:
        hints.append("按框架源码重新核对 candidate 的 stage 与 actor 责任边界，不要按直觉分配模块。")
    if ("contamination" in text or "affected baseline" in text
            or "targeted reset" in text):
        hints.append("contamination 与 decision 必须一致；确认污染时，给出 targeted reset 要求，并将受影响 baseline 设为 rerun/pending/precondition_blocked。")
    if ("lacks complete runtime" in text or "complete review lacks" in text
            or "claimed image review absent" in text):
        hints.append("coverage 中的 scan_complete、screenshots_viewed 和 log_steps_read 必须与实际完整送达记录一致；未完整读取就不要声明完成。")
    if "required checkpoint" in text or "checkpoint" in text:
        hints.append("最终 goals 必须逐字保留 required checkpoint 的 id/object/requirement。")
    if not hints:
        hints.append("针对上一轮列出的每个错误重新核对原证据与 Schema；不要只改措辞或复制上一轮判断。")
    return hints


def build_retry_context(case_id: str, previous: dict, next_index: int,
                        max_attempts: int) -> dict:
    directory = Path(previous["attempt_path"])
    try:
        status = read_json(directory / "status.json")
    except (ValueError, OSError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    errors = _dedupe(_error_lines(status.get("error")) + _error_lines(previous.get("error")))
    schema_errors = _dedupe(_error_lines(status.get("schema_errors")))
    validation_errors = _dedupe(_error_lines(status.get("validation_errors")))
    all_errors = _dedupe(errors + schema_errors + validation_errors)
    if not all_errors:
        all_errors = ["Previous attempt did not reach validated state: "
                      + str(status.get("state") or previous.get("state"))]
    return {
        "schema": "flux-retry-context-v1",
        "case_id": case_id,
        "previous_attempt": directory.name,
        "previous_state": status.get("state") or previous.get("last_state") or previous.get("state"),
        "attempt_index": next_index,
        "max_attempts": max_attempts,
        "must_reanalyze_from_original_evidence": True,
        "do_not_read_previous_attempt": True,
        "errors": all_errors,
        "schema_errors": schema_errors,
        "validation_errors": validation_errors,
        "repair_hints": repair_hints(all_errors),
        "instructions": [
            "从本 attempt 的原始材料和冻结 Skill 重新完成全部分析与提交，不读取上一 attempt 的 result.json 或历史预测。",
            "先按 checkpoint 协议提交计划，再逐 source 读取并确认送达，再按 step 顺序提交判断。",
            "提交 global_summary 后运行 io.py check；被拒时立即按错误修正并复查，直到本地严格检查通过。",
            "不要复制错误字段、旧标签或旧结论；本文件只提供上一轮的校验错误摘要。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--jobs", type=int, choices=range(1, 6), default=1,
                        help="Maximum native Flux agent concurrency hint; preparation is sequential")
    parser.add_argument("--wall", type=int, default=3600)
    parser.add_argument("--max-attempts", type=int, choices=range(1, 11), default=3,
                        help="Total fresh attempts per case, including the first attempt (default: 3)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-on-invalid", action="store_true",
                        help="Stop new cases only after retry budget is exhausted")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collect", action="store_true", help="Read existing statuses without launching cases")
    mode.add_argument("--finalize", action="store_true",
                      help="Finalize complete submissions and prepare bounded retries on failure")
    args = parser.parse_args()
    if args.wall <= 0:
        parser.error("--wall must be positive")
    verify_release()
    manifest_path, output = args.manifest.resolve(), args.out.resolve()
    manifest_payload = manifest_path.read_bytes()
    manifest = json.loads(manifest_payload.decode("utf-8-sig"))
    scope = selected_cases(manifest, args.cases)
    case_map = {case["case_id"]: case for case in manifest["cases"]}
    identity = batch_identity(manifest_path, scope, args.wall, args.max_attempts)
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

    def case_root(case_id: str) -> Path:
        directory = (output / "runs" / case_id).resolve()
        if not directory.is_relative_to(output):
            raise ValueError("Case directory escapes the batch output")
        return directory

    def latest_directory(case_id: str) -> Path | None:
        paths = attempt_paths(case_root(case_id))
        return paths[-1] if paths else None

    def next_directory(case_id: str) -> Path:
        paths = attempt_paths(case_root(case_id))
        number = (attempt_index(paths[-1]) if paths else 0) + 1
        directory = case_root(case_id) / f"attempt_{number:02d}"
        if not directory.resolve().is_relative_to(output):
            raise ValueError("Attempt directory escapes the batch output")
        return directory

    def case_record(case_id: str) -> dict:
        directory = latest_directory(case_id)
        if directory is None:
            return {"case_id": case_id, "state": "not_dispatched", "attempts_used": 0}
        index = attempt_index(directory)
        record = existing_record(directory, case_map[case_id], identity, index)
        if record is None:
            return {"case_id": case_id, "state": "not_dispatched", "attempts_used": 0}
        record.update(attempt=directory.name, attempt_path=str(directory),
                      attempt_index=index, attempts_used=index)
        if record["state"] == "pending":
            record = {**record, "state": "runtime_failed", "last_state": "pending",
                      "error": record.get("error") or "Attempt preparation did not finish",
                      "retryable": True}
        if record["state"] in RETRYABLE_STATES and record.get("retryable", True):
            if index >= args.max_attempts:
                record.update(state="unresolved_after_retries",
                              last_state=record.get("last_state") or record.get("state"),
                              retry_available=False, retry_exhausted=True,
                              attempts_remaining=0)
            else:
                record.update(retry_available=True,
                              attempts_remaining=args.max_attempts - index)
        return record

    def launch(case_id: str, directory: Path, *, finalize: bool = False,
               retry_context: Path | None = None) -> dict:
        directory.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT / "run_case.py"), "--manifest", str(manifest_path),
                   "--manifest-sha256", identity["manifest_sha256"],
                   "--release-sha256", identity["runtime_manifest_sha256"],
                   "--skill-manifest-sha256", identity["skill_manifest_sha256"],
                   "--case", case_id, "--out", str(directory), "--wall", str(args.wall)]
        if retry_context is not None:
            command.extend(["--retry-context", str(retry_context)])
        if finalize:
            command.append("--finalize")
        # run_case is local preparation/validation only. No agent/model launcher here.
        completed = None
        failure_message = None
        with (directory.parent / "launcher.log").open("ab") as log:
            try:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=args.wall + 180, check=False)
            except subprocess.TimeoutExpired:
                failure_message = "Launcher exceeded wall deadline"
            except OSError as exc:
                failure_message = f"Launcher could not start: {exc}"
        record = existing_record(directory, case_map[case_id], identity, attempt_index(directory))
        if record is not None and record["state"] == "validated":
            return {**record, "preserved_existing": True}
        if record is not None and record["state"] in FAILURE_STATES:
            return {**record, "preserved_existing": True}
        pending_finalize = (finalize and record is not None
                            and record["state"] in {"ready_for_flux", "pending"})
        if failure_message is None and completed.returncode:
            failure_message = "Launcher failed; inspect launcher.log"
        if failure_message is not None and not pending_finalize:
            write_launch_failure(directory, case_id, failure_message,
                                 None if completed is None else completed.returncode)
            record = existing_record(directory, case_map[case_id], identity,
                                     attempt_index(directory))
        if record is None:
            write_launch_failure(
                directory, case_id,
                failure_message or "Launcher produced no bound attempt; inspect launcher.log",
                None if completed is None else completed.returncode,
            )
            record = existing_record(directory, case_map[case_id], identity,
                                     attempt_index(directory))
        if record is None:
            return {"case_id": case_id, "state": "runtime_failed", "retryable": True,
                    "returncode": None if completed is None else completed.returncode,
                    "error": failure_message or "Launcher produced no bound attempt; inspect launcher.log",
                    "launch_failed": True}
        if failure_message is not None and record["state"] not in FAILURE_STATES:
            return {**record, "state": "runtime_failed", "last_state": record["state"],
                    "retryable": True, "launch_failed": True,
                    "returncode": None if completed is None else completed.returncode,
                    "error": failure_message}
        # A zero exit from --finalize cannot turn ready/pending into validated.
        return {**record, "preserved_existing": True}

    def reconcile_launch(case_id: str, outcome: dict) -> dict:
        """Persist a launcher failure as a retry even if preparation left a prompt."""
        record = case_record(case_id)
        if (outcome.get("launch_failed") and not is_terminal(record)
                and record["state"] not in RETRYABLE_STATES):
            record = retryable_failure(
                record, outcome.get("error") or "Launcher failed before producing a usable attempt")
        return record

    def prepare_retry(case_id: str, previous: dict) -> dict:
        directory = next_directory(case_id)
        if attempt_index(directory) > args.max_attempts:
            return {**previous, "state": "unresolved_after_retries",
                    "last_state": previous.get("last_state") or previous.get("state"),
                    "retry_available": False, "retry_exhausted": True}
        brief = build_retry_context(case_id, previous, attempt_index(directory), args.max_attempts)
        brief_path = output / "retry_briefs" / case_id / f"{directory.name}.json"
        atomic_json(brief_path, brief)
        outcome = launch(case_id, directory, finalize=False, retry_context=brief_path)
        return reconcile_launch(case_id, outcome)

    def retryable_failure(record: dict, message: str) -> dict:
        """Turn a protocol-level readiness failure into one bounded retry."""
        used = record.get("attempts_used", record.get("attempt_index", 0))
        if used >= args.max_attempts:
            return {**record, "state": "unresolved_after_retries",
                    "last_state": record.get("state"),
                    "error": message, "retry_available": False,
                    "retry_exhausted": True, "attempts_remaining": 0}
        return {**record, "state": "invalid", "last_state": record.get("state"),
                "error": message, "retryable": True, "retry_available": True,
                "attempts_remaining": args.max_attempts - used}

    def save() -> None:
        states = {}
        for record in results.values():
            state = record["state"]
            states[state] = states.get(state, 0) + 1
        all_finished = len(results) == len(scope) and all(
            is_terminal(record) for record in results.values())
        prepared = (not args.collect and not args.finalize and len(results) == len(scope)
                    and all(is_terminal(record) or record["state"] == "ready_for_flux"
                            for record in results.values()))
        awaiting_retry = any(record.get("retry_available") for record in results.values())
        if all_finished:
            state = ("completed" if all(record["state"] == "validated" for record in results.values())
                     else "completed_with_unresolved")
        elif awaiting_retry:
            state = "awaiting_retry"
        elif prepared:
            state = "prepared"
        else:
            state = "awaiting_flux"
        atomic_json(output / "batch_state.json", {
            "state": state,
            "updated_at": time.time(), "jobs": args.jobs, "selected": len(scope),
            "finished_records": sum(is_terminal(record) for record in results.values()),
            "retry_available": sum(bool(record.get("retry_available")) for record in results.values()),
            "states": states, "results": results,
            "max_attempts_per_case": args.max_attempts,
            "independent_attempts_per_case": args.max_attempts,
            "post_validator_repair": True,
            "repair_mode": "fresh_attempt_with_validation_feedback",
        })
        tasks = []
        for case_id in scope:
            record = results.get(case_id, {})
            directory = record.get("attempt_path")
            tasks.append({
                "case_id": case_id,
                "out": directory,
                "task_path": str(Path(directory) / "flux_task.md") if directory else None,
                "state": record.get("state", "not_dispatched"),
                "attempt_index": record.get("attempt_index"),
                "retry_of": record.get("retry_of"),
                "retry_required": bool(record.get("retry_available")),
            })
        atomic_json(output / "dispatch.json", {
            "state": state, "jobs": args.jobs, "batch_identity": identity,
            "max_attempts": args.max_attempts,
            "retry_policy": "fresh_attempt_with_validation_feedback",
            "tasks": tasks,
        })

    try:
        for case_id in scope:
            results[case_id] = case_record(case_id)
        stopped = args.stop_on_invalid and any(
            is_terminal(record) and record["state"] != "validated"
            for record in results.values())
        save()
        for case_id in scope:
            record = results[case_id]
            directory = latest_directory(case_id)
            if args.collect or stopped or (not args.finalize and (output / "PAUSE").exists()):
                continue
            if (args.finalize and record["state"] in {"ready_for_flux", "pending"}
                    and directory is not None):
                if (not (directory / "input_binding.json").is_file()
                        or not (directory / "flux_task.md").is_file()
                        or not submissions_complete(directory, case_map[case_id])):
                    results[case_id] = retryable_failure(
                        record, "Finalize was requested before all required submissions were complete")
                else:
                    try:
                        outcome = launch(case_id, directory, finalize=True)
                    except (OSError, ValueError) as exc:
                        results[case_id] = retryable_failure(record, str(exc))
                    else:
                        results[case_id] = reconcile_launch(case_id, outcome)
                        if results[case_id]["state"] in {"ready_for_flux", "pending"}:
                            results[case_id] = retryable_failure(
                                results[case_id],
                                "Finalize found no accepted local check; child must run io check until it passes")
                record = results[case_id]
            if record["state"] in RETRYABLE_STATES and record.get("retry_available"):
                try:
                    results[case_id] = prepare_retry(case_id, record)
                except (OSError, ValueError) as exc:
                    results[case_id] = {**record, "state": "runtime_failed",
                                        "retryable": True, "error": str(exc)}
            elif not args.finalize and record["state"] == "not_dispatched":
                try:
                    outcome = launch(case_id, next_directory(case_id), finalize=False)
                except (OSError, ValueError) as exc:
                    results[case_id] = {"case_id": case_id, "state": "runtime_failed",
                                        "retryable": True, "error": str(exc)}
                else:
                    results[case_id] = reconcile_launch(case_id, outcome)
                    if results[case_id]["state"] == "not_dispatched":
                        results[case_id] = retryable_failure(
                            results[case_id], "Launcher produced no bound attempt")
            print(json.dumps({"case_id": case_id, "state": results[case_id]["state"],
                              "attempt": results[case_id].get("attempt"),
                              "attempts_used": results[case_id].get("attempts_used")}), flush=True)
            if (args.stop_on_invalid and is_terminal(results[case_id])
                    and results[case_id]["state"] != "validated"):
                stopped = True
            save()
        save()
    finally:
        lock.unlink(missing_ok=True)
    if args.collect:
        successful = all(record["state"] == "validated" for record in results.values())
    elif args.finalize:
        successful = all(record["state"] == "validated" for record in results.values())
    else:
        successful = all(record["state"] in {"validated", "ready_for_flux"}
                         for record in results.values())
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
