#!/usr/bin/env python3
"""Strict local preflight for staged attribution workspaces.

This module is copied into every prepared workspace. It reconstructs the
runtime receipt from confirmed delivery tickets (Flux) or the live access
receipt (native D60), reuses the canonical assembler, and runs the same result
validator that finalization uses. It never calls a model or the network.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import stepwise_support_v2 as assembler  # noqa: E402
import validate_result as final_validator  # noqa: E402
import normalization as canonical_normalization  # noqa: E402
from harness.protocol import OutputProtocol, SourceCatalog  # noqa: E402


REFERENCES = HERE / "references"
SKILL_MANIFEST = HERE / "skill_manifest.json"


class PreflightError(ValueError):
    pass


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_mode(workspace: Path) -> str:
    """Return the receipt source for this attempt."""
    workspace = Path(workspace).resolve()
    out = workspace.parent
    binding_path = out / "input_binding.json"
    if binding_path.is_file():
        binding = read_json(binding_path)
        if binding.get("execution_backend") == "flux_native":
            return "flux"
    launch_path = out / "launch.json"
    if launch_path.is_file() and read_json(launch_path).get("family") == "native_d60":
        return "native"
    if (out / "delivery_tickets").is_dir():
        return "flux"
    if (out / "access_receipt.json").is_file():
        return "native"
    return "legacy"


def _catalog(index: dict) -> SourceCatalog:
    sources = index.get("sources")
    if not isinstance(sources, list):
        raise PreflightError("input_index.sources must be a list")
    catalog = SourceCatalog(sources)
    for entry in sources:
        source_id = entry.get("source_id")
        if source_id not in catalog.by_id or catalog.by_id[source_id]["source"] != entry.get("source"):
            raise PreflightError("input_index source IDs do not match the canonical source catalog")
    return catalog


def _page_members(index: dict, page_path: str):
    pages = index.get("log_pages")
    if not isinstance(pages, list):
        raise PreflightError("input_index.log_pages must be a list")
    page = next((item for item in pages if item.get("path") == page_path), None)
    if page is None:
        raise PreflightError("Confirmed ticket names an unknown log page: " + page_path)
    members = page.get("members")
    if not isinstance(members, list) or not members:
        raise PreflightError("input_index.log_pages has no member map; rerun workspace preparation")
    for member in members:
        if not isinstance(member, dict):
            raise PreflightError("input_index log page member must be an object")
        if not all(isinstance(member.get(name), int) for name in ("offset", "end", "total")):
            raise PreflightError("input_index log page member has invalid range metadata")
        if member["offset"] < 0 or member["end"] < member["offset"] or member["total"] < member["end"]:
            raise PreflightError("input_index log page member has an invalid range")
    return members


def _validated_tickets(workspace: Path, index: dict):
    workspace = Path(workspace).resolve()
    tickets_root = workspace.parent / "delivery_tickets"
    if not tickets_root.is_dir():
        return []
    sources = {str(item.get("source_id")): item for item in index.get("sources", [])
               if isinstance(item, dict)}
    pages = {item.get("path"): item for item in index.get("log_pages", [])
             if isinstance(item, dict)}
    confirmed = []
    for ticket_path in sorted(tickets_root.glob("*.json")):
        record = read_json(ticket_path)
        if not isinstance(record, dict) or record.get("confirmed") is not True:
            continue
        if record.get("ticket") != ticket_path.stem:
            raise PreflightError("Delivery ticket filename/content mismatch: " + ticket_path.name)
        if not isinstance(record.get("sha256"), str) or len(record["sha256"]) != 64:
            raise PreflightError("Delivery ticket has no valid SHA256: " + ticket_path.name)
        if not isinstance(record.get("tool_ref"), str) or not record["tool_ref"].strip():
            raise PreflightError("Delivery ticket has no native tool reference: " + ticket_path.name)
        kind = record.get("kind")
        resource = record.get("resource")
        if kind == "derived_image":
            if record.get("native_tool") != "view_image":
                raise PreflightError("Derived view ticket used the wrong native tool")
            continue
        if kind == "image":
            source = sources.get(str(resource))
            if source is None or source.get("kind") != "image":
                raise PreflightError("Confirmed image ticket names an unknown gallery source")
            path = (workspace / str(source.get("source"))).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise PreflightError("Delivered gallery image is missing: " + str(resource))
            if sha_file(path) != record["sha256"]:
                raise PreflightError("Delivered gallery image changed after confirmation: " + str(resource))
            if record.get("native_tool") != "view_image":
                raise PreflightError("Gallery image ticket was not confirmed by view_image")
        elif kind == "text":
            page = pages.get(str(resource))
            if page is None:
                raise PreflightError("Confirmed text ticket names an unknown log page")
            path = (workspace / str(resource)).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise PreflightError("Delivered log page is missing: " + str(resource))
            if sha_file(path) != record["sha256"]:
                raise PreflightError("Delivered log page changed after confirmation: " + str(resource))
            if record.get("native_tool") != "exec_command":
                raise PreflightError("Text ticket was not confirmed by exec_command")
        else:
            raise PreflightError("Confirmed delivery ticket has an unknown kind: " + str(kind))
        confirmed.append(record)
    return confirmed


def flux_receipt(workspace: Path, index: dict) -> dict:
    workspace = Path(workspace).resolve()
    tickets = _validated_tickets(workspace, index)
    sources = {str(item.get("source_id")): item for item in index.get("sources", [])
               if isinstance(item, dict)}
    events = {}
    ranges = {}
    for record in tickets:
        if record.get("kind") == "image":
            source_id = str(record.get("resource"))
            entry = sources[source_id]
            events[entry["source"]] = {
                "source": entry["source"],
                "sha256": entry.get("sha256"),
                "operation": "deliver_image",
                "success": True,
                "content_complete": True,
                "truncated": False,
                "delivery_sha256": record["sha256"],
                "tool_call_id": "flux-confirmation-" + record["ticket"],
                "native_tool_reference": record["tool_ref"],
            }
        elif record.get("kind") == "text":
            for member in _page_members(index, str(record.get("resource"))):
                ranges.setdefault(str(member.get("source_id")), set()).add(
                    (member["offset"], member["end"], member["total"])
                )
    for source_id, spans in ranges.items():
        entry = sources.get(source_id)
        if entry is None or entry.get("kind") == "image":
            raise PreflightError("Log page member names an unknown or image source")
        ordered = sorted(spans)
        totals = {span[2] for span in ordered}
        if len(totals) != 1:
            raise PreflightError("Delivered ranges disagree on source length: " + source_id)
        complete = (
            ordered[0][0] == 0
            and ordered[-1][1] == ordered[-1][2]
            and all(left[1] == right[0] for left, right in zip(ordered, ordered[1:]))
        )
        events[entry["source"]] = {
            "source": entry["source"],
            "sha256": entry.get("sha256"),
            "operation": "read_text",
            "success": True,
            "content_complete": complete,
            "truncated": not complete,
            "delivered_ranges": ordered,
            "tool_call_id": "flux-delivery-" + source_id,
        }
    identity = index.get("identity") if isinstance(index.get("identity"), dict) else {}
    return {
        "producer": "harness",
        "attempt": identity.get("attempt", identity.get("id")),
        "inventory_complete": True,
        "inventory": index.get("sources", []),
        "events": list(events.values()),
        "meaning": "Strict local replay of confirmed agent tool delivery.",
    }


def native_receipt(workspace: Path, index: dict) -> dict:
    path = Path(workspace).resolve().parent / "access_receipt.json"
    if not path.is_file():
        raise PreflightError("Native access receipt is not available yet; deliver evidence first")
    receipt = read_json(path)
    if not isinstance(receipt, dict) or not isinstance(receipt.get("events"), list):
        raise PreflightError("Native access receipt has no events list")
    return receipt


def runtime_receipt(workspace: Path, index: dict) -> dict:
    mode = runtime_mode(workspace)
    if mode == "flux":
        return flux_receipt(workspace, index)
    if mode == "native":
        return native_receipt(workspace, index)
    raise PreflightError("Strict preflight requires a bound Flux or native attempt")


def delivered_image_ids(workspace: Path, index: dict) -> set[str] | None:
    mode = runtime_mode(workspace)
    if mode == "legacy":
        return None
    receipt = runtime_receipt(workspace, index)
    by_source = {str(item.get("source")): str(item.get("source_id"))
                 for item in index.get("sources", []) if isinstance(item, dict)}
    delivered = set()
    for event in receipt.get("events", []):
        if (isinstance(event, dict) and event.get("success") is True
                and event.get("operation") == "deliver_image"
                and event.get("content_complete") is True
                and event.get("truncated", False) is False):
            source_id = by_source.get(str(event.get("source")))
            if source_id:
                delivered.add(source_id)
    return delivered


def assert_cited_images_delivered(workspace: Path, index: dict, evidence: list[dict]) -> None:
    image_ids = {str(item.get("source_id")) for item in index.get("sources", [])
                 if isinstance(item, dict) and item.get("kind") == "image"}
    cited_images = [item for item in evidence
                    if isinstance(item, dict) and str(item.get("source_id")) in image_ids]
    if not cited_images:
        return
    delivered = delivered_image_ids(workspace, index)
    if delivered is None:
        return
    missing = sorted({
        str(item.get("source_id"))
        for item in cited_images if str(item.get("source_id")) not in delivered
    })
    if missing:
        raise PreflightError(
            "Undelivered cited images: " + ",".join(missing)
            + "; call read and confirm the ticket before using them as evidence"
        )


def assert_gallery_delivered(workspace: Path, index: dict) -> None:
    delivered = delivered_image_ids(workspace, index)
    if delivered is None:
        return
    expected = {str(item.get("source_id")) for item in index.get("sources", [])
                if isinstance(item, dict) and item.get("kind") == "image"}
    missing = sorted(expected - delivered)
    if missing:
        raise PreflightError(
            "Gallery images not delivered: " + ",".join(missing)
            + "; every gallery image must be read, viewed and confirmed"
        )


def strict_preflight(workspace: Path, index: dict) -> dict:
    workspace = Path(workspace).resolve()
    if runtime_mode(workspace) == "legacy":
        raise PreflightError("Strict preflight requires a bound Flux or native attempt")
    if not REFERENCES.is_dir() or not SKILL_MANIFEST.is_file():
        raise PreflightError("Strict preflight support bundle is incomplete")
    catalog = _catalog(index)
    identity = index.get("identity")
    if not isinstance(identity, dict):
        raise PreflightError("input_index.identity must be an object")
    steps = index.get("available_steps")
    if not isinstance(steps, list):
        raise PreflightError("input_index.available_steps must be a list")
    manifest = read_json(SKILL_MANIFEST)
    schema = read_json(REFERENCES / "output.schema.json")
    protocol = OutputProtocol(schema, identity, manifest["skill_version"], catalog, steps)
    case = {
        "identity": identity,
        "inventory": index.get("sources", []),
        "available_steps": steps,
        "evidence_root": str(workspace),
    }
    receipt = runtime_receipt(workspace, index)
    projection = assembler.assemble(workspace, case, receipt, write_audit=False)
    canonical_normalization.normalize_prediction(projection, case)
    result = protocol.compile(projection)
    receipt_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=workspace.parent,
                prefix=".preflight-receipt-", suffix=".json", delete=False) as handle:
            json.dump(receipt, handle, ensure_ascii=False)
            receipt_path = Path(handle.name)
        errors = final_validator.validate_result(
            result,
            references=REFERENCES,
            evidence_root=workspace,
            access_receipt=receipt_path,
        )
    finally:
        if receipt_path is not None:
            receipt_path.unlink(missing_ok=True)
    if errors:
        raise PreflightError("Strict local preflight: " + "; ".join(errors[:8]))
    return {
        "evidence": len(result.get("evidence", [])),
        "candidates": len(result.get("error_candidates", [])),
        "decision": result.get("outcome", {}).get("decision"),
    }
