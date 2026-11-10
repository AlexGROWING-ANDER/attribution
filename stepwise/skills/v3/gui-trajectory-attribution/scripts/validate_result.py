#!/usr/bin/env python3
"""Read-only validation of attribution structure and references, not causal truth.

Requires the locally installed jsonschema package. No network, device access,
model calls, automatic dependency installation or writes to the input attempt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

REFERENCES = Path(__file__).resolve().parents[1] / "references"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def relative_source(value):
    """Normalize a portable source path and reject absolute/parent escapes."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source path must be non-empty")
    normalized = value.replace("\\", "/")
    if (Path(normalized).is_absolute() or PureWindowsPath(value).drive
            or normalized.startswith("/") or ".." in normalized.split("/")
            or ":" in normalized or "\x00" in normalized):
        raise ValueError(f"source must stay relative to attempt: {value}")
    canonical = PurePosixPath(normalized).as_posix()
    if canonical == ".":
        raise ValueError("source path must name a file")
    return canonical


def walk_evidence_refs(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key == "evidence_ids":
                yield child, value
            else:
                yield from walk_evidence_refs(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_evidence_refs(value, f"{path}[{index}]")


def locator_conflict(source, locator):
    """An explicit primary filename must agree; bare region/JSON locators remain legal.

    Do not interpret filenames in observation prose or fuzzy-match shortened hashes.
    Cross-file comparisons belong in separate evidence records, not this locator.
    """
    names = re.findall(r'(?<![\w.\-/\\])[\w\-]+(?:[/\\][\w\-]+)*\.(?:png|jpe?g|webp|json|txt|log)(?![\w.])',
                       locator, flags=re.IGNORECASE)
    if not names:
        return False
    canonical = source.replace('\\', '/')
    return any(not (canonical == name.replace('\\', '/') or
                    canonical.endswith('/' + name.replace('\\', '/'))) for name in names)


def is_answer_delivery_goal(goal):
    text = goal['object'] + ' ' + goal['requirement']
    return (bool(re.search(r'answer|response|reply|答复|答案|回答', text, re.I)) and
            bool(re.search(r'deliver|send|submit|return|交付|送达|发送|返回|提交', text, re.I)))


def has_delivery_observation(goal, evidence):
    """Necessary temporal evidence, not a claim that its pixels prove delivery.

    This check applies only to explicit answer delivery, never to pre-existing app goals.
    Unknown time alignment cannot establish a delivery after answer generation.
    """
    for eid in goal['evidence_ids']:
        item = evidence.get(eid, {})
        if item.get('kind') not in ('screenshot', 'execution_result'):
            continue
        if item.get('phase') not in ('after_action', 'execution', 'final_observation'):
            continue
        start, step = goal.get('first_seen_step'), item.get('step')
        if start is not None and step is not None and step < start:
            continue
        if item.get('phase') == 'final_observation' or (start is not None and step is not None):
            return True
    return False


def same_step_output(item, step):
    if item.get('step') != step or item.get('kind') not in ('module_output', 'action'):
        return False
    source_step = re.search(r'(?:^|/)step_(\d+)(?:/|$)', item.get('source', '').replace('\\', '/'))
    return source_step is None or int(source_step.group(1)) == step


def module_response_mislabeled(item, evidence_root):
    """Reject a model response presented as external metadata or an execution receipt.

    Inspect actual source structure, not a filename guess. Original task instructions
    in messages and separate genuine receipt/metadata fields remain distinct. This
    is a narrow provenance check, not automatic semantic classification of a file.
    """
    if evidence_root is None or item.get('kind') not in ('metadata', 'execution_result'):
        return False
    root = Path(evidence_root).resolve()
    try:
        target = (root / relative_source(item['source'])).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() != '.json':
            return False
        raw = load_json(target)
    except (ValueError, OSError):
        return False
    if not isinstance(raw, dict) or not isinstance(raw.get('messages'), list) or 'response' not in raw:
        return False
    locator = item.get('locator', '')
    return bool(re.search(r'(?i)(?:^|[.$/\s（(])response(?:$|[.\s/（(:：])', locator))


def actor_stage_allowed(actor, stage, framework, mapping):
    roles = (mapping['hierarchy'].get(framework) or mapping['dimensions'].get(framework) or {})
    allowed = set()
    for role, aliases in roles.items():
        if actor in aliases:
            allowed.update(mapping['stage_constraints'].get(framework, {}).get(role, []))
    for role, aliases in mapping['external_actors'].items():
        if actor in aliases:
            allowed.update(mapping['external_stage_constraints'].get(role, []))
    # Unknown sources remain unknown; this check does not invent responsibility.
    return not allowed or stage in allowed


def validate_result(data, references=REFERENCES, evidence_root=None, access_receipt=None):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RuntimeError("jsonschema is required locally; no dependency was installed") from exc
    references = Path(references)
    schema = load_json(references / "output.schema.json")
    Draft202012Validator.check_schema(schema)
    structural = sorted(Draft202012Validator(schema).iter_errors(data),
                        key=lambda e: str(list(e.absolute_path)))
    if structural:
        return [f"schema {list(e.absolute_path)}: {e.message}" for e in structural]

    errors = []
    def check(condition, message):
        if not condition:
            errors.append(message)

    def keyed(items, name):
        result = {}
        for item in items:
            check(item["id"] not in result, f"duplicate {name} id: {item['id']}")
            result[item["id"]] = item
        return result

    registry = load_json(references / "template-registry.json")
    actor_mapping = load_json(references / "actor-mapping.json")
    entry = registry["templates"].get(data["case"]["template_id"])
    taxonomy = None
    if entry is None:
        errors.append("unknown template_id; add an explicit frozen snapshot before using it")
    else:
        check(entry["framework"] == data["case"]["framework"], "framework/template mismatch")
        check(entry["sha256"] == data["case"]["template_sha256"], "result template hash differs from registry")
        relative_source(entry["taxonomy_path"])
        taxonomy_path = (references / entry["taxonomy_path"]).resolve()
        check(taxonomy_path.is_relative_to(references.resolve()), "taxonomy escapes registry directory")
        if taxonomy_path.is_relative_to(references.resolve()):
            raw = taxonomy_path.read_bytes()
            check(hashlib.sha256(raw).hexdigest() == entry["sha256"], "taxonomy snapshot hash mismatch")
            taxonomy = json.loads(raw.decode("utf-8-sig"))["zh"]

    check(data["case"]["policy_id"] == registry["policy_id"], "policy_id differs from registry")
    check(data["skill_version"] == registry["skill_version"], "skill_version differs from registry")
    evidence = keyed(data["evidence"], "evidence")
    candidates = keyed(data["error_candidates"], "candidate")
    keyed(data["goals"], "goal")
    keyed(data["preconditions"], "precondition")
    for path, refs in walk_evidence_refs(data):
        for eid in refs:
            check(eid in evidence, f"{path}: unknown evidence id {eid}")

    coverage = data["coverage"]
    steps = set(coverage["available_steps"])
    read_steps = set(coverage["log_steps_read"])
    check(coverage["total_steps"] == len(steps), "total_steps must count available_steps")
    check(read_steps <= steps, "log_steps_read includes an unavailable step")
    if coverage["scan_complete"]:
        check(read_steps == steps, "scan_complete claimed with unread log steps")
        check(not coverage["truncations"], "scan_complete claimed with truncated text")
        check(not coverage["missing"], "scan_complete claimed with missing or unreadable materials")
    if not coverage["scan_complete"]:
        check(bool(coverage["missing"] or coverage["truncations"] or coverage["unresolved"]),
              "incomplete scan needs a coverage gap explanation")
    def canonical_sources(values, name):
        normalized = set()
        for value in values:
            try:
                source = relative_source(value)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            check(source not in normalized, f"duplicate canonical {name} source: {source}")
            normalized.add(source)
        return normalized

    viewed = canonical_sources(coverage["screenshots_viewed"], "viewed image")
    check(len(viewed) <= coverage["screenshots_available"], "viewed screenshot count exceeds available count")
    available_images = canonical_sources(coverage["screenshot_sources_available"], "available image")
    check(coverage["screenshots_available"] == len(available_images), "screenshot count differs from full image index")
    check(viewed <= available_images, "screenshots_viewed includes an unindexed image")
    if coverage["scan_complete"]:
        check(coverage["screenshots_index_complete"], "scan_complete requires a complete screenshot index")
        check(viewed == available_images, "scan_complete claimed with unread screenshots")
    root = Path(evidence_root).resolve() if evidence_root is not None else None
    sources = viewed | available_images | {item["source"] for item in evidence.values()}
    for source in sources:
        try:
            normalized = relative_source(source)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if root is not None:
            target = (root / normalized).resolve()
            if not target.is_relative_to(root):
                errors.append(f"source escapes attempt through symlink: {source}")
            else:
                check(target.is_file(), f"missing evidence file: {source}")
                if target.is_file():
                    check(target.stat().st_size > 0, f"empty evidence file: {source}")
    for item in evidence.values():
        check(not module_response_mislabeled(item, root),
              f"evidence {item['id']}: module response cannot be external metadata/execution_result; cite the actual originating record")
        check(not locator_conflict(item['source'], item['locator']),
              f"evidence {item['id']}: locator filename differs from source")
        n = item["step"]
        final = bool(steps) and n == max(steps) + 1 and item["phase"] == "final_observation"
        check(n is None or n in steps or final, f"evidence {item['id']}: step not in attempt")
        if item["kind"] == "screenshot":
            try:
                source = relative_source(item["source"])
            except ValueError:
                continue  # Reported by the source checks above.
            check(source in viewed, f"screenshot {item['id']} not in screenshots_viewed")
    covered = set()
    for row in data["process"]:
        check(row["start_step"] <= row["end_step"], "process interval is reversed")
        check(row["start_step"] in steps and row["end_step"] in steps, "process endpoints not in attempt")
        covered.update(n for n in steps if row["start_step"] <= n <= row["end_step"])
    if coverage["scan_complete"]:
        check(covered == steps, "process does not cover all available steps")
    for candidate in candidates.values():
        check(candidate["step"] in steps, f"candidate {candidate['id']}: unknown step")
        check(actor_stage_allowed(candidate["actor"], candidate["stage"], data["case"]["framework"], actor_mapping),
              f"candidate {candidate['id']}: actor/stage conflicts with declared framework responsibility")
        if candidate['stage'] == 'command_generation' and candidate['actor'].lower() not in ('unknown', '未知', 'unconfirmed'):
            check(any(same_step_output(evidence.get(eid, {}), candidate['step'])
                      for eid in candidate['evidence_ids']),
                  f"candidate {candidate['id']}: command generation responsibility needs same-step original output; downstream history alone does not establish source")
    for goal in data["goals"]:
        n = goal["first_seen_step"]
        check(n is None or n in steps or (bool(steps) and n == max(steps) + 1),
              f"goal {goal['id']}: first_seen_step outside attempt")
        if goal["status"] == "satisfied":
            check(bool(goal["evidence_ids"]), f"satisfied goal {goal['id']} needs evidence")
        if goal["status"] in ("satisfied", "unsatisfied"):
            check(any(evidence.get(eid, {}).get("kind") in ("screenshot", "execution_result")
                      for eid in goal["evidence_ids"]),
                  f"goal {goal['id']}: known status needs observable goal evidence")
        if goal['status'] == 'satisfied' and is_answer_delivery_goal(goal):
            check(has_delivery_observation(goal, evidence),
                  f"goal {goal['id']}: answer delivery requires an observable post-action receipt or delivery view; generated answer and pre-action screenshots cannot establish delivery")
    for precondition in data["preconditions"]:
        n = precondition["step"]
        check(n is None or n in steps or (bool(steps) and n == max(steps) + 1),
              f"precondition {precondition['id']}: step outside attempt")

    attribution = data["attribution"]
    initial = attribution["initial"]
    recovery = attribution["recovery"]
    for name in ("initial", "recovery"):
        cause = attribution[name]
        if cause is None:
            continue
        check(cause["step"] in steps, f"{name}: step outside attempt")
        candidate = candidates.get(cause["cause_id"])
        check(candidate is not None, f"{name}: unknown cause_id")
        if candidate:
            check(cause["step"] == candidate["step"], f"{name}: candidate step mismatch")
            check(cause["stage"] == candidate["stage"], f"{name}: candidate stage mismatch")
            check(cause["responsible_actor"] == candidate["actor"], f"{name}: candidate actor mismatch")
        if cause["mapping_status"] == "gap":
            check(bool(data["template_gaps"]), f"{name}: mapping gap needs template_gaps explanation")
        if taxonomy is None:
            continue
        labels = cause["labels"]
        exact = cause["mapping_status"] == "exact"
        actor = cause["responsible_actor"]
        external = actor_mapping["external_actors"]
        check(actor_stage_allowed(actor, cause['stage'], data['case']['framework'], actor_mapping),
              f"{name}: actor/stage conflicts with declared framework responsibility")
        leaf = labels.get('l3') if entry['schema']=='hierarchy' else labels.get('mechanism')
        if leaf in actor_mapping['label_stage_constraints']:
            check(cause['stage'] in actor_mapping['label_stage_constraints'][leaf],
                  f"{name}: label/stage conflicts with declared mechanism")
        if entry["framework"] in ("mobile_agent_v3", "agent_s2"):
            if actor in ("manager", "planner"):
                check(cause["stage"] not in ("grounding", "adaptation", "execution"),
                      f"{name}: Manager stage conflicts with framework responsibility")

        if entry["schema"] == "hierarchy":
            if labels["l1"] == "framework" and labels["l2"] is not None:
                aliases = actor_mapping["hierarchy"].get(entry["framework"], {}).get(labels["l2"], [])
                check(actor in aliases, f"{name}: actor/label mismatch for {actor} and {labels['l2']}")
            elif labels["l1"] in ("app", "precondition"):
                check(actor in external[labels["l1"]], f"{name}: actor/label mismatch for external source")
            elif labels["l1"] is not None and exact:
                check(False, f"{name}: actor/label mismatch: unmapped hierarchy source")
        else:
            if labels["function"] == "E1":
                check(labels["module"] in (None, "environment_app")
                      and actor in external["app"] and cause["stage"] == "environment",
                      f"{name}: E1 needs an external App source, not a model module")
            if labels["module"] == "environment_app":
                check(name == "initial" and labels["function"] == "E1",
                      f"{name}: Environment / App module requires Initial E1")
            if labels["module"] is not None:
                aliases = actor_mapping["dimensions"].get(entry["framework"], {}).get(labels["module"], [])
                check(actor in aliases, f"{name}: actor/label mismatch for {actor} and {labels['module']}")
            elif labels["function"] == "E2":
                check(actor in external["precondition"], f"{name}: actor/label mismatch for E2")
            elif labels["function"] == "E1":
                check(actor in external["app"], f"{name}: actor/label mismatch for E1")
            elif labels["function"] in ("F9", "R5") and cause["mapping_status"] == "gap":
                check(actor in external["device"], f"{name}: actor/label mismatch for external device execution")
            else:
                check(False, f"{name}: actor/label mismatch: missing a mapped responsibility module")
        if entry["schema"] == "hierarchy":
            check(all(labels[k] is None for k in ("module", "function", "mechanism", "object")),
                  f"{name}: hierarchy cannot use dimension labels")
            node = taxonomy
            for index, key in enumerate(("l1", "l2", "l3")):
                value = labels[key]
                if value is None:
                    leaf_precondition = (key == "l3" and labels["l1"] == "precondition"
                                         and labels["l2"] in taxonomy.get("precondition", {}).get("l2", {})
                                         and not node)
                    check(not exact or leaf_precondition, f"{name}: exact hierarchy needs {key}")
                    check(all(labels[lower] is None for lower in ("l1", "l2", "l3")[index + 1:]),
                          f"{name}: child label provided without parent")
                    break
                check(value in node, f"{name}: illegal hierarchy path at {key}={value}")
                if value not in node:
                    break
                if key != "l3":
                    node = node[value].get("l2" if key == "l1" else "l3", {})
        else:
            check(all(labels[k] is None for k in ("l1", "l2", "l3")),
                  f"{name}: UniPath dimensions cannot be encoded as L1/L2/L3")
            for key, collection in [
                ("module", "modules"),
                ("function", "root_cause_functions" if name == "initial" else "recovery_functions"),
                ("mechanism", "mechanisms"), ("object", "objects"),
            ]:
                value = labels[key]
                if value is not None:
                    check(value in taxonomy[collection], f"{name}: illegal {key}={value}")
                else:
                    external_e1 = key == "module" and labels["function"] == "E1"
                    precondition_optional = labels["function"] == "E2" and key in ("module", "object")
                    check(not exact or external_e1 or precondition_optional, f"{name}: missing {key} requires a mapping gap")
    if taxonomy is not None and entry["schema"] == "hierarchy":
        for name in ("initial", "recovery"):
            cause = attribution[name]
            if cause is None or cause["labels"]["l1"] != "precondition":
                continue
            labels = cause["labels"]
            check(name == "initial", "precondition is an Initial root cause, not a Recovery defect")
            check(labels["l3"] is None, f"{name}: precondition L3 must be null")
            check(cause["stage"] == "environment" and cause["responsible_actor"] == "precondition",
                  f"{name}: precondition must not invent a model module")
            check(any(p["status"] == "confirmed" and p["type"] == labels["l2"]
                      and p["step"] == cause["step"] for p in data["preconditions"]),
                  f"{name}: precondition attribution needs matching confirmed type and step")
    if taxonomy is not None and entry["schema"] == "dimensions":
        precondition_types = set(registry["project_extensions"]["preconditions"]["types"])
        for name in ("initial", "recovery"):
            cause = attribution[name]
            if cause is None:
                continue
            labels = cause["labels"]
            if labels["function"] == "E2":
                check(name == "initial", "E2 is an Initial precondition function, not Recovery")
                check(labels["module"] is None, "E2 Module must be null")
                check(labels["mechanism"] in precondition_types, "E2 needs a precondition mechanism")
                check(cause["stage"] == "environment" and cause["responsible_actor"] == "precondition",
                      "E2 must not invent a model-module error")
                check(any(p["status"] == "confirmed" and p["type"] == labels["mechanism"]
                          and p["step"] == cause["step"] for p in data["preconditions"]),
                      "E2 needs matching confirmed type and step")
            elif labels["mechanism"] in precondition_types:
                check(False, "precondition mechanisms require Initial E2")
            if labels["mechanism"] == "invalid_action_format":
                check(labels["module"] in ("planner", "grounder"), "action format needs its generating module")
                if name == "initial":
                    check(labels["function"] == "F6", "Initial action format needs F6")
    for cid in attribution["secondary_causes"]:
        check(cid in candidates, f"unknown secondary cause: {cid}")
        if initial:
            check(cid != initial["cause_id"], "primary cause repeated as secondary")
    if initial is None:
        check(bool(attribution["absence_reason"]), "null Initial needs absence_reason")

    opportunity = data["recovery_opportunity"]
    allowed_recovery_states = {
        ("yes", "yes", "succeeded"), ("yes", "yes", "failed"), ("yes", "yes", "unknown"),
        ("yes", "no", "not_attempted"), ("yes", "unknown", "unknown"),
        ("no", "not_applicable", "not_applicable"),
        ("not_applicable", "not_applicable", "not_applicable"), ("unknown", "unknown", "unknown"),
    }
    check((opportunity["exists"], opportunity["attempted"], opportunity["outcome"]) in allowed_recovery_states,
          "illegal Recovery state combination")
    n = opportunity["step"]
    check(n is None or n in steps, "recovery opportunity step outside attempt")
    if opportunity["exists"] == "yes":
        check(n is not None and bool(opportunity["evidence_ids"]), "recovery opportunity needs step and evidence")
        if initial and n is not None:
            check(n >= initial["step"], "recovery opportunity predates Initial")
    if opportunity["exists"] in ("no", "not_applicable"):
        check(recovery is None, "Recovery filled without opportunity")
        check(opportunity["attempted"] == "not_applicable", "no opportunity requires attempted=not_applicable")
        check(opportunity["outcome"] == "not_applicable", "no opportunity requires outcome=not_applicable")
    if opportunity["exists"] == "no":
        check(bool(opportunity["evidence_ids"]), "no recovery opportunity needs terminal/flow evidence")
    if opportunity["attempted"] == "no":
        check(opportunity["outcome"] == "not_attempted", "no recovery attempt requires outcome=not_attempted")
    if opportunity["outcome"] in ("succeeded", "failed"):
        check(opportunity["exists"] == "yes" and opportunity["attempted"] == "yes",
              "recovery result requires opportunity and attempt")
    if opportunity["outcome"] == "succeeded":
        check(recovery is None, "successful recovery cannot carry a recovery failure root cause")
    if recovery:
        check(recovery["responsible_actor"] not in ("precondition", "app", "environment")
              and recovery["stage"] != "environment",
              "Recovery cannot be an external condition, including a mapping gap")
        check(initial is not None, "Recovery requires a linked Initial cause")
        check(opportunity["exists"] == "yes", "Recovery needs a confirmed opportunity")
        check(bool(attribution["recovery_independence"]), "Recovery needs an independent causal explanation")
        if initial:
            check(recovery["cause_id"] != initial["cause_id"], "Initial and Recovery cannot reuse a candidate")
            check(recovery["step"] >= initial["step"], "Recovery predates Initial")
        if n is not None:
            check(recovery["step"] >= n, "Recovery predates the opportunity")

    decision = data["outcome"]["decision"]
    completion = data["outcome"]["completion"]
    if decision in ("fail", "precondition_blocked"):
        check(completion in ("partial", "not_completed"), "completion/decision conflict")
        check(any(g["status"] == "unsatisfied" for g in data["goals"]),
              "failure or blocked decision needs an unsatisfied goal")
    if decision in ("success", "fail", "rerun", "precondition_blocked"):
        check(bool(data["outcome"]["evidence_ids"]), "outcome evidence required for a definitive decision")
    if decision in ("fail", "precondition_blocked"):
        check(any(evidence.get(eid, {}).get("kind") in ("screenshot", "execution_result")
                  for eid in data["outcome"]["evidence_ids"]),
              "observable failure evidence required, not only a module assertion or score")
    if decision == "rerun":
        check(any(evidence.get(eid, {}).get("kind") in ("screenshot", "execution_result", "metadata")
                  for eid in data["outcome"]["evidence_ids"]), "rerun needs observable or provenance evidence")
    if completion == "success":
        check(all(g["status"] == "satisfied" for g in data["goals"]), "success completion has unsatisfied/unknown goals")
    if decision == "success":
        check(data["outcome"]["completion"] == "success", "success decision needs success completion")
        check(initial is None and recovery is None, "success must not fill failure root causes")
        check(all(g["status"] == "satisfied" for g in data["goals"]), "success has unsatisfied/unknown goals")
        check(bool(data["outcome"]["evidence_ids"]), "success needs outcome evidence")
        check(opportunity["exists"] == "not_applicable", "success Recovery Opportunity must be N/A")
        check(coverage["scan_complete"], "success decision requires complete review")
        check(bool(data["goals"]), "success requires goals")
        check(any(evidence.get(eid, {}).get("kind") in ("screenshot", "execution_result")
                  for eid in data["outcome"]["evidence_ids"]), "success needs observable completion evidence")
    if data["contamination"]["status"] == "present":
        check(bool(data["contamination"]["evidence_ids"]), "confirmed contamination needs evidence")
        check(bool(data["contamination"]["reset_requirements"]), "confirmed contamination needs targeted reset requirements")
        check(decision in ("rerun", "pending", "precondition_blocked"), "affected baseline needs rerun, pending, precondition_blocked")
    if decision == "precondition_blocked":
        check(any(p["status"] == "confirmed" for p in data["preconditions"]),
              "precondition_blocked requires a confirmed precondition")
        primary_is_precondition = (initial is not None and (
            initial["labels"]["l1"] == "precondition" if entry and entry["schema"] == "hierarchy"
            else initial["labels"]["function"] == "E2"))
        check(primary_is_precondition, "blocked decision needs a precondition Initial")
        if taxonomy is not None and "precondition" not in taxonomy and "E2" not in taxonomy.get("root_cause_functions", {}):
            check(bool(data["template_gaps"]), "template lacks a dedicated precondition category; record the gap")
    if access_receipt is not None:
        errors.extend(validate_access_receipt(data, access_receipt, evidence_root))
    return errors


def validate_access_receipt(data, receipt_path, evidence_root):
    """Cross-check a caller-owned runtime receipt, not model self-reported coverage.

    This checks consistency and captured bytes, not the trustworthiness of the
    receipt issuer or whether a model comprehended delivered material.
    """
    errors = []
    if evidence_root is None:
        return ["access receipt verification requires --evidence-root"]
    receipt = load_json(receipt_path)
    if (not isinstance(receipt, dict) or receipt.get("producer") != "harness"
            or receipt.get("attempt") != data["case"]["attempt"]):
        return ["access receipt producer/attempt mismatch"]
    inventory, events = receipt.get("inventory"), receipt.get("events")
    if not isinstance(inventory, list) or not isinstance(events, list):
        return ["access receipt needs inventory and events arrays"]
    root = Path(evidence_root).resolve()
    indexed = {}
    for item in inventory:
        if not isinstance(item, dict):
            errors.append("invalid access inventory item");continue
        try:
            source = relative_source(item.get("source"))
        except ValueError as exc:
            errors.append(str(exc));continue
        target = (root/source).resolve()
        if source in indexed:
            errors.append("duplicate access inventory source")
        indexed[source] = dict(item, steps=set())
        if not target.is_relative_to(root) or not target.is_file():
            errors.append("access receipt inventory source missing or outside attempt");continue
        if hashlib.sha256(target.read_bytes()).hexdigest() != item.get("sha256"):
            errors.append("access receipt inventory hash mismatch")
        if item.get("kind") not in ("log", "image", "metadata"):
            errors.append("invalid access inventory kind")
        step = item.get("step")
        if step is not None and (type(step) is not int or step < 1):
            errors.append("invalid access inventory step")
        elif step is not None:
            indexed[source]["steps"].add(step)
        if "steps" in item:
            mapped = item["steps"]
            if (item.get("kind") not in ("log", "image", "metadata") or step is not None
                    or not isinstance(mapped, list) or not mapped
                    or any(type(n) is not int or n < 1 for n in mapped)):
                errors.append("invalid access inventory steps; use a non-empty source step list with step=null")
            elif len(set(mapped)) != len(mapped):
                errors.append("duplicate access inventory step")
            else:
                indexed[source]["steps"] = set(mapped)
    delivered = set()
    fully_delivered = set()
    for event in events:
        if not isinstance(event, dict):
            errors.append("invalid access event");continue
        if not isinstance(event.get("source"), str):
            errors.append("invalid access event source");continue
        try:
            source = relative_source(event["source"])
        except ValueError as exc:
            errors.append(str(exc));continue
        item = indexed.get(source)
        if item is None or event.get("sha256") != item.get("sha256"):
            errors.append("access event source/hash mismatch");continue
        operations = {"log": {"read_text"}, "image": {"deliver_image"}, "metadata": {"read_metadata", "read_text"}}.get(item.get("kind"), set())
        if "content_complete" in event and type(event["content_complete"]) is not bool:
            errors.append("invalid access event content_complete")
        if "truncated" in event and type(event["truncated"]) is not bool:
            errors.append("invalid access event truncated")
        if event.get("content_complete") is True and event.get("truncated") is True:
            errors.append("complete access event cannot be truncated")
        if (event.get("success") is True and event.get("operation") in operations
                and isinstance(event.get("tool_call_id"), str) and event["tool_call_id"]):
            delivered.add(source)
            if event.get("content_complete") is True and event.get("truncated", False) is False:
                fully_delivered.add(source)
    coverage = data["coverage"]
    def source_set(values):
        normalized = set()
        for value in values:
            try:
                normalized.add(relative_source(value))
            except ValueError as exc:
                errors.append(str(exc))
        return normalized

    claimed_images = source_set(coverage["screenshots_viewed"])
    for item in data["evidence"]:
        try:
            source = relative_source(item["source"])
        except ValueError:
            continue  # Already reported by the result source checks.
        if source not in delivered:
            errors.append(f"cited evidence lacks runtime access: {source}")
        origin = indexed.get(source)
        if origin is not None:
            if (item['kind']=='screenshot') != (origin.get('kind')=='image'):
                errors.append(f"evidence {item['id']}: kind differs from runtime source kind")
            mapped = origin['steps']
            final_alias = (origin.get('phase')=='final_observation'
                           and item.get('phase')=='final_observation'
                           and bool(data['coverage']['available_steps'])
                           and item.get('step')==max(data['coverage']['available_steps'])+1
                           and mapped=={max(data['coverage']['available_steps'])})
            if mapped and item.get('step') not in mapped and not final_alias:
                errors.append(f"evidence {item['id']}: step differs from runtime source alignment: {source}")
    if not claimed_images <= {s for s in fully_delivered if indexed[s].get("kind") == "image"}:
        errors.append("claimed image review absent from complete runtime delivery events")
    for step in coverage["log_steps_read"]:
        logs = {s for s, item in indexed.items() if item.get("kind") == "log" and step in item["steps"]}
        if not logs or not logs <= fully_delivered:
            errors.append(f"claimed log review lacks complete runtime reads for Step {step}")
    if coverage["scan_complete"]:
        if receipt.get("inventory_complete") is not True:
            errors.append("complete review needs complete runtime inventory")
        if not set(indexed) <= fully_delivered:
            errors.append("complete review lacks complete runtime access events")
        images = {s for s, item in indexed.items() if item.get("kind") == "image"}
        if images != source_set(coverage["screenshot_sources_available"]):
            errors.append("runtime image inventory differs from claimed complete image index")
        log_steps = {n for item in indexed.values() if item.get("kind") == "log" for n in item["steps"]}
        if log_steps != set(coverage["available_steps"]):
            errors.append("runtime log inventory differs from available decision steps")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--access-receipt", type=Path, help="Caller-owned Harness read/delivery receipt; requires --evidence-root")
    args = parser.parse_args()
    try:
        errors = validate_result(load_json(args.result), evidence_root=args.evidence_root, access_receipt=args.access_receipt)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if errors:
        print(f"INVALID: {len(errors)} issue(s)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID: schema, template paths/hashes, steps and references passed; causal correctness not assessed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
