"""Build a model-facing manifest from a packaged UI Path annotation-evidence tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stepwise" / "engine"))

from harness.protocol import SourceCatalog, digest  # noqa: E402
from study.materials import EvidenceStore  # noqa: E402
from PIL import Image  # noqa: E402

EVIDENCE_RELATIVE = Path("01_QWEN_TRAJECTORIES/reports/annotation_evidence")
GRID_COLUMNS = 5
GRID_ROWS = 6
CELL_WIDTH = 200
CELL_HEIGHT = 498
SHEET_SIZE = (GRID_COLUMNS * CELL_WIDTH, GRID_ROWS * CELL_HEIGHT)
STEP_HEADING = re.compile(r"^## Step\s+(\d+)\s*$", re.MULTILINE)
CASE_ID = re.compile(r"^- case_id:\s*`([^`]+)`\s*$", re.MULTILINE)
INTENT = re.compile(r"^- intent:\s*(.+?)\s*$", re.MULTILINE)
SCREENSHOT_COUNT = re.compile(r"^- screenshot_count:\s*(\d+)\s*$", re.MULTILINE)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate_evidence(base: Path) -> tuple[Path, Path]:
    """Return the full_130 and annotation_evidence directories for supported layouts."""
    candidates = [base / EVIDENCE_RELATIVE, *base.glob(f"*/{EVIDENCE_RELATIVE}")]
    matches = sorted({path.resolve() for path in candidates if (path / "full_130").is_dir()})
    if len(matches) != 1:
        raise ValueError("Expected exactly one UI Path annotation_evidence/full_130 tree")
    evidence_root = matches[0]
    return evidence_root / "full_130", evidence_root


def parse_digest(path: Path, expected_case_id: str) -> tuple[str, str, list[int]]:
    text = path.read_text(encoding="utf-8-sig")
    case_match = CASE_ID.search(text)
    intent_match = INTENT.search(text)
    count_match = SCREENSHOT_COUNT.search(text)
    if not case_match or not intent_match:
        raise ValueError(f"{expected_case_id}: digest is missing case_id or intent")
    case_id = case_match.group(1).strip()
    intent = intent_match.group(1).strip()
    if case_id != expected_case_id:
        raise ValueError(f"{expected_case_id}: digest case_id is {case_id!r}")
    if not intent:
        raise ValueError(f"{case_id}: digest intent is empty")
    steps = [int(value) for value in STEP_HEADING.findall(text)]
    if not steps or steps != sorted(set(steps)):
        raise ValueError(f"{case_id}: digest steps must be unique and ordered")
    if steps[-1] > GRID_COLUMNS * GRID_ROWS:
        raise ValueError(f"{case_id}: step {steps[-1]} is outside the 5x6 contact-sheet grid")
    if count_match and int(count_match.group(1)) != len(steps):
        raise ValueError(f"{case_id}: screenshot_count differs from Step headings")
    return case_id, intent, steps


def cell_box(step: int) -> tuple[int, int, int, int]:
    row, column = divmod(step - 1, GRID_COLUMNS)
    return (
        column * CELL_WIDTH,
        row * CELL_HEIGHT,
        (column + 1) * CELL_WIDTH,
        (row + 1) * CELL_HEIGHT,
    )


def materialize_case(
    case_id: str,
    digest_path: Path,
    contact_path: Path,
    tail_path: Path | None,
    steps: list[int],
    evidence_out: Path,
) -> tuple[Path, dict[int, Path], Path | None, str]:
    """Mirror the digest and split the fixed 5x6 contact sheet into lossless step cells."""
    case_root = evidence_out / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    digest_copy = case_root / "trajectory_digest.md"
    if not digest_copy.is_file() or sha256(digest_copy) != sha256(digest_path):
        shutil.copyfile(digest_path, digest_copy)

    contact_sha = sha256(contact_path)
    crops: dict[int, Path] = {}
    with Image.open(contact_path) as image:
        if image.size != SHEET_SIZE:
            raise ValueError(f"{case_id}: unsupported contact-sheet dimensions {image.size}")
        for step in steps:
            target = case_root / f"step_{step:02d}.png"
            image.crop(cell_box(step)).save(target, format="PNG", optimize=True)
            crops[step] = target

    tail_copy = None
    if tail_path is not None:
        tail_copy = case_root / "tail_sheet.png"
        if not tail_copy.is_file() or sha256(tail_copy) != sha256(tail_path):
            shutil.copyfile(tail_path, tail_copy)
    return digest_copy, crops, tail_copy, contact_sha


def build(
    base: Path,
    output: Path,
    cache_root: Path,
    evidence_out: Path,
    dataset: str = "failure_attribution_benchmark/ui_path/qwen3.7-plus",
    case_ids: list[str] | None = None,
) -> dict:
    cases_root, source_evidence = locate_evidence(base)
    registry = read_json(
        REPO / "stepwise/skills/v3/gui-trajectory-attribution/references/template-registry.json"
    )
    template = registry["templates"]["uipath"]
    if template["schema"] != "dimensions":
        raise ValueError("The frozen uipath template must use the dimensions schema")

    available = sorted(
        path for path in cases_root.iterdir()
        if path.is_dir() and (path / "trajectory_digest.md").is_file()
    )
    available_ids = [path.name for path in available]
    if not available_ids:
        raise ValueError("No UI Path cases with trajectory_digest.md were found")
    selected = available_ids if case_ids is None else list(case_ids)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Case selection must contain unique IDs")
    missing = sorted(set(selected) - set(available_ids))
    if missing:
        raise ValueError("Requested UI Path cases are missing: " + ", ".join(missing))

    cache_root.mkdir(parents=True, exist_ok=True)
    evidence_out.mkdir(parents=True, exist_ok=True)
    delivery_config = {"mode": "q85_webp_cas", "cache_root": str(cache_root.resolve())}
    manifest_cases = []
    seen_case_ids: set[str] = set()
    total_images = 0
    total_steps = 0

    for case_id in selected:
        case_dir = cases_root / case_id
        digest_path = case_dir / "trajectory_digest.md"
        contact_path = case_dir / "contact_sheet_01.png"
        if not contact_path.is_file():
            raise ValueError(f"{case_id}: contact_sheet_01.png is missing")
        parsed_id, instruction, steps = parse_digest(digest_path, case_id)
        if parsed_id in seen_case_ids:
            raise ValueError(f"Duplicate UI Path case_id: {parsed_id}")
        seen_case_ids.add(parsed_id)

        source_tail = source_evidence / "tail_130" / case_id / "tail_sheet.png"
        digest_copy, crops, tail_copy, contact_sha = materialize_case(
            case_id, digest_path, contact_path,
            source_tail if source_tail.is_file() else None, steps, evidence_out,
        )
        digest_source = f"{case_id}/trajectory_digest.md"
        inventory = [{
            "source": digest_source,
            "kind": "log",
            "step": None,
            "steps": steps.copy(),
            "sha256": sha256(digest_copy),
        }]
        ordered_images = []
        for step in steps:
            source = f"{case_id}/step_{step:02d}.png"
            crop_path = crops[step]
            inventory.append({
                "source": source,
                "kind": "image",
                "step": step,
                "sha256": sha256(crop_path),
                "derivation": {
                    "operation": "crop",
                    "source_contact_sheet_sha256": contact_sha,
                    "box": list(cell_box(step)),
                },
            })
            ordered_images.append({
                "display_index": len(ordered_images),
                "source": source,
                "sha256": sha256(crop_path),
            })

        if tail_copy is not None:
            tail_source = f"{case_id}/tail_sheet.png"
            inventory.append({
                "source": tail_source,
                "kind": "image",
                "step": max(steps),
                "phase": "final_observation",
                "sha256": sha256(tail_copy),
            })
            ordered_images.append({
                "display_index": len(ordered_images),
                "source": tail_source,
                "sha256": sha256(tail_copy),
            })

        scope = {
            "mode": "human_visible_images",
            "ordered_images": ordered_images,
            "sha256": digest(ordered_images),
        }
        catalog = SourceCatalog(inventory)
        if catalog.image_paths != sorted(item["source"] for item in ordered_images):
            raise ValueError(f"{case_id}: image catalog differs from the registered gallery")

        identity = {
            "id": case_id,
            "framework": "uipath",
            "dataset": dataset,
            "task_id": case_id.replace("__", "/", 1),
            "instruction": instruction,
            "attempt": f"uipath_evidence/{case_id}",
            "template_id": "uipath",
            "template_sha256": template["sha256"],
            "policy_id": registry["policy_id"],
            "adapter_version": "uipath-cell-crops-v2",
        }
        store = EvidenceStore(evidence_out, catalog.entries, identity["attempt"], delivery_config, scope)
        delivery_entries = []
        for image in ordered_images:
            entry = catalog.by_path[image["source"]]
            source_path = evidence_out / entry["source"]
            _, _, encoded = store._cas_image(entry, source_path.read_bytes())
            delivery_path = (
                cache_root / f"{entry['sha256']}.webp"
                if encoded["format"] == "webp"
                else source_path
            )
            if sha256(delivery_path) != encoded["delivery_sha256"]:
                raise ValueError(f"{case_id}: delivery cache mismatch for {entry['source']}")
            delivery_entries.append({
                **entry,
                "delivery_path": str(delivery_path.resolve()),
                "delivery_sha256": encoded["delivery_sha256"],
            })

        manifest_cases.append({
            "case_id": case_id,
            "identity": identity,
            "evidence_root": str(evidence_out.resolve()),
            "inventory": catalog.entries,
            "available_steps": steps,
            "images": delivery_entries,
            "image_count": len(delivery_entries),
            "image_scope": scope,
        })
        total_images += len(delivery_entries)
        total_steps += len(steps)

    manifest = {"cases": manifest_cases, "image_delivery": delivery_config}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "cases": len(manifest_cases),
        "images": total_images,
        "steps": total_steps,
        "manifest": str(output),
        "evidence_root": str(evidence_out),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="UI Path package root or qwen_uipath_teacher_share root")
    parser.add_argument("--out", type=Path, default=REPO / "data/uipath_qwen_manifest.json")
    parser.add_argument("--cache-root", type=Path, default=REPO / "data/uipath_image_cache")
    parser.add_argument("--evidence-out", type=Path, default=REPO / "data/uipath_evidence")
    parser.add_argument("--dataset", default="failure_attribution_benchmark/ui_path/qwen3.7-plus")
    parser.add_argument("--cases", nargs="+", help="Optional subset of case IDs")
    args = parser.parse_args()
    print(json.dumps(build(
        args.base.resolve(), args.out.resolve(), args.cache_root.resolve(),
        args.evidence_out.resolve(), args.dataset, args.cases,
    ), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
