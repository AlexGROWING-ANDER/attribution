"""Build the model-facing manifest from organizer inventories and public identity fields."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stepwise" / "engine"))

from harness.protocol import SourceCatalog, digest  # noqa: E402
from study.materials import EvidenceStore  # noqa: E402


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(base: Path, output: Path, cache_root: Path) -> dict:
    cases_root = base / "121cases"
    mapping_root = base / "mobileagent_121"
    annotations_root = mapping_root / "annotations" / "mobile_agent_v3"
    registry = read_json(
        REPO / "stepwise/skills/v3/gui-trajectory-attribution/references/template-registry.json"
    )
    template = registry["templates"]["mobile_agent_v3"]

    case_dirs = sorted(
        path for path in cases_root.iterdir() if path.is_dir() and path.name.startswith("case_")
    )
    expected_ids = [path.name for path in case_dirs]
    if len(expected_ids) != 121 or len(set(expected_ids)) != 121:
        raise ValueError("Expected exactly 121 uniquely named case directories")

    with (mapping_root / "映射表.csv").open(encoding="utf-8-sig", newline="") as handle:
        mapping = {row["case_id"]: row for row in csv.DictReader(handle)}
    if sorted(mapping) != expected_ids:
        raise ValueError("CSV identities do not match the 121 case directories")

    cache_root.mkdir(parents=True, exist_ok=True)
    manifest_cases = []
    seen_tasks: set[str] = set()
    total_images = 0
    for case_dir in case_dirs:
        case_id = case_dir.name
        record_path = case_dir / "zz_public_input_record.json"
        record = read_json(record_path)
        if record.get("record_kind") != "organizer_input_inventory":
            raise ValueError(f"{case_id}: unexpected input-record kind")
        if record.get("inventory_complete_for_supplied_attempt") is not True:
            raise ValueError(f"{case_id}: inventory is not complete")
        if record.get("task_outcome_not_provided") is not True:
            raise ValueError(f"{case_id}: outcome unexpectedly present")

        observed = record.get("observed_files")
        if not isinstance(observed, list) or not observed:
            raise ValueError(f"{case_id}: empty observed-file inventory")
        inventory = []
        images_by_index = {}
        step_values = set()
        for item in observed:
            source = item["source"]
            kind = item["kind"]
            if kind not in {"image", "log"}:
                raise ValueError(f"{case_id}: unsupported inventory kind {kind!r}")
            if not isinstance(source, str) or source.startswith("/") or ".." in source.split("/"):
                raise ValueError(f"{case_id}: unsafe source path {source!r}")
            source_path = case_dir / source
            if not source_path.is_file() or sha256(source_path) != item["sha256"]:
                raise ValueError(f"{case_id}: source changed or missing: {source}")
            entry = {
                "source": source,
                "kind": kind,
                "step": item.get("step"),
                "sha256": item["sha256"],
            }
            inventory.append(entry)
            if kind == "image":
                index = item.get("display_index")
                if type(index) is not int or index in images_by_index:
                    raise ValueError(f"{case_id}: invalid or duplicate display_index")
                images_by_index[index] = {
                    "display_index": index,
                    "source": source,
                    "sha256": item["sha256"],
                }
            elif isinstance(item.get("step"), int) and item["step"] > 0:
                step_values.add(item["step"])

        ordered_images = [images_by_index[index] for index in range(len(images_by_index))]
        if any(item["display_index"] != index for index, item in enumerate(ordered_images)):
            raise ValueError(f"{case_id}: image display indices are not contiguous")
        scope = {
            "mode": "human_visible_images",
            "ordered_images": ordered_images,
            "sha256": digest(ordered_images),
        }
        if scope["sha256"] != record.get("image_scope_sha256"):
            raise ValueError(f"{case_id}: gallery hash differs from organizer record")

        inventory.append(
            {
                "source": record_path.name,
                "kind": "metadata",
                "step": None,
                "sha256": sha256(record_path),
            }
        )
        catalog = SourceCatalog(inventory)
        if catalog.image_paths != sorted(item["source"] for item in ordered_images):
            raise ValueError(f"{case_id}: image catalog does not match gallery")

        annotation_path = annotations_root / mapping[case_id]["站点文件名"]
        annotation = read_json(annotation_path)
        if annotation.get("taxonomy_template") != "mobile_agent_v3":
            raise ValueError(f"{case_id}: annotation template is not mobile_agent_v3")
        task_id = annotation.get("case_id")
        instruction = annotation.get("intent")
        if not isinstance(task_id, str) or not task_id or task_id in seen_tasks:
            raise ValueError(f"{case_id}: missing or duplicate task_id")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"{case_id}: missing task instruction")
        seen_tasks.add(task_id)

        identity = {
            "id": case_id,
            "framework": "mobile_agent_v3",
            "dataset": "failure_attribution_benchmark/mobile_Agent_v3/gpt5.6",
            "task_id": task_id,
            "instruction": instruction,
            "attempt": f"121cases/{case_id}",
            "template_id": "mobile_agent_v3",
            "template_sha256": template["sha256"],
            "policy_id": registry["policy_id"],
            "adapter_version": "unknown",
        }
        delivery_entries = []
        store = EvidenceStore(
            case_dir,
            catalog.entries,
            identity["attempt"],
            {"mode": "q85_webp_cas", "cache_root": str(cache_root)},
            scope,
        )
        for image in ordered_images:
            entry = catalog.by_path[image["source"]]
            source_path = case_dir / entry["source"]
            _, _, encoded = store._cas_image(entry, source_path.read_bytes())
            delivery_path = (
                cache_root / f"{entry['sha256']}.webp"
                if encoded["format"] == "webp"
                else source_path
            )
            if sha256(delivery_path) != encoded["delivery_sha256"]:
                raise ValueError(f"{case_id}: delivery cache mismatch for {entry['source']}")
            delivery_entries.append(
                {
                    **entry,
                    "delivery_path": str(delivery_path.resolve()),
                    "delivery_sha256": encoded["delivery_sha256"],
                }
            )

        manifest_cases.append(
            {
                "case_id": case_id,
                "identity": identity,
                "evidence_root": str(case_dir.resolve()),
                "inventory": catalog.entries,
                "available_steps": sorted(step_values),
                "images": delivery_entries,
                "image_count": len(delivery_entries),
                "image_scope": scope,
            }
        )
        total_images += len(delivery_entries)

    manifest = {
        "cases": manifest_cases,
        "image_delivery": {
            "mode": "q85_webp_cas",
            "cache_root": str(cache_root.resolve()),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"cases": len(manifest_cases), "images": total_images, "manifest": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(
            "/Users/xudonglai/Desktop/projects/failure_attribution_benchmark/mobile_Agent_v3/gpt5.6"
        ),
    )
    parser.add_argument("--out", type=Path, default=REPO / "data/prediction_manifest.json")
    parser.add_argument("--cache-root", type=Path, default=REPO / "data/image_cache")
    args = parser.parse_args()
    print(json.dumps(build(args.base.resolve(), args.out.resolve(), args.cache_root.resolve())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
