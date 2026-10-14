"""`argo-hold start --freeze`: hold auto-sync off, apply nothing, restore on release."""
import json
import signal
import subprocess
import threading
import time
from pathlib import Path

import ah_hold
import ah_ops
import ah_plan
import ah_state
import pytest
from ah_kube import APP_KIND
from ah_ops import AUTOMATED_OFF
from ah_paths import ABSENT, get_at
from conftest import AFFINITY, wait_for
from fakes.fakecluster import CTX, make_app

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "argo-hold"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "state"
AUTO = ["spec", "syncPolicy", "automated"]
AFF = ["spec", "template", "spec", "affinity"]
ORIGINAL = {"prune": True, "selfHeal": True}
FREEZE = {"kind": "freeze"}
FREEZE_START = ["start", "--app", "otel-collector", "--context", CTX, "--freeze", "--yes", "--interval", "1"]
PATCH_START = ["start", "--app", "otel-collector", "--context", CTX, "--target", "daemonset/otel-collector-logs",
               "-n", "monitoring", "--patch", json.dumps(AFFINITY), "--type", "strategic", "--yes",
               "--interval", "1"]


def run(*args, **kw):
    return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, **kw)


def spawn(*args):
    p = subprocess.Popen([str(LAUNCHER), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=p.wait, daemon=True).start()     # reap, so the pid does not linger as a zombie
    return p


def app(cluster, name="otel-collector"):
    return cluster.get(CTX, "applications.argoproj.io", "argo-cd", name)


def ds(cluster):
    return cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")


def holding(name="otel-collector"):
    p = ah_state.load(name)
    return bool(p and p.get("phase") == "holding")


def verbs(cluster, *wanted):
    return [c for c in cluster.calls() if c[2] in wanted]


def freeze_begin(kube, name="otel-collector", wait_op=0, ttl=7200):
    plan = ah_plan.make_plan(kube, name, "argo-cd", FREEZE, [])
    return ah_hold.begin(kube, app=name, argocd_ns="argo-cd", context=CTX, change=FREEZE, specs=[],
                         confirmed_plan=plan, ttl=ttl, interval=60, wait_op=wait_op, sleep=lambda s: None)


# -- argument validation -------------------------------------------------------------------
@pytest.mark.parametrize("extra", [["--patch", "{}"], ["--patch-file", "p.json"], ["-f", "m.yaml"]])
def test_freeze_is_exclusive_with_the_other_change_modes(env, cluster, extra):
    r = run(*FREEZE_START, *extra)
    assert r.returncode == 2 and "not allowed with" in r.stderr


def test_one_of_the_four_modes_is_still_required(env, cluster):
    r = run("start", "--app", "otel-collector", "--context", CTX, "--yes")
    assert r.returncode == 2 and "--freeze" in r.stderr


@pytest.mark.parametrize("extra", [["--target", "daemonset/x"], ["-n", "monitoring"], ["--type", "merge"]])
def test_freeze_rejects_target_namespace_and_type(env, cluster, extra):
    r = run(*FREEZE_START, "--dry-run", *extra)
    assert r.returncode == 1 and "--freeze changes no objects" in r.stderr
    assert "Traceback" not in r.stderr
    assert get_at(app(cluster), AUTO) == ORIGINAL and ah_state.load("otel-collector") is None


def test_start_help_documents_freeze():
    help_text = " ".join(run("start", "--help").stdout.split())
    assert "--freeze" in help_text and "auto-sync" in help_text


# -- plan output ---------------------------------------------------------------------------
def test_dry_run_plan_says_what_a_freeze_does_and_what_release_does(env, cluster):
    r = run(*FREEZE_START, "--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "context:   test-ctx" in out and "app:       otel-collector (namespace argo-cd)" in out
    assert "on-call:   #on-call" in out
    assert "mode:      freeze (no change is applied; only auto-sync is turned off)" in out
    assert "auto-sync: will be turned OFF" in out
    assert "EVERY change pending in git for otel-collector syncs at once" in out
    assert "hard refresh" in out
    assert "target:" not in out and "dry-run: nothing was changed" in out
    assert "selfHeal" not in out                                  # values are never printed
    assert get_at(app(cluster), AUTO) == ORIGINAL and ah_state.load("otel-collector") is None
    assert not verbs(cluster, "patch", "apply", "annotate", "delete")


def test_dry_run_plan_when_auto_sync_is_already_off(env, cluster):
    cluster.seed(CTX, make_app("quiet", automated="off"))
    r = run("start", "--app", "quiet", "--context", CTX, "--freeze", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "auto-sync: already off" in r.stdout
    assert "release:   leaves auto-sync off" in r.stdout
    assert "EVERY" not in r.stdout


def test_declining_the_prompt_changes_nothing(env, cluster):
    r = run(*[a for a in FREEZE_START if a != "--yes"], input="n\n")
    assert r.returncode == 1 and "aborted" in r.stdout and "mode:      freeze" in r.stdout
    assert get_at(app(cluster), AUTO) == ORIGINAL and ah_state.load("otel-collector") is None


def test_dry_run_of_a_missing_application_is_a_clean_error(env, cluster):
    r = run("start", "--app", "nope", "--context", CTX, "--freeze", "--dry-run")
    assert r.returncode == 1 and "not found" in r.stderr and "Traceback" not in r.stderr


# -- safety rules --------------------------------------------------------------------------
def test_denylisted_app_is_refused_without_allow_sensitive(env, cluster):
    cluster.seed(CTX, make_app("app-root"))
    args = ["start", "--app", "app-root", "--context", CTX, "--freeze", "--dry-run"]
    r = run(*args)
    assert r.returncode == 1 and "denylist" in r.stderr
    ok = run(*args, "--allow-sensitive")
    assert ok.returncode == 0 and "context:   test-ctx" in ok.stdout and "app-root" in ok.stdout


def test_one_hold_per_app_for_freeze(env, cluster):
    p = spawn(*FREEZE_START)
    try:
        assert wait_for(holding, 10)
        second = run(*FREEZE_START)
        assert second.returncode == 1 and "already" in second.stderr
        patch = run(*PATCH_START)
        assert patch.returncode == 1 and "already" in patch.stderr
        assert ah_state.load("otel-collector")["change"] == FREEZE
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


def test_a_freeze_cannot_be_started_over_a_patch_hold(env, cluster):
    p = spawn(*PATCH_START)
    try:
        assert wait_for(holding, 10)
        r = run(*FREEZE_START)
        assert r.returncode == 1 and "already" in r.stderr
        assert get_at(ds(cluster), AFF) != ABSENT
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


def test_begin_refuses_a_second_freeze(kube, cluster):
    freeze_begin(kube)
    with pytest.raises(ah_state.HoldExists):
        freeze_begin(kube)
    assert ah_state.load("otel-collector")["phase"] == "holding"


# -- state file and start ------------------------------------------------------------------
def test_begin_records_a_freeze_and_writes_only_the_application(kube, cluster):
    payload = freeze_begin(kube)
    assert get_at(app(cluster), AUTO) == ABSENT
    saved = ah_state.load("otel-collector")
    assert saved["change"] == FREEZE and saved["targets"] == []
    assert saved["phase"] == "holding" and saved["automated_was_on"] is True
    assert saved["original_automated"] == ORIGINAL
    assert saved["reapply_count"] == 0 and saved["reenable_count"] == 0
    assert payload["change"] == FREEZE
    writes = verbs(cluster, "patch", "apply", "delete", "annotate")
    assert len(writes) == 1 and APP_KIND in writes[0] and "--field-manager" in writes[0]
    assert get_at(ds(cluster), AFF) == ABSENT                       # nothing else was touched


def test_begin_does_not_touch_an_app_whose_auto_sync_is_already_off(kube, cluster):
    cluster.seed(CTX, make_app("quiet", automated="off"))
    payload = freeze_begin(kube, "quiet")
    assert payload["automated_was_on"] is False and payload["original_automated"] == ABSENT
    assert not verbs(cluster, "patch", "apply")


def test_begin_waits_for_a_running_sync_then_rolls_back_on_timeout(kube, cluster):
    cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "otel-collector",
                   lambda o: o["status"].update(operationState={"phase": "Running"}))
    with pytest.raises(ah_plan.HoldError, match="Running"):
        freeze_begin(kube, wait_op=0)
    assert get_at(app(cluster), AUTO) == ORIGINAL            # policy restored by the rollback
    assert ah_state.load("otel-collector") is None           # lock released


def test_hold_kind_defaults_to_patch_for_old_state():
    assert ah_state.hold_kind({}) == "patch"
    assert ah_state.hold_kind({"change": {}}) == "patch"
    assert ah_state.hold_kind({"change": {"kind": "manifest"}}) == "manifest"
    assert ah_state.hold_kind({"change": FREEZE}) == "freeze"


def test_cli_freeze_writes_the_expected_state_file(env, cluster):
    p = spawn(*FREEZE_START)
    try:
        assert wait_for(holding, 10)
        s = ah_state.load("otel-collector")
        assert s["change"] == {"kind": "freeze"} and s["targets"] == []
        assert s["original_automated"] == ORIGINAL and s["context"] == CTX
        assert get_at(app(cluster), AUTO) == ABSENT
        assert oct(ah_state.state_path("otel-collector").stat().st_mode & 0o777) == "0o600"
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


# -- the reactor in freeze mode -------------------------------------------------------------
def test_reenabled_auto_sync_is_removed_again_in_freeze_mode(kube, cluster):
    p = freeze_begin(kube)
    h = ah_hold.Holder(kube, p, resolution=0.05)
    kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
               {"spec": {"syncPolicy": {"automated": {"prune": True}}}}, "argo-rollouts")
    assert h.on_app(app(cluster)) is True
    assert get_at(app(cluster), AUTO) == ABSENT and p["reenable_count"] == 1
    assert "WARNING" in ah_state.log_path("otel-collector").read_text()
    assert h.on_app(app(cluster)) is False                   # already off: no write


def test_holder_watches_only_the_application_and_reacts(kube, cluster):
    p = freeze_begin(kube)
    h = ah_hold.Holder(kube, p, resolution=0.05)
    out = {}
    th = threading.Thread(target=lambda: out.update(reason=h.run()))
    th.start()
    try:
        time.sleep(0.3)
        kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
                   {"spec": {"syncPolicy": {"automated": {"prune": True}}}}, "x")
        assert wait_for(lambda: get_at(app(cluster), AUTO) == ABSENT)
        watches = [c for c in cluster.calls() if c[2] == "get" and "-w" in c]
        assert len(watches) >= 1 and all(APP_KIND in c for c in watches)
    finally:
        h.stop("test over")
        th.join(timeout=10)
    assert out["reason"] == "test over" and not th.is_alive()


def test_resync_in_freeze_mode_needs_no_targets(kube, cluster):
    p = freeze_begin(kube)
    h = ah_hold.Holder(kube, p, resolution=0.05)
    h.resync()                                                # no target to read, no crash
    assert not h.stop_event.is_set()


def test_freeze_holder_ends_when_the_application_is_deleted(kube):
    h = ah_hold.Holder(kube, freeze_begin(kube), resolution=0.05)
    h.dispatch(("app", None), {"type": "DELETED", "object": {}})
    assert h.stop_reason == "application deleted"


# -- release -------------------------------------------------------------------------------
def test_release_restores_the_original_policy_and_requests_a_hard_refresh(kube, cluster):
    payload = freeze_begin(kube)
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])
    assert get_at(app(cluster), AUTO) == ORIGINAL
    assert app(cluster)["metadata"]["annotations"]["argocd.argoproj.io/refresh"] == "hard"
    assert app(cluster)["spec"]["syncPolicy"]["syncOptions"] == ["CreateNamespace=true"]
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])      # idempotent


def test_release_with_nothing_to_restore_when_auto_sync_was_off(kube, cluster):
    cluster.seed(CTX, make_app("quiet", automated="off"))
    payload = freeze_begin(kube, "quiet")
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])
    assert get_at(app(cluster, "quiet"), AUTO) == ABSENT
    assert not verbs(cluster, "patch", "annotate")           # no restore, no refresh
    assert "annotations" not in app(cluster, "quiet")["metadata"]


def test_release_does_not_clobber_a_policy_someone_else_set(kube, cluster):
    payload = freeze_begin(kube)
    kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
               {"spec": {"syncPolicy": {"automated": {"prune": True}}}}, "someone")
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors == [] and any("--force" in w for w in warnings)
    assert get_at(app(cluster), AUTO) == {"prune": True}
    assert not verbs(cluster, "annotate")                    # no refresh after a skipped policy
    assert ah_ops.release_all(kube, payload, True, lambda m: None) == ([], [])
    assert get_at(app(cluster), AUTO) == ORIGINAL


def test_release_notice_text():
    on = {"app": "app-root", "change": FREEZE, "original_automated": ORIGINAL}
    off = {"app": "app-root", "change": FREEZE, "original_automated": ABSENT}
    patch = {"app": "app-root", "change": {"kind": "patch"}, "original_automated": ORIGINAL}
    assert "every change pending in git syncs at once" in ah_plan.release_notice(on)
    assert "app-root" in ah_plan.release_notice(on) and "hard refresh" in ah_plan.release_notice(on)
    assert "leaves it off" in ah_plan.release_notice(off)
    assert ah_plan.release_notice(patch) is None


def test_cli_release_signals_a_live_holder_and_says_what_will_sync(env, cluster):
    p = spawn(*FREEZE_START)
    assert wait_for(holding, 10)
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTICE" in r.stdout and "syncs at once" in r.stdout
    assert p.wait(timeout=20) == 0
    assert get_at(app(cluster), AUTO) == ORIGINAL
    assert app(cluster)["metadata"]["annotations"]["argocd.argoproj.io/refresh"] == "hard"
    assert ah_state.load("otel-collector") is None
    log = ah_state.log_path("otel-collector").read_text()
    assert "syncs at once" in log and "selfHeal" not in log      # the holder logs it too, with no values


def test_release_when_the_holder_is_dead_works_from_the_state_file(env, cluster):
    p = spawn(*FREEZE_START)
    assert wait_for(holding, 10)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=10)
    assert get_at(app(cluster), AUTO) == ABSENT              # left off
    st = run("status", "--app", "otel-collector")
    assert st.returncode == 3 and "dead-pid" in st.stdout and "freeze (no change)" in st.stdout
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTICE" in r.stdout and "syncs at once" in r.stdout
    assert get_at(app(cluster), AUTO) == ORIGINAL
    assert app(cluster)["metadata"]["annotations"]["argocd.argoproj.io/refresh"] == "hard"
    assert ah_state.load("otel-collector") is None


def test_cli_release_when_auto_sync_was_originally_off_restores_nothing(env, cluster):
    cluster.seed(CTX, make_app("quiet", automated="off"))
    args = ["start", "--app", "quiet", "--context", CTX, "--freeze", "--yes", "--interval", "1"]
    p = spawn(*args)
    assert wait_for(lambda: holding("quiet"), 10)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=10)
    r = run("release", "--app", "quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "leaves it off" in r.stdout and "syncs at once" not in r.stdout
    assert get_at(app(cluster, "quiet"), AUTO) == ABSENT
    assert not verbs(cluster, "annotate")
    assert ah_state.load("quiet") is None


def test_ttl_expiry_releases_a_freeze(env, cluster):
    p = spawn(*FREEZE_START, "--ttl", "2s")
    assert p.wait(timeout=30) == 0
    assert get_at(app(cluster), AUTO) == ORIGINAL
    assert ah_state.load("otel-collector") is None
    assert "syncs at once" in ah_state.log_path("otel-collector").read_text()


def test_detached_freeze_holds_until_released(env, cluster):
    r = run(*FREEZE_START, "--detach")
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert holding() and get_at(app(cluster), AUTO) == ABSENT
    finally:
        run("release", "--app", "otel-collector")
    assert get_at(app(cluster), AUTO) == ORIGINAL


# -- status and list -----------------------------------------------------------------------
def test_status_and_list_show_a_freeze(env, cluster):
    p = spawn(*FREEZE_START)
    try:
        assert wait_for(holding, 10)
        s = run("status", "--app", "otel-collector")
        assert s.returncode == 0, s.stdout + s.stderr
        assert "mode:        freeze (no change)" in s.stdout
        assert "ttl left:" in s.stdout and "heartbeat:" in s.stdout
        assert "0 auto-sync re-disable(s)" in s.stdout and "re-apply" not in s.stdout
        assert "live:        auto-sync off" in s.stdout
        lst = run("list")
        line = [x for x in lst.stdout.splitlines() if x.startswith("otel-collector")][0]
        assert "freeze (no change)" in line and "ttl " in line
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


def test_status_counts_re_disables_in_a_freeze(env, cluster):
    p = spawn(*FREEZE_START)
    try:
        assert wait_for(holding, 10)
        cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "otel-collector",
                       lambda o: o["spec"]["syncPolicy"].update(automated={"prune": True}))
        assert wait_for(lambda: get_at(app(cluster), AUTO) == ABSENT, 5)
        assert wait_for(lambda: ah_state.load("otel-collector")["reenable_count"] >= 1, 5)
        assert "re-disable(s)" in run("status", "--app", "otel-collector").stdout
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


def test_patch_hold_output_is_unchanged(env, cluster):
    p = spawn(*PATCH_START)
    try:
        assert wait_for(holding, 10)
        s = run("status", "--app", "otel-collector").stdout
        assert "mode:" not in s and "freeze" not in s and "re-apply(s)" in s
        line = [x for x in run("list").stdout.splitlines() if x.startswith("otel-collector")][0]
        assert "freeze" not in line and line.rstrip().endswith("ttl 1h 59m")
        r = run("release", "--app", "otel-collector")
        assert r.returncode == 0 and "NOTICE" not in r.stdout
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0
    assert "NOTICE" not in ah_state.log_path("otel-collector").read_text()


# -- two holds at once ---------------------------------------------------------------------
@pytest.mark.parametrize("first", ["app-root", "otel-collector"])
def test_freeze_and_patch_hold_on_different_apps_run_together(env, cluster, first):
    cluster.seed(CTX, make_app("app-root"))
    freeze = spawn("start", "--app", "app-root", "--context", CTX, "--freeze", "--allow-sensitive",
                   "--yes", "--interval", "1")
    patch = spawn(*PATCH_START)
    procs = {"app-root": freeze, "otel-collector": patch}
    try:
        assert wait_for(lambda: holding("app-root") and holding("otel-collector"), 15)
        assert get_at(app(cluster, "app-root"), AUTO) == ABSENT
        assert get_at(app(cluster), AUTO) == ABSENT and get_at(ds(cluster), AFF) != ABSENT
        assert ah_state.load("app-root")["change"] == FREEZE
        assert ah_state.load("otel-collector")["change"]["kind"] == "patch"
        listing = run("list").stdout
        assert "app-root" in listing and "otel-collector" in listing
        assert [x for x in listing.splitlines() if x.startswith("app-root")][0].count("freeze") == 1
        # each holder defends its own app and nothing else
        cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "app-root",
                       lambda o: o["spec"]["syncPolicy"].update(automated={"prune": True}))
        cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                       lambda o: o["spec"]["template"]["spec"].pop("affinity"))
        assert wait_for(lambda: get_at(app(cluster, "app-root"), AUTO) == ABSENT, 5)
        assert wait_for(lambda: get_at(ds(cluster), AFF) != ABSENT, 5)
        # release one; the other must be untouched and still held
        second = "otel-collector" if first == "app-root" else "app-root"
        r1 = run("release", "--app", first)
        assert r1.returncode == 0, r1.stdout + r1.stderr
        assert procs[first].wait(timeout=20) == 0
        assert ah_state.load(first) is None and holding(second)
        assert procs[second].poll() is None
        if first == "app-root":
            assert get_at(app(cluster, "app-root"), AUTO) == ORIGINAL
            assert get_at(app(cluster), AUTO) == ABSENT and get_at(ds(cluster), AFF) != ABSENT
        else:
            assert get_at(ds(cluster), AFF) == ABSENT and get_at(app(cluster), AUTO) == ORIGINAL
            assert get_at(app(cluster, "app-root"), AUTO) == ABSENT
        r2 = run("release", "--app", second)
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert procs[second].wait(timeout=20) == 0
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
    assert get_at(app(cluster, "app-root"), AUTO) == ORIGINAL
    assert get_at(app(cluster), AUTO) == ORIGINAL and get_at(ds(cluster), AFF) == ABSENT
    assert ah_state.list_holds() == []


# -- backward compatibility ----------------------------------------------------------------
def test_state_file_written_by_the_previous_version_still_works(env, cluster, kube):
    """patch_hold_v1.json was written by `argo-hold start` at c12a0bd, before --freeze existed
    (hostname and pid scrubbed so no real process can match). It has no freeze marker."""
    payload = json.loads((FIXTURES / "patch_hold_v1.json").read_text())
    assert payload["change"]["kind"] == "patch" and payload["phase"] == "holding"
    kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge", AUTOMATED_OFF, "argo-hold")
    kube.patch("daemonset", "otel-collector-logs", "monitoring", "strategic", AFFINITY, "argo-hold")
    ah_state.create("otel-collector", payload)
    status = run("status", "--app", "otel-collector")
    assert status.returncode == 3 and "mode:" not in status.stdout and "re-apply(s)" in status.stdout
    assert "freeze" not in run("list").stdout
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTICE" not in r.stdout
    assert get_at(ds(cluster), AFF) == ABSENT
    assert get_at(app(cluster), AUTO) == ORIGINAL
    assert ah_state.load("otel-collector") is None
