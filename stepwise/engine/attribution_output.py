"""Persist usable attribution output even when strict validation fails."""
from __future__ import annotations

import json
import time
from pathlib import Path

from harness.storage import atomic_json


ATTRIBUTION_FIELDS = (
    "outcome",
    "goals",
    "error_candidates",
    "attribution",
    "synthesis",
    "preconditions",
    "contamination",
    "recovery_opportunity",
    "template_gaps",
    "limitations",
)
SOURCES = (
    ("result.json", "result"),
    ("workspace/global_summary.json", "global_summary"),
)
REASONING_META = "reasoning_chain.meta.json"


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _case_id(case, fallback=None):
    if isinstance(case, dict):
        identity = case.get("identity")
        if isinstance(identity, dict) and identity.get("id"):
            return identity["id"]
        if case.get("case_id"):
            return case["case_id"]
    return fallback


def _has_attribution(payload):
    return any(field in payload for field in ATTRIBUTION_FIELDS)


def reasoning_reference(attempt):
    """Return a compact pointer to the attempt's archived reasoning events."""
    attempt = Path(attempt)
    meta_path = attempt / REASONING_META
    if not meta_path.is_file():
        return {"available": False}
    try:
        meta = read_json(meta_path)
    except (OSError, ValueError, TypeError):
        return {"available": False, "error": "reasoning metadata is unreadable"}
    return {
        "available": bool(meta.get("available")),
        "event_count": meta.get("event_count", 0),
        "text_characters": meta.get("text_characters", 0),
        "jsonl": meta.get("jsonl"),
        "markdown": meta.get("markdown"),
        "capture_policy": meta.get("capture_policy"),
    }


def read_attribution(attempt, case_id=None, state=None, validation_errors=None,
                     schema_errors=None):
    """Read result.json first, then the unvalidated global summary fallback."""
    attempt = Path(attempt)
    payload = None
    source = None
    for relative, label in SOURCES:
        path = attempt / relative
        if not path.is_file():
            continue
        try:
            value = read_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(value, dict) or not _has_attribution(value):
            continue
        payload = value
        source = label
        break
    if payload is None:
        return {
            "available": False,
            "case_id": case_id,
            "state": state,
            "validation_errors": list(validation_errors or []),
            "schema_errors": list(schema_errors or []),
            "reasoning_chain": reasoning_reference(attempt),
        }
    output = {
        "available": True,
        "case_id": case_id or _case_id(payload.get("case")),
        "state": state or payload.get("state"),
        "validation_passed": state == "validated",
        "result_source": source,
        "validation_errors": list(validation_errors or []),
        "schema_errors": list(schema_errors or []),
        "reasoning_chain": reasoning_reference(attempt),
    }
    if isinstance(payload.get("case"), dict):
        output["case"] = payload["case"]
    for field in ATTRIBUTION_FIELDS:
        if field in payload:
            output[field] = payload[field]
    return output


def write_attribution_result(out, case, state, validation_errors=None,
                             schema_errors=None):
    """Write attribution_result.json without changing validation acceptance."""
    out = Path(out)
    case_id = _case_id(case)
    output = read_attribution(
        out,
        case_id=case_id,
        state=state,
        validation_errors=validation_errors,
        schema_errors=schema_errors,
    )
    if isinstance(case, dict) and isinstance(case.get("identity"), dict):
        output.setdefault("case", case["identity"])
    output["schema"] = "attribution-output-v1"
    output["attempt"] = out.name
    atomic_json(out / "attribution_result.json", output)
    return output


def compact_summary(output):
    """Small batch-level view; the full attribution stays in attribution_result.json."""
    if not isinstance(output, dict) or not output.get("available"):
        return {"available": False, "result_source": None}
    attribution = output.get("attribution")
    initial = attribution.get("initial") if isinstance(attribution, dict) else None
    labels = initial.get("labels") if isinstance(initial, dict) else None
    outcome = output.get("outcome")
    synthesis = output.get("synthesis")
    return {
        "available": True,
        "result_source": output.get("result_source"),
        "decision": outcome.get("decision") if isinstance(outcome, dict) else None,
        "completion": outcome.get("completion") if isinstance(outcome, dict) else None,
        "initial": {
            "l1": labels.get("l1") if isinstance(labels, dict) else None,
            "l2": labels.get("l2") if isinstance(labels, dict) else None,
            "l3": labels.get("l3") if isinstance(labels, dict) else None,
            "function": labels.get("function") if isinstance(labels, dict) else None,
            "mechanism": labels.get("mechanism") if isinstance(labels, dict) else None,
            "actor": initial.get("responsible_actor") if isinstance(initial, dict) else None,
            "stage": initial.get("stage") if isinstance(initial, dict) else None,
            "step": initial.get("step") if isinstance(initial, dict) else None,
        },
        "summary": synthesis.get("summary") if isinstance(synthesis, dict) else None,
    }


def batch_attribution_results(rows):
    """Build a complete, validation-independent aggregate for batch consumers."""
    cases = []
    for case_id, record in rows:
        attempt = record.get("attempt_path")
        if attempt:
            output = read_attribution(
                Path(attempt),
                case_id=case_id,
                state=record.get("state"),
                validation_errors=record.get("validation_errors"),
                schema_errors=record.get("schema_errors"),
            )
        else:
            output = {
                "available": False,
                "case_id": case_id,
                "state": record.get("state"),
                "validation_errors": list(record.get("validation_errors") or []),
                "schema_errors": list(record.get("schema_errors") or []),
            }
        output.update(case_id=case_id, state=record.get("state"),
                      attempt=record.get("attempt"), attempt_path=attempt)
        cases.append(output)
    return {
        "schema": "batch-attribution-results-v1",
        "generated_at": time.time(),
        "cases": cases,
    }
