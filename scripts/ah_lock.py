"""Atomic state-file publish and owner-liveness checks.

The state file doubles as the one-hold-per-app lock: `write_payload_atomic` publishes it
create-exclusive, `replace_payload_atomic` refreshes the heartbeat in place, and the
`owner_dead_pid` / `heartbeat_fresh` pair decides whether a lock is stale.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path


def read_payload(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, IsADirectoryError):
        return None


def write_payload_atomic(path: Path, payload: dict) -> None:
    """Write a temp file in the same dir, then os.link it into place. A reader never sees a
    partial file, and the link fails if the target exists (create-exclusive)."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(json.dumps(payload))
    try:
        os.link(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def replace_payload_atomic(path: Path, payload: dict) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.hb.{os.getpid()}.{time.time_ns()}")
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True
    return True


def heartbeat_fresh(payload: dict, ttl: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (now - float(payload.get("heartbeat_at", 0))) <= ttl


def owner_dead_pid(payload: dict) -> bool:
    if payload.get("hostname") != socket.gethostname():
        return False  # another host: cannot verify, so not provably dead
    pid = payload.get("pid")
    if pid is None:
        return False
    return not pid_alive(int(pid))
