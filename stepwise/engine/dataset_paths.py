"""按登记的目录名称和别名定位数据包，保留真实路径。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def group_names(adapter: dict) -> tuple[str, ...]:
    aliases = adapter.get("group_aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias for alias in aliases
    ):
        raise ValueError("Invalid dataset group aliases")
    return (adapter["group"], *aliases)


def matching_directories(parent: Path, names: Iterable[str]) -> list[Path]:
    if not parent.is_dir():
        return []
    accepted = {name.casefold() for name in names}
    return sorted(
        path for path in parent.iterdir()
        if path.is_dir() and path.name.casefold() in accepted
    )


def resolve_dataset_package(base: Path, adapter: dict) -> Path:
    candidates = {
        package.resolve()
        for group in matching_directories(base, group_names(adapter))
        for package in matching_directories(group, (adapter["package"],))
    }
    if not candidates:
        raise ValueError("No registered dataset package: " + adapter["group"])
    if len(candidates) > 1:
        raise ValueError("Dataset directory names are ambiguous: " + adapter["group"])
    return candidates.pop()
