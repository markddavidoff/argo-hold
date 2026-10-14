import ah_plan
import pytest
from ah_paths import is_absent
from conftest import AFFINITY, DS_SPEC, PATCH_CHANGE
from fakes.fakecluster import CTX, make_app


def test_make_plan_snapshots_policy_and_computes_paths(kube):
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    assert plan["automated_was_on"] is True
    assert plan["original_automated"] == {"prune": True, "selfHeal": True}
    (t,) = plan["targets"]
    assert t["existed"] is True
    assert [r["path"] for r in t["paths"]] == [["spec", "template", "spec", "affinity"]]
    assert is_absent(t["paths"][0]["before"])


def test_make_plan_does_not_mutate_the_cluster(kube, cluster):
    before = cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")
    ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    assert cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs") == before


def test_make_plan_rejects_noop_patch(kube, cluster):
    kube.patch("daemonset", "otel-collector-logs", "monitoring", "strategic", AFFINITY, "x")
    with pytest.raises(ah_plan.HoldError, match="changes nothing"):
        ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)


def test_make_plan_missing_application_or_target(kube):
    with pytest.raises(ah_plan.HoldError, match="Application"):
        ah_plan.make_plan(kube, "nope", "argo-cd", PATCH_CHANGE, DS_SPEC)
    missing = [dict(DS_SPEC[0], name="nope")]
    with pytest.raises(ah_plan.HoldError, match="not found"):
        ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, missing)


def test_make_plan_when_auto_sync_already_off(kube, cluster):
    cluster.seed(CTX, make_app("quiet", automated="off"))
    plan = ah_plan.make_plan(kube, "quiet", "argo-cd", PATCH_CHANGE, DS_SPEC)
    assert plan["automated_was_on"] is False
    assert is_absent(plan["original_automated"])


def test_make_plan_manifest_creating_object(kube):
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "new", "namespace": "monitoring"}, "data": {"a": "1"}}
    specs = [{"kind": "configmap", "name": "new", "namespace": "monitoring", "manifest": cm}]
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", {"kind": "manifest"}, specs)
    assert plan["targets"][0]["existed"] is False
    assert plan["targets"][0]["paths"] == []


def test_make_plan_manifest_on_existing_object_uses_merge_patch(kube, cluster):
    m = {"apiVersion": "apps/v1", "kind": "DaemonSet",
         "metadata": {"name": "otel-collector-logs", "namespace": "monitoring"},
         "spec": {"minReadySeconds": 9}}
    specs = [{"kind": "daemonset", "name": "otel-collector-logs", "namespace": "monitoring", "manifest": m}]
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", {"kind": "manifest"}, specs)
    assert plan["targets"][0]["existed"] is True
    assert [r["path"] for r in plan["targets"][0]["paths"]] == [["spec", "minReadySeconds"]]
    assert not [c for c in cluster.calls() if c[2] == "apply"]      # no server-side apply on existing objects


def test_plan_signature_is_stable_and_sensitive(kube):
    a = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    b = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    assert ah_plan.plan_signature(a) == ah_plan.plan_signature(b)
    other = {"kind": "patch", "type": "merge", "doc": {"spec": {"minReadySeconds": 9}}}
    c = ah_plan.make_plan(kube, "otel-collector", "argo-cd", other, DS_SPEC)
    assert ah_plan.plan_signature(a) != ah_plan.plan_signature(c)


def test_new_payload_shape(kube):
    plan = ah_plan.make_plan(kube, "otel-collector", "argo-cd", PATCH_CHANGE, DS_SPEC)
    p = ah_plan.new_payload("otel-collector", CTX, "argo-cd", PATCH_CHANGE, plan, 7200, 60, 1000.0)
    assert p["expires_at"] == 8200.0 and p["heartbeat_at"] == 1000.0
    assert p["phase"] == "starting" and p["reapply_count"] == 0 and p["reenable_count"] == 0
    assert p["context"] == CTX and p["interval"] == 60 and p["ttl"] == 7200
