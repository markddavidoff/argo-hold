#!/usr/bin/env python3
"""argo-hold — hold an ArgoCD app's auto-sync off while a live change stays applied.

  argo-hold start --app A --context C (--patch J | --patch-file F) --type T --target KIND/NAME -n NS
  argo-hold start --app A --context C -f MANIFEST
  argo-hold start --app A --context C --freeze        # hold auto-sync off; apply no change
  argo-hold status --app A | release --app A [--force] | list

Exit codes: 0 ok, 1 error or refusal, 2 usage, 3 finished with warnings (state kept).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ah_hold  # noqa: E402
import ah_kube  # noqa: E402
import ah_ops  # noqa: E402
import ah_paths  # noqa: E402
import ah_plan  # noqa: E402
import ah_safety  # noqa: E402
import ah_state  # noqa: E402

ARGOCD_NS = "argo-cd"
RELEASE_WAIT = 120
DEFAULT_ONCALL_CHANNEL = "#on-call"


def oncall_channel() -> str:
    """The channel to announce a hold in. The tool only names it; the person or agent posts."""
    return os.environ.get("ARGO_HOLD_ONCALL_CHANNEL", "").strip() or DEFAULT_ONCALL_CHANNEL


def parse_duration(s) -> int:
    s = str(s).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def humanize(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {sec}s" if m else f"{sec}s")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argo-hold", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="hold auto-sync off, and apply a live change unless --freeze")
    s.add_argument("--app", required=True)
    s.add_argument("--context", required=True, help="kube context (required; echoed before acting)")
    s.add_argument("-n", "--namespace", help="namespace of the target objects")
    s.add_argument("--target", action="append", default=[], metavar="KIND/NAME")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--patch", help="patch body (JSON)")
    g.add_argument("--patch-file", help="file containing the patch body (JSON)")
    g.add_argument("-f", "--filename", help="manifest: a missing object is created with server-side apply; "
                   "an existing object is merge-patched")
    g.add_argument("--freeze", action="store_true",
                   help="apply no change: only turn auto-sync off and keep it off. Takes no --target, -n "
                   "or --type. Release turns auto-sync back on, and every change pending in git syncs at once")
    s.add_argument("--type", choices=["merge", "json", "strategic"])
    s.add_argument("--ttl", default="2h")
    s.add_argument("--interval", type=int, default=60, help="heartbeat and safety-resync period (s)")
    s.add_argument("--wait-op", default="5m", help="how long to wait for a Running sync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--detach", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--allow-sensitive", action="store_true")
    s.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    st = sub.add_parser("status", help="show one hold")
    st.add_argument("--app", required=True)
    r = sub.add_parser("release", help="revert the change and restore the app")
    r.add_argument("--app", required=True)
    r.add_argument("--force", action="store_true", help="overwrite fields changed by someone else")
    sub.add_parser("list", help="show all active holds")
    return p


def make_log(app: str, echo: bool = True):
    def log(msg: str) -> None:
        ah_state.append_log(app, msg)
        if echo:
            print(f"argo-hold: {msg}", flush=True)
    return log


# -- start ---------------------------------------------------------------------
def load_change(args, kube):
    if args.freeze:
        if args.type or args.target or args.namespace:
            raise ah_safety.SafetyError("--freeze changes no objects; drop --target, -n and --type")
        return {"kind": "freeze"}, []
    if args.filename:
        if args.type or args.target:
            raise ah_safety.SafetyError("-f derives its targets from the manifest; drop --target/--type")
        specs = []
        for o in kube.convert_manifest(args.filename):
            ns = o["metadata"].get("namespace") or args.namespace
            if not ns:
                raise ah_safety.SafetyError(f"{o['kind']}/{o['metadata']['name']} has no namespace; pass -n")
            specs.append({"kind": o["kind"].lower(), "name": o["metadata"]["name"],
                          "namespace": ns, "manifest": o})
        return {"kind": "manifest"}, specs
    if not args.type:
        raise ah_safety.SafetyError("--patch/--patch-file need --type merge|json|strategic")
    if not args.target or not args.namespace:
        raise ah_safety.SafetyError("--target KIND/NAME and -n NAMESPACE are required with --patch/--patch-file")
    raw = args.patch if args.patch is not None else Path(args.patch_file).read_text()
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise ah_safety.SafetyError(f"the patch is not valid JSON: {e}") from None
    specs = []
    for t in args.target:
        kind, name = ah_safety.parse_target(t)
        specs.append({"kind": kind, "name": name, "namespace": args.namespace, "manifest": None})
    return {"kind": "patch", "type": args.type, "doc": doc}, specs


def plan_lines(args, plan) -> list[str]:
    lines = [f"context:   {args.context}",
             f"app:       {args.app} (namespace {ARGOCD_NS})",
             f"ttl:       {args.ttl}    interval: {args.interval}s",
             f"on-call:   {oncall_channel()} (post the on-call notice here before a live start)"]
    if args.freeze:
        lines.append("mode:      freeze (no change is applied; only auto-sync is turned off)")
    lines.append("auto-sync: will be turned OFF" if plan["automated_was_on"] else "auto-sync: already off")
    if args.freeze:
        lines.append(f"release:   turns auto-sync back on and requests a hard refresh; EVERY change pending "
                     f"in git for {args.app} syncs at once" if plan["automated_was_on"] else
                     "release:   leaves auto-sync off (it was already off); nothing is restored")
    for t in plan["targets"]:
        suffix = "" if t["existed"] else "   (will be CREATED)"
        lines.append(f"target:    {t['kind']}/{t['name']} -n {t['namespace']}{suffix}")
        for r in t["paths"]:
            lines.append(f"             {ah_paths.describe_op(r):8} {ah_paths.pointer(r['path'])}"
                         f"  ({ah_paths.shape(r['held'])})")
    return lines


def spawn_detached(args) -> int:
    argv = [a for a in sys.argv[1:] if a != "--detach"] + ["--yes", "--_child"]
    ah_state.state_dir().mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    logf = open(ah_state.log_path(args.app), "ab")
    os.umask(old)
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *argv],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf)
    deadline = time.time() + parse_duration(args.wait_op) + 60
    while time.time() < deadline:
        p = ah_state.load(args.app)
        if p and p.get("pid") == child.pid and p.get("phase") == "holding":
            print(f"argo-hold: holding {args.app} (pid {child.pid}); log: {ah_state.log_path(args.app)}")
            return 0
        if child.poll() is not None:
            print(f"argo-hold: the detached holder exited early (code {child.returncode}); "
                  f"see {ah_state.log_path(args.app)}", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print("argo-hold: timed out waiting for the detached holder to start", file=sys.stderr)
    return 1


def cmd_start(args) -> int:
    ah_safety.validate_app_name(args.app)
    ah_safety.check_app_allowed(args.app, args.allow_sensitive)
    if ah_state.load(args.app) is not None:
        raise ah_state.HoldExists(args.app)
    kube = ah_kube.Kube(args.context)
    change, specs = load_change(args, kube)
    for s in specs:
        ah_safety.check_target_allowed(s["kind"])
    ttl, wait_op = parse_duration(args.ttl), parse_duration(args.wait_op)
    plan = ah_plan.make_plan(kube, args.app, ARGOCD_NS, change, specs)
    lines = plan_lines(args, plan)
    if args.dry_run:
        print("\n".join(lines))
        print("dry-run: nothing was changed")
        return 0
    if not ah_safety.confirm(lines, args.yes):
        print("aborted")
        return 1
    if args.detach and not args._child:
        return spawn_detached(args)

    log = make_log(args.app, echo=not args._child)
    payload = ah_hold.begin(kube, app=args.app, argocd_ns=ARGOCD_NS, context=args.context, change=change,
                            specs=specs, confirmed_plan=plan, ttl=ttl, interval=args.interval,
                            wait_op=wait_op)
    holder = ah_hold.Holder(kube, payload, resolution=float(os.environ.get("ARGO_HOLD_TICK", "1.0")),
                            echo=(lambda m: print(f"argo-hold: {m}", flush=True)) if not args._child else None)

    def on_signal(signum, _frame):
        holder.stop(f"signal {signal.Signals(signum).name}")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    log(f"holding {args.app} on {args.context}; ttl {args.ttl}; release with: argo-hold release --app {args.app}")
    try:
        reason = holder.run()
    except BaseException as e:  # never leave auto-sync off because the holder crashed
        reason = f"internal error: {type(e).__name__}"
    log(f"releasing ({reason})")
    payload["phase"] = "releasing"
    ah_state.save(args.app, payload)
    return finish_release(kube, payload, False, log)


def finish_release(kube, payload, force, log) -> int:
    app = payload["app"]
    notice = ah_plan.release_notice(payload)
    if notice:
        log(notice)
    warnings, errors = ah_ops.release_all(kube, payload, force, log)
    for w in warnings:
        log(f"WARNING: {w}")
    for e in errors:
        log(f"ERROR: {e}")
    if errors:
        payload["phase"] = "needs-attention"
        ah_state.save(app, payload)
        log(f"release incomplete; state kept. Retry: argo-hold release --app {app}")
        return 1
    if warnings:
        payload["phase"] = "needs-attention"
        ah_state.save(app, payload)
        log(f"release skipped some fields; state kept. Overwrite with: argo-hold release --app {app} --force")
        return 3
    ah_state.remove(app)
    log("released")
    return 0


# -- release / status / list ------------------------------------------------------
def cmd_release(args) -> int:
    ah_safety.validate_app_name(args.app)
    p = ah_state.load(args.app)
    if p is None:
        print(f"no hold recorded for {args.app}; nothing to release")
        return 0
    print(f"releasing {args.app} on context {p['context']}")
    if ah_state.holder_alive(p) and p["pid"] != os.getpid():
        notice = ah_plan.release_notice(p)      # the holder logs it too, but its output is not ours
        if notice:
            print(notice)
        os.kill(p["pid"], signal.SIGTERM)
        deadline = time.time() + RELEASE_WAIT
        while time.time() < deadline and ah_state.holder_alive(p):
            time.sleep(0.2)
        if ah_state.holder_alive(p):
            print(f"the holder (pid {p['pid']}) did not exit within {RELEASE_WAIT}s", file=sys.stderr)
            return 1
        p2 = ah_state.load(args.app)
        if p2 is None:
            print("released")
            return 0
        if not args.force:
            print(f"the holder released with warnings; see {ah_state.log_path(args.app)}. "
                  f"Overwrite with: argo-hold release --app {args.app} --force")
            return 3
        p = p2
    return finish_release(ah_kube.Kube(p["context"]), p, args.force, make_log(args.app))


def cmd_status(args) -> int:
    ah_safety.validate_app_name(args.app)
    p = ah_state.load(args.app)
    if p is None:
        print(f"no hold recorded for {args.app}")
        return 1
    now = time.time()
    v = ah_state.verdict(p, now)
    left = ah_state.ttl_remaining(p, now)
    freeze = ah_state.hold_kind(p) == "freeze"
    print(f"app:         {p['app']}   context: {p['context']}")
    if freeze:
        print("mode:        freeze (no change)")
    print(f"holder:      pid {p['pid']} ({v}), phase {p['phase']}")
    print(f"heartbeat:   {int(now - p['heartbeat_at'])}s ago (interval {p['interval']}s)")
    print(f"ttl left:    {humanize(left) if left > 0 else 'EXPIRED'}")
    print(f"activity:    {p['reenable_count']} auto-sync re-disable(s)"
          + ("" if freeze else f", {p['reapply_count']} re-apply(s)"))
    try:
        live = ah_kube.Kube(p["context"]).get(ah_kube.APP_KIND, p["app"], p["argocd_ns"])
        if live is None:
            print("live:        Application not found")
        else:
            pol = ah_paths.get_at(live, ["spec", "syncPolicy", "automated"])
            print(f"live:        auto-sync {'ON' if not ah_paths.is_absent(pol) else 'off'}, "
                  f"sync status {ah_paths.get_at(live, ['status', 'sync', 'status'])}")
    except ah_kube.KubectlError as e:
        print(f"live:        unavailable ({e.first_line})")
    if v != "live":
        print(f"WARNING: the holder is {v}. Auto-sync may still be off and the change still applied. "
              f"Run: argo-hold release --app {p['app']}")
        return 3
    return 0


def cmd_list(args) -> int:
    holds = ah_state.list_holds()
    if not holds:
        print("no active holds")
        return 0
    now = time.time()
    for p in holds:
        left = ah_state.ttl_remaining(p, now)
        print(f"{p['app']:30} {p['context']:40} {ah_state.verdict(p, now):9} "
              f"ttl {humanize(left) if left > 0 else 'EXPIRED'}"
              + ("  freeze (no change)" if ah_state.hold_kind(p) == "freeze" else ""))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"start": cmd_start, "status": cmd_status, "release": cmd_release, "list": cmd_list}[args.cmd](args)
    except ah_safety.SafetyError as e:
        print(f"argo-hold: {e}", file=sys.stderr)
        return 1
    except ah_state.HoldExists:
        print(f"argo-hold: a hold for {args.app} already exists; use `argo-hold status --app {args.app}` "
              f"or `argo-hold release --app {args.app}`", file=sys.stderr)
        return 1
    except ah_plan.HoldError as e:
        print(f"argo-hold: {e}", file=sys.stderr)
        return 1
    except ah_kube.KubectlError as e:
        print(f"argo-hold: {e}", file=sys.stderr)
        return 1
    except OSError as e:        # missing --patch-file or kubectl binary; a filename is not a secret
        detail = f": {e.filename}" if e.filename else ""
        print(f"argo-hold: {e.strerror or type(e).__name__}{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
