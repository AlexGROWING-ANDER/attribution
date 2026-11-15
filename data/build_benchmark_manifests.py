"""Build model-facing manifests for the non-success GPT-5.6 benchmark cases."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "stepwise" / "engine"
sys.path.insert(0, str(ENGINE))

from harness.protocol import SourceCatalog, digest  # noqa: E402
from study.materials import EvidenceStore  # noqa: E402

DEFAULT_BASE = Path("/Users/xudonglai/Desktop/projects/failure_attribution_benchmark")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
STEP_IMAGE = re.compile(r"(?P<after>after_)?step_(?P<step>\d+)\.png$")
AGENT_IMAGE = re.compile(r"(?:screenshot_)?step_(?P<step>\d+)\.png$")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_file(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Evidence escapes case root: {path}")
    return resolved.relative_to(root.resolve()).as_posix()


def contiguous(values: list[int], label: str) -> list[int]:
    values = sorted(values)
    if not values or values != list(range(1, values[-1] + 1)):
        raise ValueError(f"{label}: steps are not contiguous from 1: {values[:20]}")
    return values


def image_entry(root: Path, path: Path, step: int | None, steps: list[int] | None = None) -> dict:
    entry = {
        "source": relative_file(root, path),
        "kind": "image",
        "sha256": sha256(path),
    }
    if steps:
        entry["step"] = None
        entry["steps"] = sorted(steps)
    else:
        entry["step"] = step
    return entry


def finalize_case(
    case_id: str,
    identity: dict,
    evidence_root: Path,
    inventory: list[dict],
    available_steps: list[int],
    ordered_image_sources: list[str],
    cache_root: Path,
    delivery_config: dict,
) -> dict:
    catalog = SourceCatalog(inventory)
    ordered = []
    for index, source in enumerate(ordered_image_sources):
        entry = catalog.by_path.get(source)
        if entry is None or entry["kind"] != "image":
            raise ValueError(f"{case_id}: ordered image is not in the catalog: {source}")
        ordered.append({
            "display_index": index,
            "source": entry["source"],
            "sha256": entry["sha256"],
        })
    if catalog.image_paths != sorted(item["source"] for item in ordered):
        raise ValueError(f"{case_id}: image scope does not equal the complete image catalog")
    scope = {
        "mode": "human_visible_images",
        "ordered_images": ordered,
        "sha256": digest(ordered),
    }
    store = EvidenceStore(
        evidence_root, catalog.entries, identity["attempt"], delivery_config, scope,
    )
    deliveries = []
    for image in ordered:
        entry = catalog.by_path[image["source"]]
        source_path = evidence_root / entry["source"]
        _, _, encoded = store._cas_image(entry, source_path.read_bytes())
        delivery_path = (
            cache_root / f"{entry['sha256']}.webp"
            if encoded["format"] == "webp"
            else source_path
        )
        if sha256(delivery_path) != encoded["delivery_sha256"]:
            raise ValueError(f"{case_id}: delivery cache mismatch: {entry['source']}")
        deliveries.append({
            **entry,
            "delivery_path": str(delivery_path.resolve()),
            "delivery_sha256": encoded["delivery_sha256"],
        })
    return {
        "case_id": case_id,
        "identity": identity,
        "evidence_root": str(evidence_root.resolve()),
        "inventory": catalog.entries,
        "available_steps": available_steps,
        "images": deliveries,
        "image_count": len(deliveries),
        "image_scope": scope,
    }


def template_identity(
    registry: dict, template_id: str, case_id: str, framework: str, dataset: str,
    task_id: str, instruction: str, attempt: str, adapter_version: str,
) -> dict:
    template = registry["templates"][template_id]
    if template["framework"] != framework:
        raise ValueError(f"{case_id}: template framework mismatch")
    return {
        "id": case_id,
        "framework": framework,
        "dataset": dataset,
        "task_id": task_id,
        "instruction": instruction,
        "attempt": attempt,
        "template_id": template_id,
        "template_sha256": template["sha256"],
        "policy_id": registry["policy_id"],
        "adapter_version": adapter_version,
    }


def build_mobile(base: Path, cache_root: Path, registry: dict) -> dict:
    package = base / "mobile_Agent_v3" / "gpt5.6"
    cases_root = package / "cases" / "converted_121cases"
    annotation_root = package / "annotations" / "mobileagent_121"
    mapping_path = annotation_root / "映射表.csv"
    with mapping_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = [row for row in rows if row.get("status", "").strip().lower() != "success"]
    cases = []
    for row in selected:
        case_id = row["case_id"]
        case_root = cases_root / case_id
        annotation = read_json(annotation_root / "annotations" / "mobile_agent_v3" / row["站点文件名"])
        instruction = annotation.get("intent", "").strip()
        if not instruction:
            raise ValueError(f"{case_id}: empty mobile instruction")
        step_dirs = sorted(
            (path for path in case_root.glob("step_*") if path.is_dir()),
            key=lambda path: int(path.name.split("_", 1)[1]),
        )
        steps = contiguous([int(path.name.split("_", 1)[1]) for path in step_dirs], case_id)
        inventory = []
        refs: dict[str, set[int]] = {}
        for step, step_dir in zip(steps, step_dirs):
            for log_path in sorted(step_dir.glob("*.json")):
                inventory.append({
                    "source": relative_file(case_root, log_path),
                    "kind": "log",
                    "step": step,
                    "sha256": sha256(log_path),
                })
                value = read_json(log_path)
                for message in value.get("messages", []):
                    for item in message.get("content", []):
                        if item.get("type") != "image_url":
                            continue
                        url = item.get("image_url", {}).get("url")
                        if not isinstance(url, str) or not url or url.startswith("data:"):
                            continue
                        image_path = (log_path.parent / url).resolve()
                        if image_path.suffix.lower() not in IMAGE_SUFFIXES:
                            continue
                        source = relative_file(case_root, image_path)
                        refs.setdefault(source, set()).add(step)
        for source, image_steps in refs.items():
            # A module-input reference identifies which step saw the image, not its
            # before/after timing. Mobile modules reuse one screenshot across roles
            # and steps, so do not bind it to one decision step.
            inventory.append(image_entry(case_root, case_root / source, None))
        extras = sorted(
            relative_file(case_root, path)
            for path in (case_root / "images" / "embedded").glob("*")
            if path.is_file() and relative_file(case_root, path) not in refs
        )
        if extras:
            raise ValueError(f"{case_id}: unreferenced embedded images: {extras[:3]}")
        ordered_sources = sorted(
            refs,
            key=lambda source: (min(refs[source]), source),
        )
        identity = template_identity(
            registry, "mobile_agent_v3", case_id, "mobile_agent_v3",
            "failure_attribution_benchmark/mobile_Agent_v3/gpt5.6",
            annotation.get("case_id", case_id), instruction,
            f"converted_121cases/{case_id}", "gpt56-lean-modules-v1",
        )
        cases.append(finalize_case(
            case_id, identity, case_root, inventory, steps, ordered_sources,
            cache_root, {"mode": "q85_webp_cas", "cache_root": str(cache_root.resolve())},
        ))
    return {"cases": cases, "image_delivery": {
        "mode": "q85_webp_cas", "cache_root": str(cache_root.resolve()),
    }}


def agent_case_dir(cases_root: Path, annotation: dict) -> Path:
    return cases_root / f"{annotation['app']}_{annotation['test_id']}"


def build_agent_s2(base: Path, cache_root: Path, registry: dict) -> dict:
    package = base / "agent_s2" / "gpt5.6-sol"
    cases_root = package / "cases" / "agents"
    annotations = read_json(package / "annotations" / "Agent-S2_完整标注.json")
    selected = [
        item for item in annotations
        if item.get("status") != "success" and not item.get("excluded", False)
    ]
    cases = []
    for annotation in selected:
        case_root = agent_case_dir(cases_root, annotation)
        case_id = case_root.name
        traj = read_json(case_root / "traj.json")
        steps = contiguous(
            [item["step_number"] for item in traj.get("trajectory", [])], case_id,
        )
        inventory = [{
            "source": "traj.json",
            "kind": "log",
            "step": None,
            "steps": steps,
            "sha256": sha256(case_root / "traj.json"),
        }]
        log_names = [name for name in ("original_console.log", f"{annotation['test_id']}.log")
                     if (case_root / name).is_file()]
        if not log_names:
            raise ValueError(f"{case_id}: no raw console log")
        log_path = case_root / log_names[0]
        inventory.append({
            "source": log_path.name,
            "kind": "log",
            "step": None,
            "steps": steps,
            "sha256": sha256(log_path),
        })
        images: dict[int, Path] = {}
        unnamed: list[Path] = []
        for path in sorted((case_root / "images").glob("*.png")):
            match = AGENT_IMAGE.fullmatch(path.name)
            if match is None:
                continue
            index = int(match.group("step"))
            if index <= len(steps):
                images[index] = path
            else:
                unnamed.append(path)
        missing = sorted(set(steps) - set(images))
        if missing:
            raise ValueError(f"{case_id}: missing step screenshots: {missing[:10]}")
        ordered_sources = []
        for step in steps:
            entry = image_entry(case_root, images[step], step)
            inventory.append(entry)
            ordered_sources.append(entry["source"])
        for path in unnamed:
            entry = image_entry(case_root, path, None)
            inventory.append(entry)
            ordered_sources.append(entry["source"])
        identity = template_identity(
            registry, "agent_s2_initial_recovery", case_id, "agent_s2",
            "failure_attribution_benchmark/agent_s2/gpt5.6-sol",
            annotation["case_key"], annotation["intent"].strip(),
            case_id, "agent-s2-traj-v1",
        )
        cases.append(finalize_case(
            case_id, identity, case_root, inventory, steps, ordered_sources,
            cache_root, {"mode": "q85_webp_cas", "cache_root": str(cache_root.resolve())},
        ))
    return {"cases": cases, "image_delivery": {
        "mode": "q85_webp_cas", "cache_root": str(cache_root.resolve()),
    }}


def build_uipath(base: Path, cache_root: Path, registry: dict) -> dict:
    package = base / "ui_path" / "gpt5.6"
    cases_root = package / "cases" / "unipath_130"
    annotations = read_json(package / "annotations" / "UniPath_完整标注.json")
    selected = [
        item for item in annotations
        if item.get("status") != "success" and not item.get("excluded", False)
    ]
    by_key = {}
    for case_root in sorted(path for path in cases_root.iterdir() if path.is_dir()):
        meta_path = case_root / "task_meta.json"
        if not meta_path.is_file():
            continue
        meta = read_json(meta_path)
        key = f"{meta['package']}::{meta['test_id']}"
        if key in by_key:
            raise ValueError(f"Duplicate UI Path task key: {key}")
        by_key[key] = (case_root, meta)
    cases = []
    for annotation in selected:
        case_root, meta = by_key[annotation["case_key"]]
        case_id = case_root.name
        trajectory_path = case_root / "trajectory.jsonl"
        trajectory = [
            json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        steps = contiguous([item["step"] for item in trajectory], case_id)
        inventory = [{
            "source": "trajectory.jsonl",
            "kind": "log",
            "step": None,
            "steps": steps,
            "sha256": sha256(trajectory_path),
        }]
        images_by_step: dict[int, list[Path]] = {step: [] for step in steps}
        unnamed: list[Path] = []
        for path in sorted((case_root / "screenshots").glob("*.png")):
            match = STEP_IMAGE.fullmatch(path.name)
            if match is None:
                continue
            step = int(match.group("step"))
            if step not in images_by_step:
                unnamed.append(path)
            else:
                images_by_step[step].append(path)
        missing = sorted(step for step, paths in images_by_step.items() if not paths)
        if missing:
            raise ValueError(f"{case_id}: missing step screenshots: {missing[:10]}")
        ordered_sources = []
        for step in steps:
            for path in sorted(images_by_step[step], key=lambda item: (item.name.startswith("after_"), item.name)):
                entry = image_entry(case_root, path, step)
                inventory.append(entry)
                ordered_sources.append(entry["source"])
        for path in unnamed:
            entry = image_entry(case_root, path, None)
            inventory.append(entry)
            ordered_sources.append(entry["source"])
        identity = template_identity(
            registry, "uipath", case_id, "uipath",
            "failure_attribution_benchmark/ui_path/gpt5.6",
            annotation["case_key"], meta["intent"].strip(),
            case_id, "uipath-trajectory-jsonl-v1",
        )
        cases.append(finalize_case(
            case_id, identity, case_root, inventory, steps, ordered_sources,
            cache_root, {"mode": "q85_webp_cas", "cache_root": str(cache_root.resolve())},
        ))
    return {"cases": cases, "image_delivery": {
        "mode": "q85_webp_cas", "cache_root": str(cache_root.resolve()),
    }}


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--cache-root", type=Path, default=REPO / "data/gpt56_image_cache")
    parser.add_argument("--framework", choices=("mobile", "agent_s2", "uipath", "all"), default="all")
    parser.add_argument("--out-root", type=Path, default=REPO / "data")
    args = parser.parse_args()
    registry = read_json(
        REPO / "stepwise/skills/v3/gui-trajectory-attribution/references/template-registry.json"
    )
    args.cache_root.mkdir(parents=True, exist_ok=True)
    builders = {
        "mobile": (build_mobile, "gpt56_mobile_fail50_manifest.json"),
        "agent_s2": (build_agent_s2, "gpt56_agents2_fail71_manifest.json"),
        "uipath": (build_uipath, "gpt56_uipath_non_success51_manifest.json"),
    }
    names = list(builders) if args.framework == "all" else [args.framework]
    summary = {}
    for name in names:
        builder, filename = builders[name]
        manifest = builder(args.base.resolve(), args.cache_root.resolve(), registry)
        output = args.out_root / filename
        write_manifest(output, manifest)
        summary[name] = {
            "manifest": str(output.resolve()),
            "cases": len(manifest["cases"]),
            "images": sum(case["image_count"] for case in manifest["cases"]),
            "steps": sum(len(case["available_steps"]) for case in manifest["cases"]),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
