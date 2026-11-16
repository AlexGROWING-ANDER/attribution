"""Compile and load framework architecture artifacts without framework-specific rules."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


ARCHITECTURE_SCHEMA = "framework-architecture-v1"
ARTIFACT_SCHEMA = "framework-architecture-artifact-v1"
BINDING_SCHEMA = "framework-architecture-binding-v1"
GENERATOR = {"name": "python-static-architecture", "version": "3"}
EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "build", "dist", "__pycache__", "tests", "test", "testing",
}
MAX_SOURCE_BYTES = 1_000_000
MAX_NODES = 160
MAX_MODULES = 160
MAX_IMPORTS = 40
MAX_FIELDS_PER_COMPONENT = 80
ACTION_CONTRACT_FIELDS = {
    "action", "actions", "action_pool", "action_schema", "action_contract",
    "action_types", "available_actions", "action_names", "action_arguments",
}
MAX_SOURCE_EXCERPT = 12_000
MAX_PROMPT_EXCERPT = 1_600
MAX_PROMPT_EXCERPTS = 6
MAX_CONSTANT_TEXT = 2_000


class ArchitectureError(ValueError):
    """A source, artifact, or binding failed deterministic validation."""


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _tree_digest(files: dict[str, str]) -> str:
    payload = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )
    return completed.stdout


def _is_git_repository(repo: Path) -> bool:
    """Accept a worktree root or tracked subtree, not an unrelated material copy."""
    try:
        if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
            return False
        top = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
        return top == repo.resolve() or bool(_git(repo, "ls-files", "--", ".").strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _matches(relative: str, source_roots: list[str] | None,
             include: list[str] | None, exclude: list[str] | None) -> bool:
    normalized = PurePosixPath(relative).as_posix()
    roots = [PurePosixPath(value).as_posix().rstrip("/") for value in (source_roots or [])]
    if roots and not any(normalized == root or normalized.startswith(root + "/") for root in roots):
        return False
    includes = include or ["**/*.py"]
    if not any(fnmatch.fnmatchcase(normalized, pattern) for pattern in includes):
        return False
    if any(fnmatch.fnmatchcase(normalized, pattern) for pattern in (exclude or [])):
        return False
    return not any(part in EXCLUDED_PARTS for part in PurePosixPath(normalized).parts)


def python_sources(repo: Path, source_roots: list[str] | None = None,
                   include: list[str] | None = None,
                   exclude: list[str] | None = None) -> list[str]:
    """Return deterministic, regular, in-repository Python source paths."""
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ArchitectureError("Source repository is not a directory")
    if _is_git_repository(repo):
        candidates = _git(repo, "ls-files", "--", "*.py").splitlines()
    else:
        candidates = [path.relative_to(repo).as_posix() for path in repo.rglob("*.py")]
    selected = []
    for relative in sorted(set(candidates)):
        path = repo / relative
        if not _matches(relative, source_roots, include, exclude):
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(repo) or path.is_symlink() or not path.is_file():
            raise ArchitectureError("Source path escapes or links outside the repository: " + relative)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ArchitectureError("Cannot stat source file: " + relative) from exc
        if size > MAX_SOURCE_BYTES:
            raise ArchitectureError("Source file exceeds the supported size: " + relative)
        selected.append(relative)
    if not selected:
        raise ArchitectureError("No eligible Python source files found")
    return selected


def _source_revision(repo: Path, files: dict[str, str]) -> str:
    digest = _tree_digest(files)
    if _is_git_repository(repo):
        try:
            head = _git(repo, "rev-parse", "HEAD").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArchitectureError("Cannot resolve repository revision") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise ArchitectureError("Repository HEAD is not a fixed revision")
        return head
    return "content-" + digest[:20]


def _dirty_sources(repo: Path, sources: set[str]) -> list[str]:
    if not _is_git_repository(repo):
        return []
    try:
        status = _git(repo, "-c", "status.relativePaths=true", "status",
                      "--short", "--untracked-files=no", "--", ".")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchitectureError("Cannot inspect repository working tree") from exc
    dirty = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        relative = line[3:]
        if " -> " in relative:
            relative = relative.rsplit(" -> ", 1)[1]
        relative = relative.strip().strip('"').replace("\\", "/")
        if relative in sources:
            dirty.append(relative)
    return sorted(set(dirty))


def _first_paragraph(value: str | None) -> str:
    if not value:
        return ""
    paragraphs = re.split(r"\n\s*\n", value.strip())
    return re.sub(r"\s+", " ", paragraphs[0]).strip()


def _signature_arg(arg: ast.arg) -> dict:
    annotation = ast.unparse(arg.annotation) if arg.annotation is not None else None
    return {"name": arg.arg, "annotation": annotation}


def _arguments(arguments: ast.arguments) -> list[dict]:
    positional = list(arguments.posonlyargs) + list(arguments.args)
    values = [_signature_arg(value) for value in positional]
    if arguments.vararg is not None:
        values.append({"name": "*" + arguments.vararg.arg,
                       "annotation": ast.unparse(arguments.vararg.annotation)
                       if arguments.vararg.annotation is not None else None})
    values.extend(_signature_arg(value) for value in arguments.kwonlyargs)
    if arguments.kwarg is not None:
        values.append({"name": "**" + arguments.kwarg.arg,
                       "annotation": ast.unparse(arguments.kwarg.annotation)
                       if arguments.kwarg.annotation is not None else None})
    return values


def _return_annotation(node: ast.AST) -> str | None:
    value = getattr(node, "returns", None)
    return ast.unparse(value) if value is not None else None


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        function = item.func
        if isinstance(function, ast.Name):
            names.add(function.id)
        elif isinstance(function, ast.Attribute):
            names.add(function.attr)
        elif isinstance(function, ast.Subscript):
            value = function.value
            if isinstance(value, ast.Name):
                names.add(value.id)
            elif isinstance(value, ast.Attribute):
                names.add(value.attr)
    return names


def _import_names(tree: ast.Module) -> list[str]:
    values = set()
    for item in tree.body:
        if isinstance(item, ast.Import):
            values.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            values.add("." * item.level + (item.module or ""))
    return sorted(value for value in values if value)[:MAX_IMPORTS]


def _references(node: ast.AST) -> set[str]:
    values: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            values.add(item.id)
        elif isinstance(item, ast.Attribute):
            values.add(item.attr)
    return values


def _long_strings(node: ast.AST) -> list[dict]:
    values = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            continue
        text = re.sub(r"\s+", " ", item.value).strip()
        if len(text) < 100:
            continue
        values.append({
            "line": getattr(item, "lineno", None),
            "text": text[:MAX_PROMPT_EXCERPT],
            "truncated": len(text) > MAX_PROMPT_EXCERPT,
        })
    unique = {}
    for value in values:
        unique[(value["line"], value["text"])] = value
    return sorted(unique.values(), key=lambda value: (-len(value["text"]), value["line"] or 0))[:MAX_PROMPT_EXCERPTS]


def _literal(value: ast.AST):
    try:
        item = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError):
        return None
    if isinstance(item, str):
        return item[:MAX_CONSTANT_TEXT]
    if isinstance(item, (int, float, bool)) or item is None:
        return item
    if isinstance(item, (list, tuple, dict)):
        text = repr(item)
        return text[:MAX_CONSTANT_TEXT] if len(text) > MAX_CONSTANT_TEXT else item
    return None


def _assignments(body: list[ast.stmt]) -> list[dict]:
    values = []
    for item in body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            values.append({
                "name": item.target.id,
                "annotation": ast.unparse(item.annotation) if item.annotation is not None else None,
                "value": _literal(item.value) if item.value is not None else None,
                "line": getattr(item, "lineno", None),
            })
        elif isinstance(item, ast.Assign):
            value = _literal(item.value)
            for target in item.targets:
                if isinstance(target, ast.Name):
                    values.append({
                        "name": target.id,
                        "annotation": None,
                        "value": value,
                        "line": getattr(item, "lineno", None),
                    })
    return values


def _self_attribute(target: ast.AST) -> str | None:
    if (isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"):
        return target.attr
    return None


def _self_targets(target: ast.AST) -> list[tuple[ast.Attribute, str]]:
    name = _self_attribute(target)
    if name is not None:
        return [(target, name)]
    if isinstance(target, (ast.Tuple, ast.List)):
        values = []
        for item in target.elts:
            values.extend(_self_targets(item))
        return values
    return []


def _instance_fields(node: ast.ClassDef) -> list[dict]:
    """Extract instance state assigned through ``self`` inside class methods."""
    fields: dict[str, dict] = {}
    write_targets: set[int] = set()
    methods = [
        item for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for method in methods:
        for item in ast.walk(method):
            if isinstance(item, ast.Assign):
                targets, value = item.targets, item.value
                annotation = None
            elif isinstance(item, ast.AnnAssign):
                targets, value = [item.target], item.value
                annotation = ast.unparse(item.annotation) if item.annotation is not None else None
            elif isinstance(item, ast.AugAssign):
                targets, value, annotation = [item.target], None, None
            else:
                continue
            for target in targets:
                for attribute, name in _self_targets(target):
                    if not isinstance(item, ast.AugAssign):
                        write_targets.add(id(attribute))
                    entry = fields.setdefault(name, {
                        "name": name,
                        "annotation": None,
                        "value": None,
                        "line": item.lineno,
                        "scope": "instance",
                        "written_in": set(),
                        "read_in": set(),
                    })
                    entry["written_in"].add(method.name)
                    entry["line"] = min(entry["line"], item.lineno)
                    if annotation and not entry["annotation"]:
                        entry["annotation"] = annotation
                    direct_target = _self_attribute(target) is not None
                    if (method.name == "__init__" and direct_target and value is not None
                            and not entry.get("value_found")):
                        literal = _literal(value)
                        if literal is not None or (isinstance(value, ast.Constant) and value.value is None):
                            entry["value"] = literal
                            entry["value_found"] = True
    for method in methods:
        for item in ast.walk(method):
            if not isinstance(item, ast.Attribute) or id(item) in write_targets:
                continue
            name = _self_attribute(item)
            if name in fields:
                fields[name]["read_in"].add(method.name)
    values = []
    for entry in sorted(fields.values(), key=lambda value: (value["line"], value["name"])):
        entry.pop("value_found", None)
        values.append({
            **entry,
            "written_in": sorted(entry["written_in"]),
            "read_in": sorted(entry["read_in"]),
        })
    return values[:MAX_FIELDS_PER_COMPONENT]


def _source_excerpt(repo: Path, relative: str, start: int, end: int) -> dict:
    lines = (repo / relative).read_text(encoding="utf-8-sig").splitlines()
    text = "\n".join(lines[max(0, start - 1):min(len(lines), end)])
    return {
        "path": relative,
        "start_line": start,
        "end_line": end,
        "text": text[:MAX_SOURCE_EXCERPT],
        "truncated": len(text) > MAX_SOURCE_EXCERPT,
    }


def _symbol_id(path: str, name: str) -> str:
    stem = path[:-3].replace("/", ".") if path.endswith(".py") else path
    return f"{stem}:{name}"


def _methods(node: ast.ClassDef) -> list[dict]:
    values = []
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        values.append({
            "name": child.name,
            "summary": _first_paragraph(ast.get_docstring(child)),
            "arguments": _arguments(child.args),
            "returns": _return_annotation(child),
            "decorators": [ast.unparse(value) for value in child.decorator_list],
            "is_async": isinstance(child, ast.AsyncFunctionDef),
            "start_line": child.lineno,
            "end_line": getattr(child, "end_lineno", child.lineno),
            "calls": sorted(_call_names(child)),
        })
    return values


def compile_repository(repo: Path, framework: str, revision: str | None = None,
                       source_roots: list[str] | None = None,
                       include: list[str] | None = None,
                       exclude: list[str] | None = None) -> dict:
    """Compile a deterministic architecture view from a pinned source tree."""
    if not isinstance(framework, str) or not framework.strip():
        raise ArchitectureError("Framework identifier is required")
    repo = Path(repo).resolve()
    paths = python_sources(repo, source_roots, include, exclude)
    files = {relative: sha256(repo / relative) for relative in paths}
    observed_revision = _source_revision(repo, files)
    if revision is not None and revision != observed_revision:
        raise ArchitectureError(
            "Repository revision does not match the requested revision: "
            + observed_revision + " != " + revision
        )
    dirty = _dirty_sources(repo, set(paths))
    if dirty:
        raise ArchitectureError(
            "Selected source files have uncommitted changes: " + ", ".join(dirty[:8])
        )

    definitions: list[dict] = []
    module_constants: list[dict] = []
    modules: list[dict] = []
    module_calls: set[str] = set()
    parsed_files = 0
    syntax_errors = []
    for relative in paths:
        try:
            tree = ast.parse((repo / relative).read_text(encoding="utf-8-sig"), filename=relative)
        except (SyntaxError, UnicodeError) as exc:
            syntax_errors.append({"path": relative, "error": str(exc)[:300]})
            continue
        parsed_files += 1
        module_calls.update(_call_names(tree))
        summary = _first_paragraph(ast.get_docstring(tree))
        component_count = sum(
            isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            for item in tree.body
        )
        if summary:
            summary_source = "docstring"
        elif component_count:
            suffix = "s" if component_count != 1 else ""
            summary = f"Defines {component_count} top-level component{suffix}."
            summary_source = "component_count"
        else:
            summary_source = "none"
        modules.append({
            "path": relative,
            "summary": summary,
            "summary_source": summary_source,
            "imports": _import_names(tree),
            "component_count": component_count,
        })
        for item in tree.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                assignment = _assignments([item])
                for value in assignment:
                    value["path"] = relative
                    module_constants.append(value)
            if not isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods = _methods(item) if isinstance(item, ast.ClassDef) else []
            class_fields = _assignments(item.body) if isinstance(item, ast.ClassDef) else []
            for value in class_fields:
                value["scope"] = "class"
            class_field_names = {value["name"] for value in class_fields}
            instance_fields = [
                value for value in (_instance_fields(item) if isinstance(item, ast.ClassDef) else [])
                if value["name"] not in class_field_names
            ]
            fields = class_fields + instance_fields
            fields.sort(key=lambda value: (value.get("line") or 0, value["name"]))
            constants = [
                value for value in fields
                if isinstance(value.get("value"), str)
            ]
            definitions.append({
                "id": _symbol_id(relative, item.name),
                "name": item.name,
                "kind": "class" if isinstance(item, ast.ClassDef) else "function",
                "path": relative,
                "start_line": item.lineno,
                "end_line": getattr(item, "end_lineno", item.lineno),
                "summary": _first_paragraph(ast.get_docstring(item)),
                "decorators": [ast.unparse(value) for value in item.decorator_list],
                "bases": [ast.unparse(value) for value in item.bases]
                if isinstance(item, ast.ClassDef) else [],
                "arguments": _arguments(item.args) if not isinstance(item, ast.ClassDef) else [],
                "returns": _return_annotation(item) if not isinstance(item, ast.ClassDef) else None,
                "is_async": isinstance(item, ast.AsyncFunctionDef),
                "methods": methods,
                "fields": fields,
                "referenced_names": sorted(_references(item)),
                "calls": sorted(_call_names(item)),
                "literal_constants": constants,
                "prompt_excerpts": _long_strings(item),
                "source": _source_excerpt(repo, relative, item.lineno,
                                          getattr(item, "end_lineno", item.lineno)),
            })

    if not definitions:
        raise ArchitectureError("No class or function components were found")

    by_name: dict[str, list[dict]] = {}
    for definition in definitions:
        by_name.setdefault(definition["name"], []).append(definition)

    edges: list[dict] = []
    outgoing: dict[str, set[str]] = {definition["id"]: set() for definition in definitions}
    incoming: dict[str, set[str]] = {definition["id"]: set() for definition in definitions}
    seen_edges = set()
    for definition in definitions:
        candidates = [(name, target) for name in definition["calls"]
                      for target in by_name.get(name, [])]
        bases = [base.split(".")[-1] for base in definition["bases"]]
        candidates.extend((base, target) for base in bases
                          for target in by_name.get(base, []))
        for label, target in candidates:
            if target["id"] == definition["id"]:
                continue
            kind = "inherits" if label in bases else "calls"
            edge = (definition["id"], target["id"], kind)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            edges.append({"from": edge[0], "to": edge[1], "kind": kind})
            outgoing[definition["id"]].add(target["id"])
            incoming[target["id"]].add(definition["id"])

    def score(definition: dict) -> tuple[int, int, int, int, int]:
        return (
            len(incoming[definition["id"]]) + len(outgoing[definition["id"]]),
            len(definition["methods"]),
            len(definition["fields"]),
            len(definition["summary"]),
            definition["end_line"] - definition["start_line"],
        )

    generic_entry_names = {"main", "run"}
    entry_ids = {
        definition["id"] for definition in definitions
        if definition["kind"] == "function" and (
            definition["name"] in generic_entry_names
            or definition["name"].startswith("run_")
            or (definition["name"] in module_calls and not incoming[definition["id"]])
            or (not incoming[definition["id"]] and outgoing[definition["id"]])
        )
    }
    ranked = sorted(
        definitions,
        key=lambda item: (
            item["id"] not in entry_ids,
            -score(item)[0], -score(item)[1], -score(item)[2],
            item["path"], item["start_line"],
        ),
    )
    selected = ranked[:MAX_NODES]
    selected_ids = {item["id"] for item in selected}
    nodes = []
    for item in sorted(selected, key=lambda value: (value["path"], value["start_line"], value["name"])):
        nodes.append({key: item[key] for key in (
            "id", "name", "kind", "path", "start_line", "end_line", "summary",
            "decorators", "bases", "arguments", "returns", "is_async", "methods",
            "fields", "referenced_names", "literal_constants", "prompt_excerpts", "source",
        )})

    selected_edges = [
        edge for edge in sorted(edges, key=lambda value: (value["from"], value["to"], value["kind"]))
        if edge["from"] in selected_ids and edge["to"] in selected_ids
    ]
    reference_counts: dict[str, int] = {}
    for definition in definitions:
        if definition["id"] not in selected_ids:
            continue
        for name in set(definition["referenced_names"]):
            for target in by_name.get(name, []):
                if target["id"] != definition["id"]:
                    reference_counts[target["id"]] = reference_counts.get(target["id"], 0) + 1
    node_by_id = {node["id"]: node for node in nodes}
    shared_state = sorted(
        [identifier for identifier in selected_ids
         if reference_counts.get(identifier, 0) >= 2
         and node_by_id[identifier]["fields"]],
        key=lambda value: (-reference_counts.get(value, 0), value),
    )
    action_components = []
    for node in nodes:
        named_action = "action" in node["name"].lower()
        method_action = any("action" in method["name"].lower() for method in node["methods"])
        field_action = any(field["name"].lower() in ACTION_CONTRACT_FIELDS for field in node["fields"])
        if named_action or method_action or field_action:
            action_components.append(node["id"])

    return {
        "schema": ARCHITECTURE_SCHEMA,
        "framework": framework,
        "mode": "generated",
        "repo_revision": observed_revision,
        "source_tree_sha256": _tree_digest(files),
        "generator": GENERATOR,
        "source_roots": source_roots or [],
        "modules": sorted(modules, key=lambda value: value["path"])[:MAX_MODULES],
        "nodes": nodes,
        "edges": selected_edges,
        "entrypoints": sorted(entry_ids & selected_ids),
        "shared_state_candidates": shared_state,
        "action_components": action_components,
        "module_constants": sorted(module_constants, key=lambda value: (value["path"], value["line"] or 0, value["name"]))[:300],
        "source_files": files,
        "extraction": {
            "source_file_count": len(files),
            "parsed_file_count": parsed_files,
            "module_count": len(modules),
            "component_count": len(definitions),
            "selected_node_count": len(nodes),
            "node_limit": MAX_NODES,
            "truncated_nodes": max(0, len(definitions) - len(nodes)),
            "syntax_errors": syntax_errors[:20],
        },
        "notes": [
            "Architecture facts are compiled from a content-pinned source tree.",
            "This artifact describes code relationships and contracts only; it is not evidence about a trajectory.",
        ],
    }


def compile_manual(framework: str, overview: str,
                   repo_revision: str | None = None,
                   source_tree_sha256: str | None = None) -> dict:
    text = overview.strip()
    if not text:
        raise ArchitectureError("Manual overview is empty")
    return {
        "schema": ARCHITECTURE_SCHEMA,
        "framework": framework,
        "mode": "manual",
        "repo_revision": repo_revision,
        "source_tree_sha256": source_tree_sha256,
        "generator": {"name": "manual-overview", "version": "1"},
        "source_roots": [],
        "modules": [],
        "nodes": [],
        "edges": [],
        "entrypoints": [],
        "shared_state_candidates": [],
        "action_components": [],
        "module_constants": [],
        "source_files": {},
        "manual_overview": text,
        "extraction": {"source_file_count": 0, "module_count": 0, "component_count": 0,
                       "selected_node_count": 0, "truncated_nodes": 0},
        "notes": ["Manual overview supplied as an evaluation configuration."],
    }


def render_markdown(architecture: dict) -> str:
    lines = [
        f"# {architecture['framework']} framework architecture",
        "",
        f"- Mode: `{architecture['mode']}`",
        f"- Repository revision: `{architecture.get('repo_revision') or 'n/a'}`",
        f"- Source tree SHA-256: `{architecture.get('source_tree_sha256') or 'n/a'}`",
        "",
        "This artifact describes module composition, responsibilities, interfaces, state, and call relationships. It is not evidence about the current attempt and must not be cited as runtime evidence.",
        "",
    ]
    if architecture["mode"] == "manual":
        lines.extend([architecture["manual_overview"].strip(), ""])
        return "\n".join(lines)

    node_by_id = {node["id"]: node for node in architecture["nodes"]}
    lines.extend(["## Entry points", ""])
    if architecture["entrypoints"]:
        for identifier in architecture["entrypoints"]:
            node = node_by_id[identifier]
            lines.append(f"- `{node['name']}` (`{node['path']}:{node['start_line']}`)")
    else:
        lines.append("- No entry-like component was resolved.")
    lines.extend(["", "## Shared state candidates", ""])
    if architecture["shared_state_candidates"]:
        for identifier in architecture["shared_state_candidates"]:
            node = node_by_id[identifier]
            lines.append(f"- `{node['name']}` (`{node['path']}:{node['start_line']}`)")
    else:
        lines.append("- No frequently referenced state-bearing component was resolved.")

    lines.extend(["", "## Source modules", ""])
    modules = architecture.get("modules", [])
    if modules:
        for module in modules:
            line = f"- `{module['path']}`"
            if module.get("summary"):
                line += f": {module['summary']}"
            elif module.get("component_count"):
                line += f": {module['component_count']} top-level components"
            lines.append(line)
            if module.get("imports"):
                lines.append("  Imports: " + ", ".join(f"`{value}`" for value in module["imports"]))
    else:
        lines.append("- No module summary was extracted.")

    lines.extend(["", "## Components", ""])
    for node in architecture["nodes"]:
        lines.append(f"### {node['name']}  ")
        lines.append(f"`{node['path']}:{node['start_line']}-{node['end_line']}`")
        lines.append("")
        if node["bases"]:
            lines.append("Bases: " + ", ".join(f"`{value}`" for value in node["bases"]) + ".")
            lines.append("")
        if node["summary"]:
            lines.append(node["summary"])
            lines.append("")
        if node["decorators"]:
            lines.append("Decorators: " + ", ".join(f"`{value}`" for value in node["decorators"]) + ".")
            lines.append("")
        if node["fields"]:
            lines.append("State / fields:")
            for field in node["fields"][:40]:
                annotation = f": `{field['annotation']}`" if field.get("annotation") else ""
                value = field.get("value")
                rendered = "" if value is None else f" = `{str(value)[:300]}`"
                lifecycle = []
                if field.get("written_in"):
                    lifecycle.append("written in " + ", ".join(f"`{name}`" for name in field["written_in"]))
                if field.get("read_in"):
                    lifecycle.append("read in " + ", ".join(f"`{name}`" for name in field["read_in"]))
                suffix = f" ({'; '.join(lifecycle)})" if lifecycle else ""
                lines.append(f"- `{field['name']}`{annotation}{rendered}{suffix}")
            if len(node["fields"]) > 40:
                lines.append(f"- ... {len(node['fields']) - 40} more fields in architecture.json")
            lines.append("")
        if node["arguments"]:
            args = ", ".join(
                value["name"] + (f": {value['annotation']}" if value.get("annotation") else "")
                for value in node["arguments"]
            )
            lines.append(f"Arguments: `{args}`" + (f" → `{node['returns']}`" if node.get("returns") else "") + ".")
            lines.append("")
        if node["methods"]:
            lines.append("Methods:")
            for method in node["methods"]:
                args = ", ".join(
                    value["name"] + (f": {value['annotation']}" if value.get("annotation") else "")
                    for value in method["arguments"]
                )
                suffix = f" → `{method['returns']}`" if method.get("returns") else ""
                async_prefix = "async " if method.get("is_async") else ""
                lines.append(f"- `{async_prefix}{method['name']}({args})`{suffix}")
                if method["summary"]:
                    lines.append(f"  {method['summary']}")
            lines.append("")
        for excerpt in node["prompt_excerpts"]:
            lines.append(f"Representative source text (line {excerpt['line']}):")
            lines.append("```text")
            lines.append(excerpt["text"])
            lines.append("```")
            lines.append("")

    lines.extend(["## Call and inheritance relationships", ""])
    if architecture["edges"]:
        for edge in architecture["edges"][:500]:
            source = node_by_id.get(edge["from"], {"name": edge["from"]})["name"]
            target = node_by_id.get(edge["to"], {"name": edge["to"]})["name"]
            lines.append(f"- `{source}` --{edge['kind']}--> `{target}`")
        if len(architecture["edges"]) > 500:
            lines.append(f"- ... {len(architecture['edges']) - 500} more relationships in architecture.json")
    else:
        lines.append("- No cross-component relationship was resolved.")
    lines.append("")
    return "\n".join(lines)


def write_artifact(root: Path, architecture: dict) -> dict:
    root = Path(root).resolve()
    if root.exists():
        raise ArchitectureError("Architecture output already exists")
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=root.name + ".", dir=root.parent))
    try:
        architecture_path = temporary / "architecture.json"
        markdown_path = temporary / "architecture.md"
        _write_json(architecture_path, architecture)
        markdown_path.write_text(render_markdown(architecture), encoding="utf-8")
        artifact = {
            "schema": ARTIFACT_SCHEMA,
            "framework": architecture["framework"],
            "mode": architecture["mode"],
            "repo_revision": architecture.get("repo_revision"),
            "source_tree_sha256": architecture.get("source_tree_sha256"),
            "generator": architecture.get("generator"),
            "architecture_sha256": sha256(architecture_path),
            "markdown_sha256": sha256(markdown_path),
        }
        _write_json(temporary / "artifact.json", artifact)
        os.replace(temporary, root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return artifact


def bind_artifact(root: Path, artifact: dict) -> dict:
    root = Path(root).resolve()
    return {
        "schema": BINDING_SCHEMA,
        "framework": artifact["framework"],
        "mode": artifact["mode"],
        "artifact_root": str(root),
        "repo_revision": artifact.get("repo_revision"),
        "source_tree_sha256": artifact.get("source_tree_sha256"),
        "artifact_sha256": sha256(root / "artifact.json"),
        "architecture_sha256": artifact["architecture_sha256"],
        "markdown_sha256": artifact["markdown_sha256"],
    }


def _validate_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ArchitectureError("Framework architecture artifact root is invalid")
    expected = {"artifact.json", "architecture.json", "architecture.md"}
    actual = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if (Path(dirpath) / name).is_symlink():
                raise ArchitectureError("Framework architecture artifact contains a symlink")
        for name in filenames:
            actual.add((Path(dirpath) / name).relative_to(root).as_posix())
    if actual != expected:
        raise ArchitectureError(
            "Framework architecture artifact tree mismatch: missing="
            + str(sorted(expected - actual)) + " extra=" + str(sorted(actual - expected))
        )


def select_framework_binding(value, framework: str):
    """Select a binding from a single binding or a framework-keyed registry."""
    if value is None:
        return None
    if isinstance(value, dict) and value.get("schema") == BINDING_SCHEMA:
        return value
    if isinstance(value, list):
        matches = [item for item in value if isinstance(item, dict) and item.get("framework") == framework]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ArchitectureError("No framework architecture binding for: " + framework)
        raise ArchitectureError("Duplicate framework architecture bindings for: " + framework)
    if isinstance(value, dict):
        candidate = value.get(framework)
        if candidate is None and isinstance(value.get("bindings"), dict):
            candidate = value["bindings"].get(framework)
        if candidate is None:
            raise ArchitectureError("No framework architecture binding for: " + framework)
        return candidate
    raise ArchitectureError("Unsupported framework architecture binding registry")


def resolve_framework_architecture(binding, base, identity):
    if binding is None:
        return None
    if not isinstance(binding, dict) or binding.get("schema") != BINDING_SCHEMA:
        raise ArchitectureError("Unknown framework architecture binding schema")
    framework = identity.get("framework")
    if binding.get("framework") != framework:
        raise ArchitectureError("Framework architecture target differs from case identity")
    if binding.get("mode") not in ("generated", "manual"):
        raise ArchitectureError("Unsupported framework architecture mode")
    root = Path(binding.get("artifact_root", "")).expanduser()
    root = (root if root.is_absolute() else Path(base) / root).resolve()
    _validate_tree(root)
    artifact_path = root / "artifact.json"
    architecture_path = root / "architecture.json"
    markdown_path = root / "architecture.md"
    if sha256(artifact_path) != binding.get("artifact_sha256"):
        raise ArchitectureError("Framework architecture artifact changed")
    artifact = _read_json(artifact_path)
    if artifact.get("schema") != ARTIFACT_SCHEMA or artifact.get("framework") != framework:
        raise ArchitectureError("Framework architecture artifact identity mismatch")
    if artifact.get("mode") != binding.get("mode"):
        raise ArchitectureError("Framework architecture binding metadata mismatch")
    if artifact.get("repo_revision") != binding.get("repo_revision"):
        raise ArchitectureError("Framework architecture revision mismatch")
    if artifact.get("source_tree_sha256") != binding.get("source_tree_sha256"):
        raise ArchitectureError("Framework architecture source digest mismatch")
    if sha256(architecture_path) != artifact.get("architecture_sha256"):
        raise ArchitectureError("Framework architecture JSON changed")
    if sha256(markdown_path) != artifact.get("markdown_sha256"):
        raise ArchitectureError("Framework architecture Markdown changed")
    architecture = _read_json(architecture_path)
    if architecture.get("schema") != ARCHITECTURE_SCHEMA or architecture.get("framework") != framework:
        raise ArchitectureError("Framework architecture content identity mismatch")
    if architecture.get("mode") != binding.get("mode"):
        raise ArchitectureError("Framework architecture content mode mismatch")
    return {
        "artifact_root": str(root),
        "mode": binding["mode"],
        "repo_revision": binding.get("repo_revision"),
        "source_tree_sha256": binding.get("source_tree_sha256"),
        "artifact_sha256": binding["artifact_sha256"],
        "architecture_sha256": artifact["architecture_sha256"],
        "markdown_sha256": artifact["markdown_sha256"],
        "path": ".framework-architecture/architecture.json",
        "markdown_path": ".framework-architecture/architecture.md",
        "authority": "framework architecture reference only; never current-attempt evidence",
    }


def install_framework_architecture(case, workspace, out):
    architecture = case.get("framework_architecture")
    if not architecture:
        return None
    source = Path(architecture["artifact_root"]).resolve()
    _validate_tree(source)
    workspace = Path(workspace).resolve()
    destination = (workspace / ".framework-architecture").resolve()
    if not destination.is_relative_to(workspace) or destination.exists():
        raise ArchitectureError("Framework architecture destination is invalid")
    temporary = (workspace / ".framework-architecture.installing").resolve()
    if temporary.exists():
        raise ArchitectureError("Framework architecture temporary destination already exists")
    try:
        shutil.copytree(source, temporary)
        _validate_tree(temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if sha256(destination / "artifact.json") != architecture["artifact_sha256"]:
        raise ArchitectureError("Installed framework architecture artifact changed")
    if sha256(destination / "architecture.json") != architecture["architecture_sha256"]:
        raise ArchitectureError("Installed framework architecture JSON changed")
    if sha256(destination / "architecture.md") != architecture["markdown_sha256"]:
        raise ArchitectureError("Installed framework architecture Markdown changed")
    installed = {
        **{key: value for key, value in architecture.items() if key != "artifact_root"},
        "artifact_root": ".framework-architecture",
        "path": ".framework-architecture/architecture.json",
        "markdown_path": ".framework-architecture/architecture.md",
    }
    Path(out).mkdir(parents=True, exist_ok=True)
    _write_json(Path(out) / "framework_architecture_binding.json", installed)
    return installed
