"""Refusals and the confirmation prompt. Pure except `confirm`'s I/O."""
from __future__ import annotations

import os
import re

DEFAULT_DENYLIST = frozenset({"argo-cd", "app-root", "external-secrets"})
_APP_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


class SafetyError(Exception):
    pass


def denylist() -> frozenset:
    """The default denylist plus any apps named in ARGO_HOLD_DENYLIST_EXTRA (comma or space separated)."""
    extra = os.environ.get("ARGO_HOLD_DENYLIST_EXTRA", "").replace(",", " ").split()
    return DEFAULT_DENYLIST | frozenset(extra)


def validate_app_name(app: str) -> None:
    """App names become file names; accept only DNS-style names."""
    if not isinstance(app, str) or not _APP_RE.fullmatch(app):
        raise SafetyError(f"invalid app name {app!r}: use lowercase letters, digits, '-' and '.'")


def check_app_allowed(app: str, allow_sensitive: bool) -> None:
    if app in denylist() and not allow_sensitive:
        raise SafetyError(f"refusing to hold {app!r}: it is on the sensitive-app denylist; "
                          "pass --allow-sensitive to override")


def parse_target(spec: str) -> tuple[str, str]:
    parts = spec.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SafetyError(f"invalid --target {spec!r}: expected KIND/NAME")
    return parts[0].lower(), parts[1]


def check_target_allowed(kind: str) -> None:
    if kind.lower().split(".")[0] in ("secret", "secrets"):
        raise SafetyError("refusing to target a Secret: held values are stored in the state file")


def confirm(lines, assume_yes: bool, input_fn=input, out=print) -> bool:
    for line in lines:
        out(line)
    if assume_yes:
        return True
    try:
        return input_fn("Proceed? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
