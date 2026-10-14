"""Forward ops (disable auto-sync, apply) and the revert path shared by the holder's
release and the CLI's state-file-only release. Revert never relies on Argo."""
from __future__ import annotations

from ah_kube import APP_KIND, Kube, KubectlError
from ah_paths import get_at, is_absent, plan_revert, pointer

FIELD_MANAGER = "argo-hold"
AUTOMATED_OFF = {"spec": {"syncPolicy": {"automated": None}}}
REFRESH_KEY = "argocd.argoproj.io/refresh"


def disable_auto_sync(kube: Kube, payload: dict) -> None:
    kube.patch(APP_KIND, payload["app"], payload["argocd_ns"], "merge", AUTOMATED_OFF, FIELD_MANAGER)


def apply_change(kube: Kube, payload: dict, t: dict) -> None:
    change = payload["change"]
    if change["kind"] == "patch":
        kube.patch(t["kind"], t["name"], t["namespace"], change["type"], change["doc"], FIELD_MANAGER)
    elif t["existed"]:
        kube.patch(t["kind"], t["name"], t["namespace"], "merge", t["manifest"], FIELD_MANAGER)
    else:
        kube.apply_ssa(t["manifest"], FIELD_MANAGER)


def revert_target(kube: Kube, t: dict, force: bool, log) -> list[str]:
    ref = f"{t['kind']}/{t['name']}"
    live = kube.get(t["kind"], t["name"], t["namespace"])
    if not t["existed"]:
        if live is None:
            log(f"{ref}: already gone")
            return []
        # An entry with no manager name is someone else's write: it counts against "sole manager".
        managers = {m.get("manager") for m in live["metadata"].get("managedFields", [])}
        if managers <= {FIELD_MANAGER} or force:
            kube.delete(t["kind"], t["name"], t["namespace"])
            log(f"{ref}: deleted (created by this hold)")
            return []
        others = sorted(m for m in managers - {FIELD_MANAGER} if isinstance(m, str))
        return [f"{ref}: modified by other managers {others}; "
                "not deleting (use --force)"]
    if live is None:
        return [f"{ref}: no longer exists; nothing to revert"]
    for attempt in (1, 2):
        ops, conflicts = plan_revert(t["paths"], live, force)
        if not ops:
            break
        try:
            kube.patch(t["kind"], t["name"], t["namespace"], "json", ops, FIELD_MANAGER)
            log(f"{ref}: reverted {len(ops)} op(s)")
            break
        except KubectlError:
            if attempt == 2:
                raise
            live = kube.get(t["kind"], t["name"], t["namespace"])
            if live is None:
                return [f"{ref}: disappeared during revert"]
    return [f"{ref}: {pointer(c['path'])} changed by someone else (now {c['live_shape']}); "
            "skipped (use --force)" for c in conflicts]


def restore_policy(kube: Kube, payload: dict, force: bool, log) -> list[str]:
    app, ns = payload["app"], payload["argocd_ns"]
    live_app = kube.get(APP_KIND, app, ns)
    if live_app is None:
        return [f"Application {app} not found; cannot restore syncPolicy"]
    live = get_at(live_app, ["spec", "syncPolicy", "automated"])
    orig = payload["original_automated"]
    warnings: list[str] = []
    if live != orig:
        conflict = not is_absent(live)
        if conflict and not force:
            warnings.append(f"{app}: spec.syncPolicy.automated was changed by someone else "
                            "during the hold; left as is (use --force)")
        else:
            if conflict:
                kube.patch(APP_KIND, app, ns, "merge", AUTOMATED_OFF, FIELD_MANAGER)
            if not is_absent(orig):
                kube.patch(APP_KIND, app, ns, "merge",
                           {"spec": {"syncPolicy": {"automated": orig}}}, FIELD_MANAGER)
                log(f"{app}: restored spec.syncPolicy.automated")
    if not is_absent(orig) and not warnings:
        kube.annotate(APP_KIND, app, ns, REFRESH_KEY, "hard")
        log(f"{app}: requested hard refresh")
    return warnings


def release_all(kube: Kube, payload: dict, force: bool, log) -> tuple[list[str], list[str]]:
    """Targets first, then the Application policy. A step that fails does not stop the
    next: leaving auto-sync off is worse than a half-reverted change that Argo will fix."""
    warnings: list[str] = []
    errors: list[str] = []
    for t in payload["targets"]:
        try:
            warnings += revert_target(kube, t, force, log)
        except Exception as e:      # KeyboardInterrupt/SystemExit still propagate
            errors.append(f"{t['kind']}/{t['name']}: {_describe(e)}")
    try:
        warnings += restore_policy(kube, payload, force, log)
    except Exception as e:
        errors.append(f"{payload['app']}: {_describe(e)}")
    return warnings, errors


def _describe(e: Exception) -> str:
    """Value-free error text: a KubectlError message is already value-free; anything else is
    reported by class name only (its str() could carry cluster values)."""
    return str(e) if isinstance(e, KubectlError) else f"unexpected <{type(e).__name__}>"
