"""Deterministic, value-preserving normalization shared by finalization and preflight."""
from __future__ import annotations

import re


def normalize_refs(prediction):
    """Drop evidence references that do not resolve; success has no recovery."""
    evidence = prediction.get("evidence") or []
    known = {item.get("id") for item in evidence if isinstance(item, dict)}

    def clean(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "evidence_ids" and isinstance(value, list):
                    node[key] = [item for item in value if item in known]
                else:
                    clean(value)
        elif isinstance(node, list):
            for item in node:
                clean(item)

    clean(prediction)
    if ((prediction.get("outcome") or {}).get("decision")) == "success":
        opportunity = prediction.get("recovery_opportunity")
        if isinstance(opportunity, dict):
            for key in ("exists", "attempted", "outcome"):
                if key in opportunity:
                    opportunity[key] = "not_applicable"
    return prediction


def normalize_locator(prediction, case=None):
    """Remove locator filenames that do not agree with the cited source."""
    source_by_id = {}
    for entry in (case or {}).get("inventory") or []:
        source_id = entry.get("source_id")
        source = entry.get("source")
        if source_id and source:
            source_by_id[source_id] = source
    pattern = re.compile(
        r"(?<![\w.\-/\\])[\w\-]+(?:[/\\][\w\-]+)*\.(?:png|jpe?g|webp|json|txt|log)(?![\w.])",
        re.I,
    )
    for evidence in prediction.get("evidence") or []:
        locator = evidence.get("locator")
        source = str(evidence.get("source") or source_by_id.get(evidence.get("source_id")) or "")
        if not isinstance(locator, str) or not locator or not source:
            continue
        canonical = source.replace("\\", "/")
        names = {canonical, canonical.rsplit("/", 1)[-1]}

        def keep(match):
            name = match.group(0).replace("\\", "/").lstrip("./")
            return match.group(0) if (canonical == name or canonical.endswith("/" + name)
                                      or name in names) else ""

        fixed = pattern.sub(keep, locator)
        evidence["locator"] = " ".join(fixed.split()) or canonical
    return prediction


def normalize_steps(prediction, case):
    """Use the registered source step; leave unregistered image timing unchanged."""
    registered = {}
    for entry in case.get("inventory") or []:
        source_id = entry.get("source_id")
        step = entry.get("step")
        if source_id and step is not None:
            registered[source_id] = step
    for evidence in prediction.get("evidence") or []:
        source_id = evidence.get("source_id")
        if source_id in registered:
            evidence["step"] = registered[source_id]
    return prediction


def normalize_actors(node):
    """Normalize actor serialization without changing responsibility."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in ("responsible_actor", "actor") and isinstance(value, str) and value:
                node[key] = value.strip().lower()
            else:
                normalize_actors(value)
    elif isinstance(node, list):
        for item in node:
            normalize_actors(item)
    return node


def normalize_labels(prediction):
    """Fill hierarchy-only null labels without inventing values."""
    attribution = prediction.get("attribution") or {}
    if not isinstance(attribution, dict):
        return prediction
    for name in ("initial", "recovery"):
        node = attribution.get(name)
        if not isinstance(node, dict):
            continue
        labels = node.get("labels")
        if not isinstance(labels, dict):
            continue
        for field in ("module", "function", "mechanism", "object"):
            labels.setdefault(field, None)
    return prediction


def normalize_prediction(prediction, case=None):
    normalize_refs(prediction)
    normalize_locator(prediction, case)
    normalize_steps(prediction, case or {})
    normalize_actors(prediction)
    normalize_labels(prediction)
    return prediction
