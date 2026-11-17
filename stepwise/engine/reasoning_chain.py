"""Archive reasoning events emitted by the model runtime.

The archive is faithful to what the runtime exposes.  It never invents a
missing chain-of-thought and never upgrades a reasoning summary into raw
hidden reasoning.  Native CLI stdout and Codex session JSONL are supported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time


SCHEMA = "reasoning-chain-v1"
EVENT_SCHEMA = "reasoning-event-v1"
OUTPUT_JSONL = "reasoning_chain.jsonl"
OUTPUT_MARKDOWN = "reasoning_chain.md"
OUTPUT_META = "reasoning_chain.meta.json"


def _texts(value):
    """Extract only text-bearing fields from a reasoning payload."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_texts(item))
        return result
    if not isinstance(value, dict):
        return []
    result = []
    for key in ("text", "summary_text", "raw_content", "content", "summary", "delta"):
        if key in value:
            result.extend(_texts(value[key]))
    return result


def _reasoning_candidates(event):
    if not isinstance(event, dict):
        return []
    event_type = str(event.get("type", ""))
    if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        return [event]
    if event_type.lower() == "reasoning":
        return [event]
    candidates = []
    item = event.get("item")
    if isinstance(item, dict) and str(item.get("type", "")).lower() == "reasoning":
        candidates.append(item)
    payload = event.get("payload")
    if isinstance(payload, dict):
        if str(payload.get("type", "")).lower() == "reasoning":
            candidates.append(payload)
        payload_item = payload.get("item")
        if (isinstance(payload_item, dict)
                and str(payload_item.get("type", "")).lower() == "reasoning"):
            candidates.append(payload_item)
    return candidates


def _source_label(path, attempt):
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(attempt).resolve()).as_posix()
    except ValueError:
        return str(path)


def read_reasoning_events(path, attempt=None):
    """Read reasoning records from one JSONL source without synthesizing text."""
    path = Path(path)
    attempt = Path(attempt or path.parent)
    records = []
    if not path.is_file():
        return records
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            for candidate in _reasoning_candidates(event):
                parts = []
                for text in _texts(candidate):
                    text = text.strip()
                    if text and text not in parts:
                        parts.append(text)
                if not parts:
                    continue
                event_id = candidate.get("id") or event.get("id")
                records.append({
                    "schema": EVENT_SCHEMA,
                    "event_id": str(event_id) if event_id else None,
                    "timestamp": event.get("timestamp"),
                    "text": "\n\n".join(parts),
                    "source": _source_label(path, attempt),
                    "source_line": line_number,
                    "record_type": event.get("type"),
                })
    return records


def _deduplicate(records):
    """Codex emits the same reasoning item in event_msg and response_item."""
    result = []
    positions = {}
    for record in records:
        key = record.get("event_id")
        if not key:
            key = "source:" + record["source"] + ":" + str(record["source_line"])
        if key in positions:
            existing = result[positions[key]]
            if len(record["text"]) > len(existing["text"]):
                existing["text"] = record["text"]
            source_refs = existing.setdefault("source_refs", [{
                "source": existing["source"], "line": existing["source_line"],
            }])
            source_refs.append({"source": record["source"], "line": record["source_line"]})
            continue
        positions[key] = len(result)
        record["source_refs"] = [{
            "source": record["source"], "line": record["source_line"],
        }]
        result.append(record)
    for sequence, record in enumerate(result, 1):
        record["sequence"] = sequence
    return result


def _discover_sources(attempt, thread_id=None, sessions_root=None):
    attempt = Path(attempt)
    sources = [attempt / name for name in ("stdout.jsonl", "events.jsonl", "transcript.jsonl")
               if (attempt / name).is_file()]
    if sources or not thread_id:
        return sources
    root = _sessions_root(sessions_root)
    if not root.is_dir():
        return sources
    sources = sorted(root.rglob(f"*{thread_id}*.jsonl"))
    return sources


def _sessions_root(sessions_root=None):
    if sessions_root:
        return Path(sessions_root)
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _stream_contains(path, token):
    token = token.encode("utf-8")
    overlap = max(0, len(token) - 1)
    tail = b""
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if token in tail + chunk:
                return True
            tail = (tail + chunk)[-overlap:] if overlap else b""
    return False


def _is_subagent_session(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            first = json.loads(stream.readline())
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return False
    payload = first.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


def _discover_attempt_sources(attempt, sessions_root=None):
    original = Path(attempt)
    attempt = original.resolve()
    root = _sessions_root(sessions_root)
    if not root.is_dir():
        return []
    marker = attempt / "status.json"
    start = marker.stat().st_mtime if marker.is_file() else attempt.stat().st_mtime
    cutoff = start - 3600
    tokens = {str(original), str(attempt)}
    found = []
    for path in root.rglob("*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified < cutoff or not _is_subagent_session(path):
            continue
        if any(_stream_contains(path, token) for token in tokens):
            found.append(path)
    return [max(found, key=lambda path: path.stat().st_mtime)] if found else []


def write_reasoning_chain(attempt, sources=(), *, module=None, model=None,
                          thread_id=None, sessions_root=None,
                          discover_attempt=False, write_empty=True):
    attempt = Path(attempt)
    attempt.mkdir(parents=True, exist_ok=True)
    source_paths = [Path(source) for source in sources]
    if not source_paths:
        source_paths = _discover_sources(attempt, thread_id, sessions_root)
    if not source_paths and discover_attempt:
        source_paths = _discover_attempt_sources(attempt, sessions_root)
    if not source_paths and not write_empty:
        return {
            "schema": SCHEMA, "available": False, "event_count": 0,
            "text_characters": 0, "module": module, "model": model,
            "thread_id": thread_id, "sources": [],
            "capture_policy": "verbatim_emitted_reasoning_only; no reconstruction",
            "reason": "no reasoning source was discovered",
        }
    records = []
    source_meta = []
    for source in source_paths:
        source = source.resolve()
        source_meta.append({
            "path": _source_label(source, attempt),
            "exists": source.is_file(),
            "sha256": _sha256(source) if source.is_file() else None,
        })
        records.extend(read_reasoning_events(source, attempt))
    records = _deduplicate(records)

    jsonl = attempt / OUTPUT_JSONL
    markdown = attempt / OUTPUT_MARKDOWN
    jsonl_temporary = jsonl.with_name(jsonl.name + ".tmp")
    with jsonl_temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(jsonl_temporary, jsonl)
    lines = ["# Model reasoning chain", ""]
    if not records:
        lines.append("No reasoning events were emitted by the runtime for this attempt.")
    for record in records:
        heading = f"## {record['sequence']}"
        if record.get("timestamp"):
            heading += " · " + str(record["timestamp"])
        lines.extend([heading, "", record["text"], ""])
    _atomic_text(markdown, "\n".join(lines))

    meta = {
        "schema": SCHEMA,
        "available": bool(records),
        "event_count": len(records),
        "text_characters": sum(len(record["text"]) for record in records),
        "module": module,
        "model": model,
        "thread_id": thread_id,
        "sources": source_meta,
        "jsonl": jsonl.name,
        "markdown": markdown.name,
        "capture_policy": "verbatim_emitted_reasoning_only; no reconstruction",
        "generated_at": time.time(),
    }
    _atomic_text(attempt / OUTPUT_META, json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--thread-id")
    parser.add_argument("--sessions-root", type=Path)
    parser.add_argument("--discover-attempt", action="store_true")
    parser.add_argument("--no-empty", action="store_true")
    parser.add_argument("--module")
    parser.add_argument("--model")
    args = parser.parse_args()
    meta = write_reasoning_chain(
        args.attempt, args.source, module=args.module, model=args.model,
        thread_id=args.thread_id, sessions_root=args.sessions_root,
        discover_attempt=args.discover_attempt, write_empty=not args.no_empty,
    )
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
