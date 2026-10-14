"""Read-only planning: snapshot, server dry-run, touched paths. Nothing is written."""
from __future__ import annotations

import os
import socket

from ah_kube import APP_KIND, Kube
from ah_ops import FIELD_MANAGER
from ah_paths import ABSENT, diff_paths, get_at, pointer


class HoldError(Exception):
    pass


def make_plan(kube: Kube, app: str, argocd_ns: str, change: dict, specs: list[dict]) -> dict:
    live_app = kube.get(APP_KIND, app, argocd_ns)
    if live_app is None:
        raise HoldError(f"Application {app} not found in namespace {argocd_ns}")
    original = get_at(live_app, ["spec", "syncPolicy", "automated"])
    targets = []
    for s in specs:
        ref = f"{s['kind']}/{s['name']}"
        live = kube.get(s["kind"], s["name"], s["namespace"])
        if change["kind"] == "patch":
            if live is None:
                raise HoldError(f"{ref} not found in namespace {s['namespace']}")
            after = kube.patch(s["kind"], s["name"], s["namespace"], change["type"],
                               change["doc"], FIELD_MANAGER, dry_run=True)
            paths = diff_paths(live, after)
            existed = True
        else:
            existed = live is not None
            if existed:   # never server-side apply onto an Argo-managed object (findings §4)
                after = kube.patch(s["kind"], s["name"], s["namespace"], "merge", s["manifest"],
                                   FIELD_MANAGER, dry_run=True)
                paths = diff_paths(live, after)
            else:
                kube.apply_ssa(s["manifest"], FIELD_MANAGER, dry_run=True)   # validates the create
                paths = []
            if existed and not paths:
                raise HoldError(f"the manifest changes nothing on {ref}")
        if existed and not paths:
            raise HoldError(f"the patch changes nothing on {ref}")
        targets.append({"kind": s["kind"], "name": s["name"], "namespace": s["namespace"],
                        "existed": existed, "paths": paths, "manifest": s.get("manifest")})
    return {"automated_was_on": original != ABSENT, "original_automated": original,
            "targets": targets}


def plan_signature(plan: dict) -> list[str]:
    sig = [f"automated:{plan['automated_was_on']}"]
    for t in plan["targets"]:
        sig.append(f"{t['kind']}/{t['name']}:{t['existed']}")
        sig += sorted(f"{t['kind']}/{t['name']}{pointer(r['path'])}" for r in t["paths"])
    return sig


def release_notice(payload: dict) -> str | None:
    """What releasing a freeze will do, in plain words; None for a hold that is not a freeze.
    Restoring `automated` is what syncs the pending changes; the hard refresh only makes it prompt."""
    if (payload.get("change") or {}).get("kind") != "freeze":
        return None
    app = payload["app"]
    if payload["original_automated"] == ABSENT:
        return (f"NOTICE: auto-sync on {app} was already off before the freeze; "
                "release leaves it off and syncs nothing")
    return (f"NOTICE: releasing the freeze on {app} turns auto-sync back on, and a hard refresh is "
            "requested: every change pending in git syncs at once")


def new_payload(app, context, argocd_ns, change, plan, ttl, interval, now) -> dict:
    return {
        "app": app, "context": context, "argocd_ns": argocd_ns,
        "pid": os.getpid(), "hostname": socket.gethostname(),
        "started_at": now, "heartbeat_at": now, "expires_at": now + ttl,
        "ttl": ttl, "interval": interval,
        "automated_was_on": plan["automated_was_on"],
        "original_automated": plan["original_automated"],
        "change": change, "targets": plan["targets"],
        "phase": "starting", "last_tick": None, "reapply_count": 0, "reenable_count": 0,
    }
