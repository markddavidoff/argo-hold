"""Start sequence and the event-driven holder.

Watchers (one `kubectl get -w` per watched object) feed one reactor thread; a timer
thread only writes the heartbeat, enforces the TTL, and enqueues a safety resync. The
reactor is the only thread that writes to the cluster, so writes are serial.
"""
from __future__ import annotations

import collections
import os
import queue
import socket
import threading
import time

import ah_ops
import ah_state
from ah_kube import APP_KIND, Kube, KubectlError, iter_json_docs
from ah_ops import AUTOMATED_OFF, FIELD_MANAGER
from ah_paths import ABSENT, get_at, is_absent
from ah_plan import HoldError, make_plan, new_payload, plan_signature

OP_POLL_SECONDS = 5
RATE_MAX, RATE_WINDOW = 5, 60.0
BACKOFF = (1, 2, 5, 10, 30)
KNOWN_EVENT_TYPES = ("ADDED", "MODIFIED", "DELETED", "BOOKMARK", "RESTART", "ERROR")


def wait_for_no_operation(kube: Kube, app: str, ns: str, limit: float,
                          sleep=time.sleep, now=time.time) -> None:
    """Wait for any Running sync operation to finish; never terminate one."""
    deadline = now() + limit
    while True:
        live = kube.get(APP_KIND, app, ns)
        if live is None:
            raise HoldError(f"Application {app} disappeared")
        if get_at(live, ["status", "operationState", "phase"]) != "Running":
            return
        if now() >= deadline:
            raise HoldError(f"a sync operation is still Running on {app} after {int(limit)}s; "
                            "not terminating it")
        sleep(OP_POLL_SECONDS)


def begin(kube: Kube, *, app: str, argocd_ns: str, context: str, change: dict, specs: list[dict],
          confirmed_plan: dict, ttl: int, interval: int, wait_op: int,
          sleep=time.sleep, now=time.time) -> dict:
    """Take the lock first, then re-plan, turn auto-sync off, wait out a running sync, apply.
    Re-planning after the lock catches a cluster that changed while the user sat at the prompt."""
    t0 = now()
    payload = {"app": app, "context": context, "argocd_ns": argocd_ns, "pid": os.getpid(),
               "hostname": socket.gethostname(), "started_at": t0, "heartbeat_at": t0,
               "expires_at": t0 + ttl, "ttl": ttl, "interval": interval,
               "automated_was_on": False, "original_automated": ABSENT, "change": change,
               "targets": [], "phase": "planning", "last_tick": None,
               "reapply_count": 0, "reenable_count": 0}
    ah_state.create(app, payload)          # the one-hold-per-app lock; HoldExists propagates untouched
    try:
        plan = make_plan(kube, app, argocd_ns, change, specs)
        if plan_signature(plan) != plan_signature(confirmed_plan):
            raise HoldError("the cluster changed since the plan was shown; re-run start")
        payload.update(new_payload(app, context, argocd_ns, change, plan, ttl, interval, t0))
        ah_state.save(app, payload)
        if payload["automated_was_on"]:
            ah_ops.disable_auto_sync(kube, payload)
        wait_for_no_operation(kube, app, argocd_ns, wait_op, sleep, now)
        for t in payload["targets"]:
            ah_ops.apply_change(kube, payload, t)
        payload["phase"] = "holding"
        ah_state.save(app, payload)
    except BaseException:
        if payload["phase"] == "planning":      # nothing was touched yet
            ah_state.remove(app)
        else:
            warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
            if not warnings and not errors:
                ah_state.remove(app)
            else:                               # keep the state so `release` can finish the job
                payload["phase"] = "needs-attention"
                ah_state.save(app, payload)
        raise
    return payload


class Watcher(threading.Thread):
    """Runs `kubectl get -w` for one object, restarting with backoff. Pushes
    (tag, event) onto the queue; a RESTART event asks the reactor to re-read the object."""

    def __init__(self, kube: Kube, kind, name, ns, tag, q, stop_event):
        super().__init__(daemon=True)
        self.kube, self.kind, self.name, self.ns = kube, kind, name, ns
        self.tag, self.q, self.stop_event = tag, q, stop_event
        self.proc = None

    def run(self):
        attempt = 0
        while not self.stop_event.is_set():
            self.proc = self.kube.watch(self.kind, self.name, self.ns)
            try:
                for ev in iter_json_docs(self.proc.stdout):
                    attempt = 0
                    self.q.put((self.tag, ev))
            except (ValueError, OSError):
                pass
            finally:
                self.terminate()
            if self.stop_event.is_set():
                break
            self.q.put((self.tag, {"type": "RESTART"}))
            self.stop_event.wait(BACKOFF[min(attempt, len(BACKOFF) - 1)])
            attempt += 1

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


class Holder:
    def __init__(self, kube: Kube, payload: dict, *, now=time.time, resolution: float = 1.0, echo=None):
        self.kube, self.payload, self.now = kube, payload, now
        self.resolution, self.echo = resolution, echo
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.stop_reason: str | None = None
        self._reapplies: collections.deque[float] = collections.deque()
        interval = payload["interval"]
        self._next_heartbeat = now() + interval
        self._next_resync = now() + interval

    # -- logging --------------------------------------------------------------
    def log(self, msg: str) -> None:
        ah_state.append_log(self.payload["app"], msg)
        if self.echo:
            self.echo(msg)

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
        self.stop_event.set()

    # -- reactor handlers (called from one thread) -------------------------------
    def on_app(self, obj: dict) -> bool:
        if is_absent(get_at(obj, ["spec", "syncPolicy", "automated"])):
            return False
        p = self.payload
        self.log(f"WARNING: spec.syncPolicy.automated reappeared on {p['app']}; removing it again")
        self.kube.patch(APP_KIND, p["app"], p["argocd_ns"], "merge", AUTOMATED_OFF, FIELD_MANAGER)
        with self.lock:
            p["reenable_count"] += 1
        return True

    def _drifted(self, t: dict, live: dict | None) -> bool:
        if live is None:
            return True
        if not t["existed"]:
            return False
        return any(get_at(live, r["path"]) != r["held"] for r in t["paths"])

    def _rate_ok(self) -> bool:
        now = self.now()
        while self._reapplies and now - self._reapplies[0] > RATE_WINDOW:
            self._reapplies.popleft()
        if len(self._reapplies) >= RATE_MAX:
            return False
        self._reapplies.append(now)
        return True

    def on_target(self, t: dict, live: dict | None) -> bool:
        ref = f"{t['kind']}/{t['name']}"
        if not self._drifted(t, live):
            return False
        if live is None and self.payload["change"]["kind"] == "patch":
            self.log(f"WARNING: {ref} was deleted; cannot re-apply a patch to a missing object")
            return False
        if not self._rate_ok():
            self.log(f"WARNING: re-apply rate limit hit for {ref} (something keeps reverting it); "
                     "deferring to the next resync")
            return False
        self.log(f"drift on {ref}; re-applying")
        ah_ops.apply_change(self.kube, self.payload, t)
        with self.lock:
            self.payload["reapply_count"] += 1
        return True

    def resync(self) -> None:
        p = self.payload
        live_app = self.kube.get(APP_KIND, p["app"], p["argocd_ns"])
        if live_app is None:
            self.log("ERROR: Application is gone; ending the hold")
            self.stop("application deleted")
            return
        self.on_app(live_app)
        for t in p["targets"]:
            self.on_target(t, self.kube.get(t["kind"], t["name"], t["namespace"]))

    def dispatch(self, tag, ev) -> None:
        kind, ref = tag
        etype = None
        try:
            etype = ev.get("type")      # inside the try: a non-dict watch document must not end the hold
            if kind == "timer" or etype in ("RESTART", "ERROR"):
                self.resync()
            elif kind == "app":
                if etype == "DELETED":
                    self.log("ERROR: Application was deleted; ending the hold")
                    self.stop("application deleted")
                else:
                    self.on_app(ev["object"])
            else:
                t = self.payload["targets"][ref]
                self.on_target(t, None if etype == "DELETED" else ev["object"])
        except KubectlError as e:
            self.log(f"ERROR: {e}")
        except Exception as e:      # one odd event must not end a healthy hold
            # Class name and source only: never str(e) or any part of the event (values stay out of logs).
            shown = etype if etype in KNOWN_EVENT_TYPES else "other"
            self.log(f"ERROR: unexpected <{type(e).__name__}> handling {kind} event "
                     f"type={shown}; skipped")

    # -- timer ------------------------------------------------------------------
    def write_heartbeat(self) -> None:
        with self.lock:
            now = self.now()
            self.payload["heartbeat_at"] = now
            self.payload["last_tick"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
            ah_state.save(self.payload["app"], self.payload)

    def housekeeping(self, q: queue.Queue | None = None) -> None:
        now = self.now()
        if now >= self.payload["expires_at"]:
            self.log("TTL expired; releasing")
            self.stop("ttl expired")
            return
        interval = self.payload["interval"]
        if now >= self._next_heartbeat:
            self.write_heartbeat()
            self._next_heartbeat = now + interval
        if now >= self._next_resync:
            self._next_resync = now + interval
            if q is not None:
                q.put((("timer", None), {}))

    def _timer(self, q: queue.Queue) -> None:
        while not self.stop_event.wait(self.resolution):
            self.housekeeping(q)

    # -- main loop ----------------------------------------------------------------
    def run(self) -> str:
        p = self.payload
        q: queue.Queue = queue.Queue()
        watchers = [Watcher(self.kube, APP_KIND, p["app"], p["argocd_ns"], ("app", None), q, self.stop_event)]
        for i, t in enumerate(p["targets"]):
            watchers.append(Watcher(self.kube, t["kind"], t["name"], t["namespace"], ("target", i), q,
                                    self.stop_event))
        timer = threading.Thread(target=self._timer, args=(q,), daemon=True)
        for w in watchers:
            w.start()
        timer.start()
        try:
            while not self.stop_event.is_set():
                try:
                    tag, ev = q.get(timeout=self.resolution)
                except queue.Empty:
                    continue
                self.dispatch(tag, ev)
        finally:
            self.stop("stopped")
            for w in watchers:
                w.terminate()
            for w in watchers:
                w.join(timeout=5)
            timer.join(timeout=5)
        return self.stop_reason or "stopped"
