"""Real kubectl + real Argo CD. Opt-in: `make test-k3d`."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.k3d

CTX = "k3d-argo-hold-test"
LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "argo-hold"
STRATEGIC = json.dumps({"spec": {"template": {"spec": {"tolerations": [{"key": "argo-hold", "operator": "Exists"}]}}}})


def kubectl(*args):
    out = subprocess.run(["kubectl", "--context", CTX, *args], capture_output=True, text=True, check=True)
    return out.stdout


def dep():
    return json.loads(kubectl("-n", "toy", "get", "deployment", "guestbook-ui", "-o", "json"))


def toy():
    return json.loads(kubectl("-n", "argo-cd", "get", "applications.argoproj.io", "toy", "-o", "json"))


def argo_hold(*args, **kw):
    env = dict(os.environ, ARGO_HOLD_STATE_DIR=str(kw.pop("state_dir")))
    env.pop("ARGO_HOLD_KUBECTL", None)
    return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, env=env, **kw)


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


def start_args(*extra):
    return ["start", "--app", "toy", "--context", CTX, "--target", "deployment/guestbook-ui", "-n", "toy",
            "--patch", STRATEGIC, "--type", "strategic", "--yes", "--interval", "5", "--detach", *extra]


def test_real_dry_run_computes_paths_and_changes_nothing(state_dir):
    before = dep()
    r = argo_hold(*[a for a in start_args() if a != "--detach"], "--dry-run", state_dir=state_dir)
    assert r.returncode == 0, r.stderr
    assert "/spec/template/spec/tolerations" in r.stdout
    assert dep()["metadata"]["resourceVersion"] == before["metadata"]["resourceVersion"]


def wait_for(predicate, deadline=15.0, step=0.5):
    """Poll until predicate() is truthy; return elapsed seconds, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if predicate():
            return time.monotonic() - start
        time.sleep(step)
    return None


def has_toleration():
    return any(t.get("key") == "argo-hold" for t in dep()["spec"]["template"]["spec"].get("tolerations") or [])


def auto_sync_absent():
    return "automated" not in toy()["spec"]["syncPolicy"]


def test_real_hold_redisables_auto_sync_and_release_restores_hand_added_field(state_dir):
    """Proves: after a manual re-enable of auto-sync the holder turns it off again, and release restores
    the original toleration list and sync policy. NOT a drift test: the toleration is a hand-added field
    Argo's selfHeal never reverts (see k3d-findings.md), so its presence says nothing about the holder."""
    original = dep()["spec"]["template"]["spec"].get("tolerations")
    original_policy = toy()["spec"]["syncPolicy"]["automated"]
    try:
        r = argo_hold(*start_args(), state_dir=state_dir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert auto_sync_absent()
        assert has_toleration()
        kubectl("-n", "argo-cd", "patch", "applications.argoproj.io", "toy", "--type", "merge",
                "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}')
        assert wait_for(auto_sync_absent, deadline=15) is not None, "holder did not re-disable auto-sync"
        assert has_toleration()
    finally:
        rel = argo_hold("release", "--app", "toy", state_dir=state_dir)
    assert rel.returncode == 0, rel.stdout + rel.stderr
    assert dep()["spec"]["template"]["spec"].get("tolerations") == original
    assert toy()["spec"]["syncPolicy"]["automated"] == original_policy


def test_real_hold_reapplies_a_git_managed_field_that_argo_reverts(state_dir):
    """The headline behavior. replicas is git-managed (git says 1), so once auto-sync is re-enabled real Argo
    reverts it within ~1 s. --interval 60 keeps the 60 s safety resync out of the test window: only the watch
    path can re-disable auto-sync and re-apply replicas here."""
    original_replicas = dep()["spec"]["replicas"]
    original_policy = toy()["spec"]["syncPolicy"]["automated"]
    held = original_replicas + 1
    args = ["start", "--app", "toy", "--context", CTX, "--target", "deployment/guestbook-ui", "-n", "toy",
            "--patch", json.dumps({"spec": {"replicas": held}}), "--type", "merge", "--yes",
            "--interval", "60", "--detach"]
    try:
        r = argo_hold(*args, state_dir=state_dir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert dep()["spec"]["replicas"] == held
        assert auto_sync_absent()
        kubectl("-n", "argo-cd", "patch", "applications.argoproj.io", "toy", "--type", "merge",
                "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}')
        elapsed = wait_for(lambda: auto_sync_absent() and dep()["spec"]["replicas"] == held, deadline=20)
        assert elapsed is not None, "holder did not restore both the disabled auto-sync and the replicas"
        print(f"re-disable + re-apply observed after {elapsed:.1f}s")

        counts = {"reenable": 0, "reapply": 0}

        def reenables():
            st = argo_hold("status", "--app", "toy", state_dir=state_dir)
            m = re.search(r"activity:\s+(\d+) auto-sync re-disable\(s\), (\d+) re-apply", st.stdout)
            if m:
                counts.update(reenable=int(m.group(1)), reapply=int(m.group(2)))
            return counts["reenable"]

        # counters reach state.json only on the heartbeat tick (every --interval = 60 s), so status lags
        # the watch-path reaction by up to one interval: poll past one full interval
        assert wait_for(lambda: reenables() >= 1, deadline=75, step=2) is not None, "status reports no re-disable"
        print(f"status activity: {counts['reenable']} re-disable(s), {counts['reapply']} re-apply(s)")
    finally:
        rel = argo_hold("release", "--app", "toy", state_dir=state_dir)
    assert rel.returncode == 0, rel.stdout + rel.stderr
    assert dep()["spec"]["replicas"] == original_replicas
    assert toy()["spec"]["syncPolicy"]["automated"] == original_policy


def test_real_server_side_apply_creates_and_release_deletes(state_dir, tmp_path):
    assert "argo-hold-it" not in kubectl("-n", "toy", "get", "configmap", "-o", "name")
    cm = tmp_path / "cm.yaml"
    cm.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: argo-hold-it\n  namespace: toy\ndata:\n  a: '1'\n")
    try:
        r = argo_hold("start", "--app", "toy", "--context", CTX, "-f", str(cm), "--yes", "--interval", "5",
                      "--detach", state_dir=state_dir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "argo-hold-it" in kubectl("-n", "toy", "get", "configmap", "-o", "name")
        data = json.loads(kubectl("-n", "toy", "get", "configmap", "argo-hold-it", "-o", "json"))["data"]
        assert data["a"] == "1"
    finally:
        rel = argo_hold("release", "--app", "toy", state_dir=state_dir)
    assert rel.returncode == 0, rel.stdout + rel.stderr
    assert "argo-hold-it" not in kubectl("-n", "toy", "get", "configmap", "-o", "name")


def sync_status():
    return toy()["status"]["sync"]["status"]


def test_real_freeze_holds_a_pending_change_back_until_release(state_dir):
    """A freeze applies no change. While it holds, a change that Argo would sync (a drifted git-managed
    field, here replicas) stays unsynced; release turns auto-sync back on and Argo then syncs it."""
    original_replicas = dep()["spec"]["replicas"]
    original_policy = toy()["spec"]["syncPolicy"]["automated"]
    drifted = original_replicas + 1
    try:
        r = argo_hold("start", "--app", "toy", "--context", CTX, "--freeze", "--yes", "--interval", "5",
                      "--detach", state_dir=state_dir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert auto_sync_absent()
        assert dep()["spec"]["replicas"] == original_replicas           # a freeze changes no object
        st = argo_hold("status", "--app", "toy", state_dir=state_dir)
        assert "mode:        freeze (no change)" in st.stdout, st.stdout
        kubectl("-n", "toy", "scale", "deployment", "guestbook-ui", f"--replicas={drifted}")
        assert wait_for(lambda: sync_status() == "OutOfSync", deadline=60) is not None
        time.sleep(15)                                                  # long enough for selfHeal to have acted
        assert dep()["spec"]["replicas"] == drifted and sync_status() == "OutOfSync"
        assert auto_sync_absent()
    finally:
        rel = argo_hold("release", "--app", "toy", state_dir=state_dir)
    assert rel.returncode == 0, rel.stdout + rel.stderr
    assert "NOTICE" in rel.stdout and "syncs at once" in rel.stdout
    assert toy()["spec"]["syncPolicy"]["automated"] == original_policy
    elapsed = wait_for(lambda: dep()["spec"]["replicas"] == original_replicas, deadline=90)
    assert elapsed is not None, "Argo did not sync the pending change after release"
    print(f"pending change synced {elapsed:.1f}s after release returned")


def test_real_freeze_redisables_auto_sync_when_something_reenables_it(state_dir):
    """Re-enable only; no drift is present, so nothing for Argo to sync in the race window."""
    original_policy = toy()["spec"]["syncPolicy"]["automated"]
    try:
        r = argo_hold("start", "--app", "toy", "--context", CTX, "--freeze", "--yes", "--interval", "5",
                      "--detach", state_dir=state_dir)
        assert r.returncode == 0, r.stdout + r.stderr
        assert auto_sync_absent()
        kubectl("-n", "argo-cd", "patch", "applications.argoproj.io", "toy", "--type", "merge",
                "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}')
        assert wait_for(auto_sync_absent, deadline=15) is not None, "holder did not re-disable auto-sync"
    finally:
        rel = argo_hold("release", "--app", "toy", state_dir=state_dir)
    assert rel.returncode == 0, rel.stdout + rel.stderr
    assert toy()["spec"]["syncPolicy"]["automated"] == original_policy
