"""State file (one per app), log file, and verdicts. The state file is created with
`ah_lock`'s create-exclusive publish, so it doubles as the one-hold-per-app lock."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import ah_lock

STALE_FACTOR = 2.5  # heartbeat older than STALE_FACTOR * interval is stale


class HoldExists(Exception):
    pass


def state_dir() -> Path:
    return Path(os.environ.get("ARGO_HOLD_STATE_DIR") or Path.home() / ".local/state/argo-hold")


def state_path(app: str) -> Path:
    return state_dir() / f"{app}.json"


def log_path(app: str) -> Path:
    return state_dir() / f"{app}.log"


def _private_dir() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def create(app: str, payload: dict) -> None:
    _private_dir()
    old = os.umask(0o077)
    try:
        ah_lock.write_payload_atomic(state_path(app), payload)
    except FileExistsError as e:
        raise HoldExists(app) from e
    finally:
        os.umask(old)


def load(app: str) -> dict | None:
    return ah_lock.read_payload(state_path(app))


def save(app: str, payload: dict) -> None:
    old = os.umask(0o077)
    try:
        ah_lock.replace_payload_atomic(state_path(app), payload)
    finally:
        os.umask(old)


def remove(app: str) -> None:
    state_path(app).unlink(missing_ok=True)


def list_holds() -> list[dict]:
    d = state_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        payload = ah_lock.read_payload(p)
        if payload:
            out.append(payload)
    return out


def append_log(app: str, msg: str) -> None:
    _private_dir()
    old = os.umask(0o077)
    try:
        with open(log_path(app), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    finally:
        os.umask(old)


def holder_alive(payload: dict) -> bool:
    return payload.get("hostname") == socket.gethostname() and ah_lock.pid_alive(payload["pid"])


def verdict(payload: dict, now: float | None = None) -> str:
    if ah_lock.owner_dead_pid(payload):
        return "dead-pid"
    if not ah_lock.heartbeat_fresh(payload, STALE_FACTOR * payload["interval"], now):
        return "stale"
    return "live"


def hold_kind(payload: dict) -> str:
    """'patch', 'manifest' or 'freeze'. State files from before --freeze existed all carry
    change.kind, but a missing key is read as a patch hold rather than failing."""
    return (payload.get("change") or {}).get("kind") or "patch"


def ttl_remaining(payload: dict, now: float | None = None) -> float:
    return payload["expires_at"] - (time.time() if now is None else now)
