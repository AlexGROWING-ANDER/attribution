"""Losslessly gzip completed run transcripts without removing case results."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import tempfile


def compact_file(path: Path) -> tuple[int, int]:
    target = path.with_name(path.name + ".gz")
    if target.exists():
        return 0, 0
    before = path.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source, target.open("wb") as raw:
        with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw, mtime=0) as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    with gzip.open(target, "rb") as handle:
        while handle.read(1024 * 1024):
            pass
    path.unlink()
    return before, target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    attempts = sorted(root.glob("runs/*/attempt_*/status.json"))
    files: list[Path] = []
    for status in attempts:
        attempt = status.parent
        try:
            record = json.loads(status.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if record.get("state") not in {"validated", "invalid"}:
            continue
        files.extend((attempt / "calls").glob("*/responses_public.json"))
        files.extend((attempt / "home" / "sessions").rglob("*.jsonl"))
    before = after = count = 0
    for path in files:
        if not path.is_file():
            continue
        used_before, used_after = compact_file(path)
        if used_before:
            before += used_before
            after += used_after
            count += 1
    print(json.dumps({
        "completed_attempts": len(attempts),
        "compressed_files": count,
        "bytes_before": before,
        "bytes_after": after,
        "bytes_saved": before - after,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
