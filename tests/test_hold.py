import threading
import time

import ah_hold
import ah_plan
import ah_state
import pytest
from ah_kube import APP_KIND
from ah_paths import ABSENT, get_at
from conftest import AFFINITY, DS_SPEC, PATCH_CHANGE, wait_for
from fakes.fakecluster import CTX

AFF = ["spec", "template", "spec", "affinity"]


def ds(cluster):
    return cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")


def app(cluster, name="otel-collector"):
    return cluster.get(CTX, "applications.argoproj.io", "argo-cd", name)


def start(kube, wait_op=0, **kw):
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    return ah_hold.begin(kube, app="otel-collector", argocd_ns="argo-cd", context=CTX,
                         change=PATCH_CHANGE, specs=DS_SPEC, confirmed_plan=plan, ttl=7200,
                         interval=60, wait_op=wait_op, sleep=lambda s: None, **kw)


def test_begin_disables_auto_sync_applies_and_records_state(kube, cluster):
    p = start(kube)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT
    assert get_at(ds(cluster), AFF) != ABSENT
    saved = ah_state.load("otel-collector")
    assert saved["phase"] == "holding" and saved["original_automated"] == {"prune": True, "selfHeal": True}
    assert p["targets"][0]["paths"][0]["path"] == AFF


def test_begin_uses_field_manager_and_never_client_side_apply(kube, cluster):
    start(kube)
    calls = cluster.calls()
    writes = [c for c in calls if c[2] in ("patch", "apply")]
    assert writes and all("--field-manager" in c and "argo-hold" in c for c in writes)
    assert all("--server-side" in c for c in writes if c[2] == "apply")


def test_begin_refuses_a_second_hold(kube, cluster):
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    kw = dict(app="otel-collector", argocd_ns="argo-cd", context=CTX, change=PATCH_CHANGE, specs=DS_SPEC,
              confirmed_plan=plan, ttl=60, interval=60, wait_op=0, sleep=lambda s: None)
    ah_hold.begin(kube, **kw)
    with pytest.raises(ah_state.HoldExists):
        ah_hold.begin(kube, **kw)
    assert get_at(ds(cluster), AFF) != ABSENT          # the first hold is untouched
    assert ah_state.load("otel-collector")["phase"] == "holding"


def test_begin_waits_for_running_operation_then_rolls_back_on_timeout(kube, cluster):
    cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "otel-collector",
                   lambda o: o["status"].update(operationState={"phase": "Running"}))
    with pytest.raises(ah_plan.HoldError, match="Running"):
        start(kube, wait_op=0)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}
    assert get_at(ds(cluster), AFF) == ABSENT                  # nothing applied
    assert ah_state.load("otel-collector") is None             # lock released


def test_begin_proceeds_once_operation_finishes(kube, cluster):
    cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "otel-collector",
                   lambda o: o["status"].update(operationState={"phase": "Running"}))
    polls = []

    def sleep(_):
        polls.append(1)
        cluster.mutate(CTX, "applications.argoproj.io", "argo-cd", "otel-collector",
                       lambda o: o["status"].update(operationState={"phase": "Succeeded"}))

    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    ah_hold.begin(kube, app="otel-collector", argocd_ns="argo-cd", context=CTX, change=PATCH_CHANGE,
                  specs=DS_SPEC, confirmed_plan=plan, ttl=60, interval=60, wait_op=60, sleep=sleep)
    assert polls and get_at(ds(cluster), AFF) != ABSENT


def test_begin_rolls_back_when_apply_fails(kube, cluster):
    # patch calls in order: dry-run in start(), dry-run in begin(), disable auto-sync, apply.
    cluster.fail_next("patch", n=1, skip=3)      # fail only the apply
    with pytest.raises(Exception):
        start(kube)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}
    assert get_at(ds(cluster), AFF) == ABSENT
    assert ah_state.load("otel-collector") is None


def test_begin_aborts_if_cluster_changed_while_confirming(kube, cluster):
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    kube.patch("daemonset", "otel-collector-logs", "monitoring", "strategic", AFFINITY, "x")  # now a no-op
    with pytest.raises(ah_plan.HoldError):
        ah_hold.begin(kube, app="otel-collector", argocd_ns="argo-cd", context=CTX, change=PATCH_CHANGE,
                      specs=DS_SPEC, confirmed_plan=plan, ttl=60, interval=60, wait_op=0,
                      sleep=lambda s: None)
    assert ah_state.load("otel-collector") is None


def holder(kube, clock=None, **kw):
    p = start(kube)
    now = (lambda: clock[0]) if clock else time.time
    return ah_hold.Holder(kube, p, now=now, resolution=0.05, **kw), p


def test_reenabled_auto_sync_is_removed_again_loudly(kube, cluster):
    h, p = holder(kube)
    kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
               {"spec": {"syncPolicy": {"automated": {"prune": True}}}}, "argo-rollouts")
    assert h.on_app(app(cluster)) is True
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT
    assert p["reenable_count"] == 1
    assert "WARNING" in ah_state.log_path("otel-collector").read_text()
    assert h.on_app(app(cluster)) is False         # already off: no write


def test_drift_is_reapplied(kube, cluster):
    h, p = holder(kube)
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].pop("affinity"))   # a sync reverted it
    assert h.on_target(p["targets"][0], ds(cluster)) is True
    assert get_at(ds(cluster), AFF) != ABSENT
    assert p["reapply_count"] == 1
    assert h.on_target(p["targets"][0], ds(cluster)) is False   # in place: nothing to do


def test_status_only_event_does_not_write(kube, cluster):
    h, p = holder(kube)
    patches = lambda: len([c for c in cluster.calls() if c[2] == "patch"])   # noqa: E731
    before = patches()
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["status"].update(numberReady=3))
    h.dispatch(("target", 0), {"type": "MODIFIED", "object": ds(cluster)})
    assert patches() == before


def test_reapply_is_rate_limited(kube, cluster):
    clock = [1000.0]
    h, p = holder(kube, clock)
    t = p["targets"][0]
    results = []
    for _ in range(7):
        cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                       lambda o: o["spec"]["template"]["spec"].pop("affinity", None))
        results.append(h.on_target(t, ds(cluster)))
    assert results == [True] * 5 + [False, False]
    assert "rate limit" in ah_state.log_path("otel-collector").read_text()
    clock[0] += 61
    assert h.on_target(t, ds(cluster)) is True      # window passed


def test_target_deleted_with_patch_change_warns(kube, cluster):
    h, p = holder(kube)
    cluster.delete(CTX, "daemonset", "monitoring", "otel-collector-logs")
    assert h.on_target(p["targets"][0], None) is False      # no crash
    assert "cannot re-apply a patch" in ah_state.log_path("otel-collector").read_text()


def test_heartbeat_and_ttl_in_housekeeping(kube):
    clock = [1000.0]
    h, p = holder(kube, clock)
    p["expires_at"] = 1000.0 + 100                 # interval is 60: first heartbeat is due at 1060
    clock[0] = 1061.0
    h.housekeeping()
    assert ah_state.load("otel-collector")["heartbeat_at"] == 1061.0
    assert not h.stop_event.is_set()
    clock[0] = 1101.0
    h.housekeeping()
    assert h.stop_event.is_set() and h.stop_reason == "ttl expired"


def test_application_deleted_event_stops_hold(kube):
    h, _ = holder(kube)
    h.dispatch(("app", None), {"type": "DELETED", "object": {}})
    assert h.stop_reason == "application deleted"


def test_unreachable_kubectl_during_event_does_not_crash(kube, cluster):
    h, p = holder(kube)
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].pop("affinity"))
    cluster.fail_next("patch", n=1)
    h.dispatch(("target", 0), {"type": "MODIFIED", "object": ds(cluster)})   # logs, continues
    assert not h.stop_event.is_set()


def test_run_reacts_to_watch_events_end_to_end(kube, cluster):
    h, p = holder(kube)
    out = {}
    th = threading.Thread(target=lambda: out.update(reason=h.run()))
    th.start()
    try:
        time.sleep(0.3)
        cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                       lambda o: o["spec"]["template"]["spec"].pop("affinity"))
        assert wait_for(lambda: get_at(ds(cluster), AFF) != ABSENT)          # re-applied
        kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
                   {"spec": {"syncPolicy": {"automated": {"prune": True}}}}, "x")
        assert wait_for(lambda: get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT)
    finally:
        h.stop("test over")
        th.join(timeout=10)
    assert out["reason"] == "test over" and not th.is_alive()


def test_malformed_events_are_logged_and_skipped(kube):
    h, _ = holder(kube)
    h.dispatch(("target", 0), {"type": "MODIFIED"})        # no "object" key
    h.dispatch(("app", None), {"type": "ADDED"})
    assert not h.stop_event.is_set()
    log = ah_state.log_path("otel-collector").read_text()
    assert "ERROR" in log and "KeyError" in log


def test_malformed_event_values_never_reach_the_log(kube):
    h, _ = holder(kube)
    h.dispatch(("target", 99), {"type": "MODIFIED", "object": {"spec": {"token": "s3cr3t"}},
                                "extra": "s3cr3t"})         # no such target: IndexError
    h.dispatch(("app", None), {"type": "s3cr3t"})           # odd type string, no object
    assert not h.stop_event.is_set()
    log = ah_state.log_path("otel-collector").read_text()
    assert "IndexError" in log and "s3cr3t" not in log


@pytest.mark.parametrize("tag, ev", [(("target", 0), []), (("app", None), "s3cr3t-event"),
                                     (("target", 0), 1), (("app", None), None)])
def test_non_dict_watch_documents_are_logged_and_skipped(kube, tag, ev):
    h, _ = holder(kube)
    h.dispatch(tag, ev)                                    # must not raise
    assert not h.stop_event.is_set()
    log = ah_state.log_path("otel-collector").read_text()
    assert "ERROR" in log and "AttributeError" in log
    assert "s3cr3t" not in log and "[]" not in log
