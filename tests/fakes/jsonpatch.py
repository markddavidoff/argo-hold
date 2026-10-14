"""JSON patch helpers for the fake kubectl and tests.

merge_patch is RFC 7386, apply_json_patch is the RFC 6902 subset argo-hold emits
(add/replace/remove/test). strategic_patch APPROXIMATES kubectl's strategic merge:
dict merge, lists of dicts keyed by `name` merge by name, other lists replace.
Real strategic-merge behavior is checked against k3d (test_k3d.py), not here.
"""
import copy


class PatchError(Exception):
    pass


_MISSING = object()


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def strategic_patch(target, patch):
    if isinstance(patch, dict) and isinstance(target, dict):
        out = copy.deepcopy(target)
        for k, v in patch.items():
            if v is None:
                out.pop(k, None)
            elif k in out:
                out[k] = strategic_patch(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out
    if (
        isinstance(patch, list)
        and isinstance(target, list)
        and patch
        and all(isinstance(x, dict) and "name" in x for x in patch + target)
    ):
        out = copy.deepcopy(target)
        idx = {x["name"]: i for i, x in enumerate(out)}
        for item in patch:
            if item["name"] in idx:
                out[idx[item["name"]]] = strategic_patch(out[idx[item["name"]]], item)
            else:
                out.append(copy.deepcopy(item))
        return out
    return copy.deepcopy(patch)


def _tokens(path):
    return [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]


def _walk(doc, toks):
    cur = doc
    try:
        for t in toks:
            cur = cur[int(t)] if isinstance(cur, list) else cur[t]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise PatchError(f"path not found: /{'/'.join(toks)}") from e
    return cur


def apply_json_patch(doc, ops):
    doc = copy.deepcopy(doc)
    for op in ops:
        toks = _tokens(op["path"])
        parent, last = _walk(doc, toks[:-1]), toks[-1]
        kind = op["op"]
        if kind == "test":
            if isinstance(parent, list):
                cur = parent[int(last)]
            else:
                cur = parent.get(last, _MISSING)
            if cur is _MISSING or cur != op["value"]:
                raise PatchError(f"test failed at {op['path']}")
        elif kind == "remove":
            if isinstance(parent, list):
                parent.pop(int(last))
            elif last in parent:
                del parent[last]
            else:
                raise PatchError(f"remove: missing {op['path']}")
        elif kind in ("add", "replace"):
            value = copy.deepcopy(op["value"])
            if isinstance(parent, list):
                if kind == "add":
                    parent.insert(len(parent) if last == "-" else int(last), value)
                else:
                    parent[int(last)] = value
            else:
                if kind == "replace" and last not in parent:
                    raise PatchError(f"replace: missing {op['path']}")
                parent[last] = value
        else:
            raise PatchError(f"unsupported op {kind}")
    return doc
