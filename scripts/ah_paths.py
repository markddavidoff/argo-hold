"""Pure helpers: touched-path diff and revert-patch planning. No I/O.

A "touched path" record is {"path": [keys...], "before": value|ABSENT, "held": value|ABSENT}.
Arrays are always one whole path; a key that was absent is recorded at its topmost
absent key, so an added block is one path and its inverse removes the whole block.
"""
from __future__ import annotations

import copy
from typing import Any

ABSENT: dict = {"__argo_hold__": "ABSENT"}  # JSON-serialisable "no such key" marker
_VOLATILE_METADATA = ("managedFields", "resourceVersion", "generation", "uid",
                      "creationTimestamp", "selfLink")


def is_absent(v: Any) -> bool:
    return isinstance(v, dict) and v == ABSENT


def normalize(obj: dict) -> dict:
    out = copy.deepcopy(obj)
    out.pop("status", None)
    md = out.get("metadata")
    if isinstance(md, dict):
        for k in _VOLATILE_METADATA:
            md.pop(k, None)
    return out


def get_at(obj: Any, path: list[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return ABSENT
        cur = cur[key]
    return cur


def diff_paths(before: dict, after: dict) -> list[dict]:
    out: list[dict] = []

    def walk(b: Any, a: Any, path: list[str]) -> None:
        if isinstance(b, dict) and isinstance(a, dict):
            for k in sorted(set(b) | set(a)):
                if k not in b:
                    out.append({"path": path + [k], "before": ABSENT, "held": a[k]})
                elif k not in a:
                    out.append({"path": path + [k], "before": b[k], "held": ABSENT})
                else:
                    walk(b[k], a[k], path + [k])
        elif b != a:
            out.append({"path": path, "before": b, "held": a})

    walk(normalize(before), normalize(after), [])
    return out


def pointer(path: list[str]) -> str:
    return "".join("/" + str(k).replace("~", "~0").replace("/", "~1") for k in path)


def shape(v: Any) -> str:
    """A value's type and size, never the value."""
    if is_absent(v):
        return "absent"
    if isinstance(v, dict):
        return f"object({len(v)} keys)"
    if isinstance(v, list):
        return f"array({len(v)})"
    return type(v).__name__


def describe_op(rec: dict) -> str:
    if is_absent(rec["before"]):
        return "add"
    if is_absent(rec["held"]):
        return "remove"
    return "replace"


def plan_revert(paths: list[dict], live: dict, force: bool = False) -> tuple[list[dict], list[dict]]:
    """Return (RFC 6902 ops, conflicts) that restore each path from `held` to `before`.

    A path already at `before` is skipped. A path at neither `held` nor `before` is a
    conflict (someone else changed it) and is skipped unless `force`. Ops guard on the
    held value with a `test` op, so a concurrent writer makes the patch fail instead of
    being overwritten. Absence cannot be `test`ed, so no guard is emitted for it.
    """
    ops: list[dict] = []
    conflicts: list[dict] = []
    for rec in paths:
        cur = get_at(live, rec["path"])
        before, held = rec["before"], rec["held"]
        if cur == before:
            continue
        if cur != held and not force:
            conflicts.append({"path": rec["path"], "live_shape": shape(cur)})
            continue
        ptr = pointer(rec["path"])
        guard = [] if (cur != held or is_absent(held)) else [{"op": "test", "path": ptr, "value": held}]
        if is_absent(before):
            ops += guard + [{"op": "remove", "path": ptr}]
        else:
            ops += guard + [{"op": "add" if is_absent(cur) else "replace", "path": ptr, "value": before}]
    return ops, conflicts
