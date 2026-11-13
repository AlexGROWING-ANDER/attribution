#!/usr/bin/env python3
"""Cross-platform per-step submission helper for macOS and Linux."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image


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
DERIVED_ROOT = "derived_views"
DERIVED_SCHEMA = "stepwise-derived-view-v1"
DERIVED_MIN_SCALE = 1.0
DERIVED_MAX_SCALE = 4.0
DERIVED_MAX_SIDE = 8192
DERIVED_MAX_PIXELS = 30_000_000
OBSERVABLE_KINDS = {"screenshot", "execution_result"}
SOURCE_EVIDENCE_KINDS = {
    "image": "screenshot",
    "log": "module_output",
    "metadata": "metadata",
}


class HelperError(ValueError):
    pass


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace_path(value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise HelperError(f"{label} must be relative to the workspace")
    resolved = (WORK / candidate).resolve()
    if not resolved.is_relative_to(WORK.resolve()):
        raise HelperError(f"{label} escapes the workspace")
    return resolved


def parse_box(value: str) -> tuple[int, int, int, int]:
    parts = value.split(",")
    if len(parts) != 4:
        raise HelperError("--box must be x0,y0,x1,y1")
    try:
        box = tuple(int(part.strip()) for part in parts)
    except ValueError as exc:
        raise HelperError("--box coordinates must be integers") from exc
    return box


def validate_scale(value: float) -> float:
    if not DERIVED_MIN_SCALE <= value <= DERIVED_MAX_SCALE:
        raise HelperError(f"--scale must be between {DERIVED_MIN_SCALE:g} and {DERIVED_MAX_SCALE:g}")
    return value


def derived_output_size(size: tuple[int, int], box: tuple[int, int, int, int], scale: float) -> tuple[int, int]:
    width = box[2] - box[0]
    height = box[3] - box[1]
    output = (max(1, int(width * scale + 0.5)), max(1, int(height * scale + 0.5)))
    if output[0] > DERIVED_MAX_SIDE or output[1] > DERIVED_MAX_SIDE:
        raise HelperError(f"Derived view exceeds the {DERIVED_MAX_SIDE}px side limit")
    if output[0] * output[1] > DERIVED_MAX_PIXELS:
        raise HelperError("Derived view exceeds the pixel limit")
    return output


def derive_view(index: dict, source_id: str, box_value: str, scale_value: float, output_value: str) -> dict:
    sources = index.get("sources")
    if not isinstance(sources, list):
        raise HelperError("sources must be a list")
    source = next((item for item in sources if item.get("source_id") == source_id), None)
    if source is None or source.get("kind") != "image":
        raise HelperError("--source-id must name a registered gallery image")
    source_sha256 = source.get("sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise HelperError("Registered image has no valid source SHA256")

    parent = workspace_path(str(source.get("source", "")), "Source path")
    if not parent.is_file():
        raise HelperError("Registered source image is missing")

    output = workspace_path(output_value, "--out")
    derived_root = (WORK / DERIVED_ROOT).resolve()
    if not output.is_relative_to(derived_root) or output == derived_root:
        raise HelperError(f"--out must be a .png path under {DERIVED_ROOT}/")
    if output.suffix != ".png":
        raise HelperError("--out must use the .png suffix")
    metadata_path = output.with_name(output.name + ".json")
    if output.exists() or metadata_path.exists():
        raise HelperError("Derived view output already exists; choose a new path")

    box = parse_box(box_value)
    scale = validate_scale(scale_value)
    try:
        with Image.open(parent) as image:
            image.load()
            parent_size = image.size
            width, height = parent_size
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise HelperError(f"--box must satisfy 0 <= x0 < x1 <= {width} and 0 <= y0 < y1 <= {height}")
            output_size = derived_output_size(parent_size, box, scale)
            view = image.crop(box)
            if scale != 1.0:
                view = view.resize(output_size, Image.Resampling.LANCZOS)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.name + ".tmp")
            try:
                view.save(temporary, format="PNG", compress_level=6)
                os.replace(temporary, output)
            finally:
                if temporary.exists():
                    temporary.unlink()
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError(f"Could not derive image: {exc}") from exc

    relative_output = output.relative_to(WORK.resolve()).as_posix()
    metadata = {
        "schema": DERIVED_SCHEMA,
        "derived_view_id": relative_output,
        "source_id": source_id,
        "parent_source": source["source"],
        "parent_source_sha256": source_sha256,
        "parent_delivery_sha256": file_sha256(parent),
        "parent_dimensions": list(parent_size),
        "box": list(box),
        "scale": scale,
        "resampling": "lanczos" if scale != 1.0 else "none",
        "output_format": "png",
        "output": relative_output,
        "output_dimensions": list(output_size),
        "output_sha256": file_sha256(output),
    }
    try:
        save_json(metadata_path, metadata)
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    log_event("derive", True, f"Created {relative_output} from {source_id}")
    return {"derived_view": relative_output, "metadata": metadata_path.relative_to(WORK).as_posix(), **metadata}


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


def checked_evidence_kinds(index: dict, steps: list[int]) -> dict[str, str]:
    sources = index.get("sources")
    if not isinstance(sources, list):
        raise HelperError("sources must be a list")
    catalog = {}
    for source in sources:
        if not isinstance(source, dict):
            raise HelperError("sources entries must be objects")
        source_id = str(source.get("source_id", ""))
        kind = SOURCE_EVIDENCE_KINDS.get(source.get("kind"))
        if not source_id or kind is None:
            raise HelperError("Registered source has an invalid ID or kind")
        catalog[source_id] = kind
    known = set(catalog)
    kinds = {}
    for step in steps:
        value = read_checked_step(step, known)
        for number, evidence in enumerate(value["evidence"], 1):
            kinds[f"E{step:02d}_{number}"] = catalog[str(evidence["source_id"])]
    return kinds


def check_global_goal_observability(global_summary: dict, evidence_kinds: dict[str, str]) -> None:
    goals = global_summary.get("goals")
    if not isinstance(goals, list):
        raise HelperError("goals must be a list")
    for position, goal in enumerate(goals, 1):
        if not isinstance(goal, dict):
            raise HelperError(f"goal at position {position} must be an object")
        if goal.get("status") not in ("satisfied", "unsatisfied"):
            continue
        goal_id = goal.get("id")
        label = f"goal {goal_id}" if isinstance(goal_id, str) and goal_id else f"goal at position {position}"
        refs = goal.get("evidence_ids")
        if not isinstance(refs, list):
            refs = []
        if not any(evidence_kinds.get(str(ref)) in OBSERVABLE_KINDS for ref in refs):
            raise HelperError(
                f"{label}: known status needs screenshot evidence; module_output alone is insufficient; "
                "cite at least one registered image evidence ID or use unknown"
            )


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
    evidence_kinds = checked_evidence_kinds(index, steps)
    schema = read_json(WORK / "global_summary.schema.json")
    required = schema.get("required")
    if not isinstance(required, list):
        raise HelperError("global_summary.schema.json has no required list")
    require_fields(payload, required)
    check_global_goal_observability(payload, evidence_kinds)
    save_json(WORK / "global_summary.json", payload)
    log_event("global", True, "Saved global semantic judgment")
    return {"saved": "global_summary.json"}


def check_global(index: dict) -> dict:
    steps = require_all_steps_complete(index)
    global_summary = read_json(WORK / "global_summary.json")
    evidence_kinds = checked_evidence_kinds(index, steps)
    ids = list(evidence_kinds)
    bad = sorted({ref for ref in references(global_summary) if ref not in ids})
    if bad:
        raise HelperError("Unknown evidence refs: " + ",".join(bad))
    check_global_goal_observability(global_summary, evidence_kinds)
    log_event("check", True, "Local JSON and reference check; final strict validator not invoked")
    return {"ok": True, "steps": len(steps), "evidence_ids": len(ids), "strict_validation": "caller_only"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("step", "global", "check", "derive"))
    parser.add_argument("--json", dest="payload", help="JSON payload; omit to read stdin")
    parser.add_argument("--source-id", help="Derive mode: registered image source ID")
    parser.add_argument("--box", help="Derive mode: crop box x0,y0,x1,y1")
    parser.add_argument("--scale", type=float, default=1.0,
                        help=f"Derive mode: fixed scale from {DERIVED_MIN_SCALE:g} to {DERIVED_MAX_SCALE:g}")
    parser.add_argument("--out", help=f"Derive mode: output .png path under {DERIVED_ROOT}/")
    args = parser.parse_args()
    try:
        index = read_json(WORK / "input_index.json")
        if args.mode == "step":
            result = submit_step(index, read_payload(args.payload))
        elif args.mode == "global":
            result = submit_global(index, read_payload(args.payload))
        elif args.mode == "derive":
            missing = [name for name in ("source_id", "box", "out") if not getattr(args, name)]
            if missing:
                raise HelperError("Derive mode requires --" + ", --".join(item.replace("_", "-") for item in missing))
            result = derive_view(index, args.source_id, args.box, args.scale, args.out)
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
