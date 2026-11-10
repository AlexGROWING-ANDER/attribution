"""Compile caller-owned metadata and short source IDs without changing causes."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from jsonschema import Draft202012Validator


class ProtocolError(ValueError):
    pass


def strict_json(text: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def nonfinite(value):
        raise ProtocolError(f"Non-finite JSON value: {value}")
    value = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ProtocolError("Output must be a JSON object")
    return value


def parse_wire_json(text: str) -> tuple[dict, list[dict]]:
    """Remove an unambiguous outer envelope without changing JSON values.

    Format noncompliance remains audited. Multiple objects, malformed objects,
    duplicate keys and structured non-object envelopes are never repaired.
    """
    try:
        return strict_json(text), []
    except (ValueError, TypeError):
        match = re.fullmatch(r'```(?:json)?\r?\n([\s\S]*?)\r?\n```', text.strip(), re.IGNORECASE)
        if match is not None:
            value = strict_json(match.group(1))
            return value, [{'kind':'single_json_code_fence_removed','json_values_changed':False,
                        'reason':'Exact outer envelope only; original response retained; raw format violation counted'}]
        if not isinstance(text,str):raise
        if '```' in text:
            matches=list(re.finditer(r'```(?:json)?\r?\n([\s\S]*?)\r?\n```',text,re.IGNORECASE))
            if len(matches)!=1 or text.count('```')!=2:raise ProtocolError('Ambiguous JSON code blocks')
            block=matches[0];prefix,suffix=text[:block.start()],text[block.end():]
            body=block.group(1)
        else:
            if text.lstrip().startswith(('[','"')):raise ProtocolError('Structured envelope is not an object')
            start=text.find('{')
            if start<0:raise ProtocolError('No JSON object in response')
            _,end=json.JSONDecoder().raw_decode(text[start:])
            prefix,body,suffix=text[:start],text[start:start+end],text[start+end:]
        if any(char in prefix+suffix for char in '{}'):
            raise ProtocolError('Multiple or ambiguous JSON objects in response')
        value=strict_json(body)
        return value,[{'kind':'single_json_prose_envelope_removed','json_values_changed':False,
                       'raw_format_violation':True,'prefix_characters':len(prefix),
                       'suffix_characters':len(suffix),'envelope_sha256':digest([prefix,suffix]),
                       'reason':'One complete object only; original response and format violation retained'}]


def canonical_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("Empty source path")
    text = value.replace("\\", "/")
    if text.startswith("/") or PureWindowsPath(value).drive or ":" in text or ".." in text.split("/") or "\0" in text:
        raise ProtocolError("Source must stay inside the attempt")
    text = PurePosixPath(text).as_posix()
    if text == ".":
        raise ProtocolError("Source must name a file")
    return text


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def protected_judgments(data: dict) -> dict:
    """These values are never editable by protocol repair, even if GT disagrees."""
    return {"attribution": data["attribution"], "error_candidates": data["error_candidates"],
            "goals": data["goals"], "preconditions": data["preconditions"], "evidence": data["evidence"],
            "completion": data["outcome"]["completion"],
            "contamination_impact": data["contamination"]["impact"], "contamination_provenance": data["contamination"]["provenance"]}


class SourceCatalog:
    def __init__(self, records: list[dict]):
        seen = set()
        self.entries = []
        for record in sorted(records, key=lambda r: canonical_path(r["source"])):
            source = canonical_path(record["source"])
            if source in seen:
                raise ProtocolError("Duplicate canonical source: " + source)
            seen.add(source)
            if record["kind"] not in ("log", "image", "metadata"):
                raise ProtocolError("Unknown catalog kind")
            step = record.get('step')
            if step is not None and (type(step) is not int or step < 1):
                raise ProtocolError('Invalid catalog step')
            if 'steps' in record:
                mapped = record['steps']
                if (step is not None or not isinstance(mapped, list) or not mapped
                        or any(type(n) is not int or n < 1 for n in mapped) or len(set(mapped))!=len(mapped)):
                    raise ProtocolError('Invalid catalog steps; use unique positive steps with step=null')
            self.entries.append({**record, "source": source, "source_id": f"S{len(self.entries)+1:04d}"})
        self.by_id = {r["source_id"]: r for r in self.entries}
        self.by_path = {r["source"]: r for r in self.entries}

    @property
    def image_paths(self) -> list[str]:
        return [r["source"] for r in self.entries if r["kind"] == "image"]

    @property
    def image_ids(self) -> list[str]:
        return [r["source_id"] for r in self.entries if r["kind"] == "image"]

    def encode(self, source: str) -> str:
        path = canonical_path(source)
        if path not in self.by_path:
            raise ProtocolError("Unknown source; no fuzzy correction allowed: " + path)
        return self.by_path[path]["source_id"]

    def decode(self, source_id: str) -> str:
        if source_id not in self.by_id:
            raise ProtocolError("Unknown source ID: " + str(source_id))
        return self.by_id[source_id]["source"]

    def verify_files(self, root: Path) -> None:
        root = root.resolve()
        for entry in self.entries:
            path = (root / entry["source"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ProtocolError("Missing/outside source: " + entry["source"])
            if entry.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ProtocolError("Source hash mismatch: " + entry["source"])


class OutputProtocol:
    STATIC_COVERAGE = ("total_steps", "available_steps", "screenshots_available", "screenshot_sources_available", "screenshots_index_complete")

    def __init__(self, canonical_schema: dict, identity: dict, skill_version: str,
                 catalog: SourceCatalog, steps: list[int], image_index_complete: bool = True):
        self.schema = copy.deepcopy(canonical_schema)
        self.identity, self.skill_version = copy.deepcopy(identity), skill_version
        self.catalog = catalog
        if len(set(steps)) != len(steps) or any(type(n) is not int or n < 1 for n in steps):
            raise ProtocolError("Invalid decision step inventory")
        self.steps = sorted(steps)
        self.image_index_complete = image_index_complete

    def wire_schema(self) -> dict:
        schema = copy.deepcopy(self.schema)
        for key in ("schema_version", "skill_version", "case"):
            schema["properties"].pop(key)
            schema["required"].remove(key)
        coverage = schema["properties"]["coverage"]
        for key in self.STATIC_COVERAGE:
            coverage["properties"].pop(key)
            coverage["required"].remove(key)
        coverage["properties"]["screenshots_viewed"]["items"] = ({"type": "string", "enum": self.catalog.image_ids}
                                                                    if self.catalog.image_ids else False)
        evidence = schema["properties"]["evidence"]["items"]
        evidence["properties"].pop("source")
        evidence["required"].remove("source")
        evidence["properties"]["source_id"] = ({"type": "string", "enum": list(self.catalog.by_id)}
                                                  if self.catalog.by_id else False)
        evidence["required"].append("source_id")
        return schema

    def project_legacy(self, raw: dict) -> dict:
        if raw["case"] != self.identity or raw["skill_version"] != self.skill_version:
            raise ProtocolError("Result identity/version differs from bound attempt; cannot transplant it")
        data = copy.deepcopy(raw)
        for key in ("schema_version", "skill_version", "case"):
            data.pop(key)
        for key in self.STATIC_COVERAGE:
            data["coverage"].pop(key)
        data["coverage"]["screenshots_viewed"] = [self.catalog.encode(s) for s in data["coverage"]["screenshots_viewed"]]
        for evidence in data["evidence"]:
            evidence["source_id"] = self.catalog.encode(evidence.pop("source"))
        return data



    def compile(self, projection: dict) -> dict:
        _schema = self.wire_schema()
        projection = prune_unknown_properties(copy.deepcopy(projection), _schema)
        errors = sorted(Draft202012Validator(_schema).iter_errors(projection), key=lambda e: str(e.path))
        if errors:
            raise ProtocolError("Wire schema: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8]))
        data = copy.deepcopy(projection)
        data.update(schema_version="1.0", skill_version=self.skill_version, case=copy.deepcopy(self.identity))
        coverage = data["coverage"]
        coverage.update(total_steps=len(self.steps), available_steps=self.steps.copy(),
                        screenshots_available=len(self.catalog.image_paths), screenshot_sources_available=self.catalog.image_paths,
                        screenshots_index_complete=self.image_index_complete)
        coverage["screenshots_viewed"] = [self.catalog.decode(s) for s in coverage["screenshots_viewed"]]
        for evidence in data["evidence"]:
            source_id = evidence.pop("source_id")
            if (evidence["kind"] == "screenshot") != (self.catalog.by_id[source_id]["kind"] == "image"):
                raise ProtocolError("Evidence kind/source kind mismatch")
            origin = self.catalog.by_id[source_id]
            mapped = set(origin.get('steps', []))
            if origin.get('step') is not None:
                mapped.add(origin['step'])
            final_alias = (origin.get('phase')=='final_observation' and evidence.get('phase')=='final_observation'
                           and bool(self.steps) and evidence.get('step')==max(self.steps)+1 and mapped=={max(self.steps)})
            if mapped and evidence.get('step') not in mapped and not final_alias:
                raise ProtocolError('Evidence step/source alignment mismatch: '+source_id)
            evidence["source"] = self.catalog.decode(source_id)
        return data

    def repair_schema(self, data: dict) -> dict:
        coverage = data["coverage"]
        can_complete = (set(coverage["log_steps_read"]) == set(self.steps)
                        and set(coverage["screenshots_viewed"]) == set(self.catalog.image_paths)
                        and self.image_index_complete and not coverage["missing"] and not coverage["truncations"])
        decisions = list(self.schema['properties']['outcome']['properties']['decision']['enum'])
        if data['outcome']['completion']=='success':
            decisions = [x for x in decisions if x not in ('fail','precondition_blocked')]
        if not can_complete:
            decisions = [x for x in decisions if x!='success']
        return {"type": "object", "additionalProperties": False, "properties": {
            "decision": {"type": "string", "enum": decisions},
            "contamination_status": {"type": "string", "enum": ["present", "suspected", "not_observed", "unknown"]},
            "reset_requirements": {"type": "array", "uniqueItems": True, "items":
                                   {"type": "string", "enum": data["contamination"]["reset_requirements"]}
                                   if data["contamination"]["reset_requirements"] else False},
            "scan_complete": {"type": "boolean", **({"const": False} if not can_complete else {})},
            "reason": {"type": "string", "minLength": 1}},
            "required": ["decision", "contamination_status", "reset_requirements", "scan_complete", "reason"]}

    def apply_repair(self, data: dict, patch: dict, validator: Callable[[dict], list[str]]) -> tuple[dict, dict]:
        errors = list(Draft202012Validator(self.repair_schema(data)).iter_errors(patch))
        if errors:
            raise ProtocolError("Repair schema: " + "; ".join(e.message for e in errors))
        before = digest(protected_judgments(data))
        revised = copy.deepcopy(data)
        basis = (f"输出协议处理：completion={data['outcome']['completion']}；decision={patch['decision']}；"
                 f"scan_complete={str(patch['scan_complete']).lower()}。"
                 "原有完整性缺口、证据和因果判断保留。原分析说明与模型修复说明另存供审阅；此处为程序生成的处置说明。")
        revised["outcome"].update(decision=patch["decision"], basis=basis)
        revised["contamination"].update(status=patch["contamination_status"], reset_requirements=patch["reset_requirements"])
        revised["coverage"]["scan_complete"] = patch["scan_complete"]
        if digest(protected_judgments(revised)) != before:
            raise ProtocolError("Repair altered protected causal judgments")
        remaining = validator(revised)
        if remaining:
            raise ProtocolError("Quarantined inconsistent output: " + "; ".join(remaining))
        return revised, {"protected_judgments_sha256": before, "causal_judgments_unchanged": True,
                         "original_basis": data['outcome']['basis'], "basis_generated_by_caller": True,
                         "patch": patch, "changed_fields": ["outcome.decision/basis", "contamination.status/reset_requirements",
                                                               "coverage.scan_complete"],
                         "gt_used": False, "scope": "Common output-contract consistency only; factual correctness not established"}


def repair_instruction() -> str:
    return (
        "Resolve output-contract inconsistencies only. Return the supplied small JSON schema, no prose/fences. "
        "No GT is available. Do not reassess, rewrite or optimize Initial, Recovery, candidates, steps, goals, completion, "
        "or evidence observations. Fields omitted from this patch cannot change. "
        "The original uncertainty details are frozen. The caller generates procedural basis text, so do not supply basis or uncertainty text. "
        "Success disposition requires complete review. Completion can stay success while disposition is pending. "
        "Historical state observed is not confirmed contamination unless it violates the required baseline and affects this task. "
        "If the retained impact states no effect, do not confirm contamination just because history exists. "
        "Retain real material gaps, do not invent reads, delete uncertainty, add evidence or invent reset needs to pass validation. "
        "If you cannot resolve these fields with the existing facts, explain that in reason and retain pending where legal."
    )


def procedural_view(data: dict, errors: list[str]) -> dict:
    """Only the fields needed for a procedural patch; never sends causal labels or GT."""
    return copy.deepcopy({"validation_errors": errors, "outcome": {
        key: data["outcome"][key] for key in ("completion", "decision", "basis")},
        "coverage": data["coverage"], "contamination": data["contamination"]})


def bind_output_schema(request: dict, schema: dict, mode: str = "prompt", name: str = "attribution_output") -> dict:
    """Portable JSON defaults to local enforcement; server grammar is opt-in.

    Acceptance of a small schema does not establish support for a full nullable
    attribution schema. Never silently retry a rejected POST in a different mode.
    """
    payload = copy.deepcopy(request)
    if mode == "server":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": name, "strict": True, "schema": copy.deepcopy(schema)}}
    elif mode == "prompt":
        payload.pop("response_format", None)
        payload["messages"] = [*payload["messages"], {"role": "user", "content": (
            "Public output contract, identical across experimental groups. Return one complete JSON object, no Markdown/code fences. "
            "The following wire schema is enforced strictly by the caller; provider response_format is omitted for compatibility. "
            "Do not add fields outside the schema. This concerns serialization only, not causal conclusions.\n" +
            json.dumps(schema, ensure_ascii=False))}]
    else:
        raise ProtocolError("Unknown output-schema mode")
    return payload


def normalize_unknown_phase(projection: dict, schema: dict) -> tuple[dict, list[dict]]:
    """Unknown wire annotation falls back to unknown; never infer before/after action.

    Original phase strings are retained in the caller audit. This does not repair
    labels, steps, source IDs, evidence facts or other invalid fields.
    """
    data = copy.deepcopy(projection)
    allowed = schema['properties']['evidence']['items']['properties']['phase']['enum']
    if 'unknown' not in allowed:
        raise ProtocolError('Schema has no conservative unknown phase')
    audit = []
    for index, item in enumerate(data.get('evidence', [])):
        if isinstance(item, dict) and isinstance(item.get('phase'), str) and item['phase'] not in allowed:
            audit.append({'path':f'evidence[{index}].phase','evidence_id':item.get('id'),
                          'source_id':item.get('source_id'),'original':item['phase'],'normalized':'unknown',
                          'reason':'Invalid phase enum; no temporal alignment inferred', 'observation_changed':False})
            item['phase'] = 'unknown'
    return data, audit


def procedural_errors_only(errors: list[str]) -> bool:
    """Do not spend a formatting call on unsupported goals, labels or evidence."""
    allowed = ('success decision requires complete review',
               'affected baseline needs rerun, pending, precondition_blocked',
               'scan_complete claimed with missing or unreadable materials',
               'completion/decision conflict')
    return bool(errors) and all(any(message.startswith(prefix) for prefix in allowed) for message in errors)


def deduplicate_read_claims(projection: dict) -> tuple[dict, list[dict]]:
    """Remove repeated claims only. The set of claimed reads can never increase."""
    data = copy.deepcopy(projection)
    audit = []
    for field, kind in (('screenshots_viewed', str), ('log_steps_read', int)):
        values = data.get('coverage', {}).get(field)
        if not isinstance(values, list) or not all(type(v) is kind for v in values):
            continue
        unique = list(dict.fromkeys(values))
        if len(unique) != len(values):
            audit.append({'field':'coverage.'+field,'original_count':len(values),'unique_count':len(unique),
                          'new_reads_added':False,'reason':'Repeated identical read claims removed; claimed read set unchanged'})
            data['coverage'][field] = unique
    return data, audit


def drop_empty_evidence_annotations(projection: dict, schema: dict) -> tuple[dict, list[dict]]:
    """Only empty-string unknown evidence annotations; never drop facts or repair names."""
    data = copy.deepcopy(projection)
    allowed = schema['properties']['evidence']['items']['properties']
    audit = []
    for index, item in enumerate(data.get('evidence', [])):
        if not isinstance(item, dict):
            continue
        for key in list(item):
            if key not in allowed and item[key] == '':
                audit.append({'path':f'evidence[{index}].{key}', 'original':'',
                              'action':'removed_unknown_empty_annotation',
                              'facts_or_required_fields_changed':False})
                del item[key]
    return data, audit


def prune_unknown_properties(instance, schema):
    """按 schema 的 properties 递归删掉未声明的键；已声明字段的值一律不动。

    模型偶尔自带 cause_id / stage 之类冗余字段，wire schema 是严格集合，
    一处多余就整体报错。这里只做"删多余键"，不补字段、不改取值，
    缺字段仍然照常报错。
    """
    if not isinstance(schema, dict):
        return instance
    if isinstance(instance, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            for key in list(instance):
                if key not in props:
                    del instance[key]
            for key, sub in props.items():
                if key in instance:
                    instance[key] = prune_unknown_properties(instance[key], sub)
        else:
            ap = schema.get("additionalProperties")
            if isinstance(ap, dict):
                for key in list(instance):
                    instance[key] = prune_unknown_properties(instance[key], ap)
    elif isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            instance = [prune_unknown_properties(x, items) for x in instance]
    return instance
