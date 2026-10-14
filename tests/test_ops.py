import ah_ops
import pytest
from ah_kube import APP_KIND
from ah_paths import ABSENT, get_at, normalize
from conftest import DS_SPEC, PATCH_CHANGE
from fakes.fakecluster import CTX, make_app

AFF = ["spec", "template", "spec", "affinity"]


def ds(cluster):
    return cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")


def app(cluster, name="otel-collector"):
    return cluster.get(CTX, "applications.argoproj.io", "argo-cd", name)


def test_start_ops_disable_auto_sync_and_apply(held, cluster):
    held()
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT
    assert get_at(ds(cluster), AFF) != ABSENT


def test_release_reverts_change_and_restores_policy(held, kube, cluster):
    original = normalize(ds(cluster))
    payload = held()
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert (warnings, errors) == ([], [])
    assert normalize(ds(cluster)) == original
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}
    assert app(cluster)["metadata"]["annotations"]["argocd.argoproj.io/refresh"] == "hard"
    assert app(cluster)["spec"]["syncPolicy"]["syncOptions"] == ["CreateNamespace=true"]


def test_release_is_idempotent(held, kube, cluster):
    payload = held()
    ah_ops.release_all(kube, payload, False, lambda m: None)
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])


def test_field_changed_by_someone_else_is_not_clobbered(held, kube, cluster):
    payload = held()
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].update(affinity={"theirs": True}))
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors == []
    assert any("changed by someone else" in w and "--force" in w for w in warnings)
    assert get_at(ds(cluster), AFF) == {"theirs": True}           # untouched
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) != ABSENT  # policy still restored


def test_force_overrides_the_conflict(held, kube, cluster):
    payload = held()
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].update(affinity={"theirs": True}))
    warnings, _ = ah_ops.release_all(kube, payload, True, lambda m: None)
    assert warnings == []
    assert get_at(ds(cluster), AFF) == ABSENT


def test_warnings_never_contain_values(held, kube, cluster):
    payload = held()
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].update(affinity={"token": "s3cr3t"}))
    warnings, _ = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert warnings and not any("s3cr3t" in w for w in warnings)


def test_created_object_is_deleted_on_release(kube, cluster):
    import time

    import ah_plan
    from conftest import wait_for  # noqa: F401
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "new", "namespace": "monitoring"}, "data": {"a": "1"}}
    specs = [{"kind": "configmap", "name": "new", "namespace": "monitoring", "manifest": cm}]
    change = {"kind": "manifest"}
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", change, specs)
    payload = ah_plan.new_payload("otel-collector", CTX, "argo-cd", change, plan, 60, 60, time.time())
    ah_ops.disable_auto_sync(kube, payload)
    ah_ops.apply_change(kube, payload, payload["targets"][0])
    assert cluster.get(CTX, "configmap", "monitoring", "new") is not None
    ah_ops.release_all(kube, payload, False, lambda m: None)
    assert cluster.get(CTX, "configmap", "monitoring", "new") is None


def test_manifest_on_existing_object_is_a_merge_patch_and_reverts(kube, cluster):
    import time

    import ah_plan
    m = {"apiVersion": "apps/v1", "kind": "DaemonSet",
         "metadata": {"name": "otel-collector-logs", "namespace": "monitoring"},
         "spec": {"minReadySeconds": 9}}
    specs = [{"kind": "daemonset", "name": "otel-collector-logs", "namespace": "monitoring", "manifest": m}]
    change = {"kind": "manifest"}
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", change, specs)
    payload = ah_plan.new_payload("otel-collector", CTX, "argo-cd", change, plan, 60, 60, time.time())
    ah_ops.apply_change(kube, payload, payload["targets"][0])
    assert ds(cluster)["spec"]["minReadySeconds"] == 9
    assert not [c for c in cluster.calls() if c[2] == "apply"]
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])
    assert ds(cluster)["spec"]["minReadySeconds"] == 5


def test_created_object_modified_by_others_is_not_deleted(kube, cluster):
    import time

    import ah_plan
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "new", "namespace": "monitoring"}, "data": {"a": "1"}}
    specs = [{"kind": "configmap", "name": "new", "namespace": "monitoring", "manifest": cm}]
    change = {"kind": "manifest"}
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", change, specs)
    payload = ah_plan.new_payload("otel-collector", CTX, "argo-cd", change, plan, 60, 60, time.time())
    ah_ops.apply_change(kube, payload, payload["targets"][0])
    kube.patch("configmap", "new", "monitoring", "merge", {"data": {"b": "2"}}, "someone-else")
    warnings, _ = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert any("not deleting" in w for w in warnings)
    assert cluster.get(CTX, "configmap", "monitoring", "new") is not None


def test_policy_changed_during_hold_is_not_clobbered(held, kube, cluster):
    payload = held()
    kube.patch(APP_KIND, "otel-collector", "argo-cd", "merge",
               {"spec": {"syncPolicy": {"automated": {"prune": False}}}}, "someone-else")
    warnings, _ = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert any("syncPolicy.automated" in w and "--force" in w for w in warnings)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": False}


def test_no_refresh_when_original_had_no_auto_sync(kube, cluster):
    import time

    import ah_plan
    cluster.seed(CTX, make_app("quiet", automated="off"))
    plan = ah_plan.make_plan(kube, "quiet", "argo-cd", PATCH_CHANGE, DS_SPEC)
    payload = ah_plan.new_payload("quiet", CTX, "argo-cd", PATCH_CHANGE, plan, 60, 60, time.time())
    ah_ops.apply_change(kube, payload, payload["targets"][0])
    ah_ops.release_all(kube, payload, False, lambda m: None)
    assert "argocd.argoproj.io/refresh" not in app(cluster, "quiet")["metadata"].get("annotations", {})
    assert get_at(app(cluster, "quiet"), ["spec", "syncPolicy", "automated"]) == ABSENT


def test_release_keeps_going_and_reports_errors_when_cluster_unreachable(held, kube, cluster):
    payload = held()
    # the target revert tries twice (retry), then the policy restore: three patch calls fail
    cluster.fail_next("patch", n=3)
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert len(errors) == 2
    # a retry with a healthy cluster succeeds
    assert ah_ops.release_all(kube, payload, False, lambda m: None) == ([], [])
    assert get_at(ds(cluster), AFF) == ABSENT


def test_revert_retries_once_when_a_test_guard_races(held, kube, cluster):
    payload = held()
    cluster.fail_next("patch", n=1)       # first revert patch fails, second succeeds
    t = payload["targets"][0]
    assert ah_ops.revert_target(kube, t, False, lambda m: None) == []
    assert get_at(ds(cluster), AFF) == ABSENT


def _created_cm_payload(kube):
    import time

    import ah_plan
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "new", "namespace": "monitoring"}, "data": {"a": "1"}}
    specs = [{"kind": "configmap", "name": "new", "namespace": "monitoring", "manifest": cm}]
    change = {"kind": "manifest"}
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", change, specs)
    payload = ah_plan.new_payload("otel-collector", CTX, "argo-cd", change, plan, 60, 60, time.time())
    ah_ops.disable_auto_sync(kube, payload)
    ah_ops.apply_change(kube, payload, payload["targets"][0])
    return payload


def test_managed_fields_entry_without_manager_does_not_crash_or_delete(kube, cluster):
    payload = _created_cm_payload(kube)
    cluster.mutate(CTX, "configmap", "monitoring", "new",
                   lambda o: o["metadata"].setdefault("managedFields", []).append({"operation": "Update"}))
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors == []
    assert any("not deleting" in w for w in warnings)
    assert cluster.get(CTX, "configmap", "monitoring", "new") is not None
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}


def test_unnamed_and_named_other_managers_do_not_crash_the_warning(kube, cluster):
    payload = _created_cm_payload(kube)

    def add(o):
        o["metadata"].setdefault("managedFields", []).extend(
            [{"operation": "Update"}, {"manager": "kubectl-edit", "operation": "Update"}])
    cluster.mutate(CTX, "configmap", "monitoring", "new", add)
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors == []
    assert any("not deleting" in w and "kubectl-edit" in w for w in warnings)
    assert cluster.get(CTX, "configmap", "monitoring", "new") is not None


def test_only_unnamed_manager_entries_are_not_treated_as_argo_hold(kube, cluster):
    payload = _created_cm_payload(kube)
    cluster.mutate(CTX, "configmap", "monitoring", "new",
                   lambda o: o["metadata"].update(managedFields=[{"operation": "Update"}]))
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors == []
    assert any("not deleting" in w for w in warnings)
    assert cluster.get(CTX, "configmap", "monitoring", "new") is not None


def test_non_kubectl_error_in_a_target_revert_still_restores_policy(held, kube, cluster, monkeypatch):
    payload = held()

    def boom(*a, **k):
        raise ValueError("s3cr3t")
    monkeypatch.setattr(ah_ops, "revert_target", boom)
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert len(errors) == 1
    assert "ValueError" in errors[0] and "daemonset/otel-collector-logs" in errors[0]
    assert not any("s3cr3t" in e for e in errors)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}


def test_non_json_kubectl_output_on_target_get_still_restores_policy(held, kube, cluster, monkeypatch):
    payload = held()
    real_get = kube.get

    def get(kind, name, ns):
        if kind == "daemonset":
            raise ValueError("s3cr3t")
        return real_get(kind, name, ns)
    monkeypatch.setattr(kube, "get", get)
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert errors and not any("s3cr3t" in e for e in errors)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}


def test_non_kubectl_error_in_restore_policy_is_recorded_not_raised(held, kube, monkeypatch):
    payload = held()

    def boom(*a, **k):
        raise RuntimeError("s3cr3t")
    monkeypatch.setattr(ah_ops, "restore_policy", boom)
    warnings, errors = ah_ops.release_all(kube, payload, False, lambda m: None)
    assert len(errors) == 1
    assert "RuntimeError" in errors[0] and payload["app"] in errors[0]
    assert "s3cr3t" not in errors[0]


def test_keyboard_interrupt_in_a_step_propagates(held, kube, monkeypatch):
    payload = held()

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(ah_ops, "revert_target", boom)
    with pytest.raises(KeyboardInterrupt):
        ah_ops.release_all(kube, payload, False, lambda m: None)
