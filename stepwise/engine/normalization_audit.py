"""Record deterministic field changes without changing the historical protocol."""
from __future__ import annotations


def changes(before, after, path: str = "$") -> list[dict]:
    result = []
    if type(before) is not type(after):
        return [{"path": path, "before": before, "after": after}]
    if isinstance(before, dict):
        for key in sorted(before.keys() | after.keys()):
            child = path + "." + key
            if key not in before:
                result.append({"path": child, "operation": "added", "after": after[key]})
            elif key not in after:
                result.append({"path": child, "operation": "removed", "before": before[key]})
            else:
                result.extend(changes(before[key], after[key], child))
    elif isinstance(before, list):
        if len(before) != len(after):
            result.append({"path": path, "before": before, "after": after})
        else:
            for index, (left, right) in enumerate(zip(before, after)):
                result.extend(changes(left, right, f"{path}[{index}]"))
    elif before != after:
        result.append({"path": path, "before": before, "after": after})
    return result
