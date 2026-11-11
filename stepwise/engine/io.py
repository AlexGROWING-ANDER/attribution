#!/usr/bin/env python3
"""Cross-platform per-step submission helper for macOS and Linux."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


WORK = Path(__file__).resolve().parent
PHASES = {
    "before_action",
    "after_action",
    "decision",
    "execution",
    "metadata",
    "final_observation",
    "unknown",
}
VERDICTS = {"reasonable", "error", "uncertain"}


class HelperError(ValueError):
    pass


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log_event(mode: str, accepted: bool, detail: str) -> None:
    row = {
        "at": int(time.time() * 1000),
        "mode": mode,
        "accepted": accepted,
        "detail": detail,
    }
    with (WORK / "io_events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def require_fields(value, names) -> None:
    if not isinstance(value, dict):
        raise HelperError("Expected a JSON object")
    actual = set(value)
    expected = set(names)
    missing = expected - actual
    if missing:
        raise HelperError("Missing field " + sorted(missing)[0])
    unexpected = actual - expected
    if unexpected:
        raise HelperError("Unexpected field " + sorted(unexpected)[0])


def references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids":
                if isinstance(child, list):
                    for item in child:
                        yield str(item)
                else:
                    yield str(child)
            else:
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def revision_files(steps: list[int]) -> list[Path]:
    allowed = {f"revision_{step:03d}.json" for step in steps}
    unexpected = [path for path in (WORK / "step_judgments").glob("revision_*.json")
                  if path.is_file() and path.name not in allowed]
    if unexpected:
        raise HelperError("Unexpected revision file: " + unexpected[0].name)
    return sorted((WORK / "step_judgments").glob("revision_*.json"))


def require_evidence_fields(evidence: dict) -> None:
    require_fields(evidence, ("source_id", "phase", "locator", "observation"))


def check_step_value(value: dict, step: int, known: set[str], prior: dict | None = None) -> None:
    require_fields(value, ("step", "verdict", "summary", "evidence"))
    if type(value["step"]) is not int or value["step"] != step:
        raise HelperError(f"Revision step mismatch: {step}" if prior is not None else "step must be an integer")
    if value["verdict"] not in VERDICTS:
        raise HelperError("Invalid verdict")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise HelperError("Empty summary")
    if not isinstance(value["evidence"], list) or not value["evidence"]:
        raise HelperError("Evidence observations required")

    if prior is not None:
        if value["verdict"] != prior["verdict"]:
            raise HelperError(
                f"Step {step} revision cannot change the original verdict; "
                "append observations and explain them in summary/limitations"
            )
        original_evidence = prior["evidence"]
        if len(value["evidence"]) < len(original_evidence):
            raise HelperError(
                f"Step {step} revision removed original evidence; "
                "restore the original prefix and append new observations"
            )
        for index, original in enumerate(original_evidence):
            for field, original_value in original.items():
                if value["evidence"][index].get(field) != original_value:
                    raise HelperError(
                        f"Original evidence E{step:02d}_{index + 1} differs at {field}. "
                        "Restore the original evidence prefix unchanged; append the corrected "
                        "observation as a new item and explain it in summary/limitations."
                    )

    for index, evidence in enumerate(value["evidence"], 1):
        require_evidence_fields(evidence)
        for field in ("source_id", "phase", "locator", "observation"):
            field_value = evidence[field]
            if not isinstance(field_value, str) or not field_value.strip():
                raise HelperError(f"Evidence {index} field {field} must be a nonempty string")
        if evidence["source_id"] not in known:
            raise HelperError("Unknown revision source_id" if prior is not None else "Unknown source_id")
        if evidence["phase"] not in PHASES:
            raise HelperError("Invalid revision phase" if prior is not None else "Invalid phase")


def read_checked_step(step: int, known: set[str]) -> dict:
    original = read_json(WORK / "step_judgments" / f"step_{step:03d}.json")
    revision_path = WORK / "step_judgments" / f"revision_{step:03d}.json"
    if not revision_path.is_file():
        return original
    revised = read_json(revision_path)
    if not isinstance(revised, dict):
        raise HelperError("Expected a JSON object")
    check_step_value(revised, step, known, prior=original)
    return revised


def payload_from_json(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise HelperError(f"Invalid JSON: {exc}") from exc


def read_payload(payload: str | None) -> dict:
    if payload is None or payload == "-":
        if sys.stdin.isatty():
            raise HelperError("Json payload required")
        payload = sys.stdin.read()
    value = payload_from_json(payload)
    if not isinstance(value, dict):
        raise HelperError("Expected a JSON object")
    return value


def available_steps(index: dict) -> list[int]:
    steps = index.get("available_steps")
    if not isinstance(steps, list):
        raise HelperError("available_steps must be a list")
    return sorted(int(step) for step in steps)


def pending_steps(steps: list[int]) -> list[int]:
    return [step for step in steps if not (WORK / "step_judgments" / f"step_{step:03d}.json").is_file()]


def known_sources(index: dict) -> set[str]:
    sources = index.get("sources")
    if not isinstance(sources, list):
        raise HelperError("sources must be a list")
    return {str(source["source_id"]) for source in sources}


def submit_step(index: dict, payload: dict) -> dict:
    steps = available_steps(index)
    pending = pending_steps(steps)
    if not pending:
        raise HelperError("All steps are already saved")
    step = payload.get("step")
    if type(step) is not int:
        raise HelperError("step must be an integer")
    if step != pending[0]:
        raise HelperError(f"Current required step is {pending[0]}")
    check_step_value(payload, step, known_sources(index))
    save_json(WORK / "step_judgments" / f"step_{step:03d}.json", payload)
    ids = [f"E{step:02d}_{index}" for index in range(1, len(payload["evidence"]) + 1)]
    log_event("step", True, f"Saved step {step}")
    return {"saved": step, "evidence_ids": ids}


def require_all_steps_complete(index: dict) -> list[int]:
    steps = available_steps(index)
    pending = pending_steps(steps)
    if pending:
        raise HelperError("Unfinished steps: " + ",".join(str(step) for step in pending))
    revision_files(steps)
    return steps


def submit_global(index: dict, payload: dict) -> dict:
    steps = require_all_steps_complete(index)
    known = known_sources(index)
    for step in steps:
        read_checked_step(step, known)
    schema = read_json(WORK / "global_summary.schema.json")
    required = schema.get("required")
    if not isinstance(required, list):
        raise HelperError("global_summary.schema.json has no required list")
    require_fields(payload, required)
    save_json(WORK / "global_summary.json", payload)
    log_event("global", True, "Saved global semantic judgment")
    return {"saved": "global_summary.json"}


def check_global(index: dict) -> dict:
    steps = require_all_steps_complete(index)
    known = known_sources(index)
    global_summary = read_json(WORK / "global_summary.json")
    ids = []
    for step in steps:
        value = read_checked_step(step, known)
        ids.extend(f"E{step:02d}_{index}" for index in range(1, len(value["evidence"]) + 1))
    bad = sorted({ref for ref in references(global_summary) if ref not in ids})
    if bad:
        raise HelperError("Unknown evidence refs: " + ",".join(bad))
    log_event("check", True, "Local JSON and reference check; final strict validator not invoked")
    return {"ok": True, "steps": len(steps), "evidence_ids": len(ids), "strict_validation": "caller_only"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("step", "global", "check"))
    parser.add_argument("--json", dest="payload", help="JSON payload; omit to read stdin")
    args = parser.parse_args()
    try:
        index = read_json(WORK / "input_index.json")
        if args.mode == "step":
            result = submit_step(index, read_payload(args.payload))
        elif args.mode == "global":
            result = submit_global(index, read_payload(args.payload))
        else:
            result = check_global(index)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        message = str(exc)
        try:
            log_event(args.mode, False, message)
        except Exception:
            pass
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
