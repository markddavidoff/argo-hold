import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                    # `fakes` package
sys.path.insert(0, str(HERE.parent / "scripts"))  # ah_* modules


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ARGO_HOLD_K3D") == "1":
        return
    skip = pytest.mark.skip(reason="set ARGO_HOLD_K3D=1 (see make test-k3d)")
    for item in items:
        if "k3d" in item.keywords:
            item.add_marker(skip)


import time  # noqa: E402

from fakes.fakecluster import CTX, FakeCluster, make_app, make_daemonset  # noqa: E402

FAKE_KUBECTL = HERE / "fakes" / "kubectl"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KUBECTL_STORE", str(tmp_path / "cluster.json"))
    monkeypatch.setenv("ARGO_HOLD_KUBECTL", str(FAKE_KUBECTL))
    monkeypatch.setenv("ARGO_HOLD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ARGO_HOLD_TICK", "0.05")
    return tmp_path


@pytest.fixture
def cluster(env):
    c = FakeCluster(os.environ["FAKE_KUBECTL_STORE"])
    c.seed(CTX, make_app())
    c.seed(CTX, make_daemonset())
    return c


@pytest.fixture
def kube(cluster):
    from ah_kube import Kube
    return Kube(CTX)


def wait_for(fn, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


AFFINITY = {"spec": {"template": {"spec": {"affinity": {"nodeAffinity": {
    "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "gke-nodepool", "operator": "NotIn",
                               "values": ["infra-pool"]}]}]}}}}}}}
DS_SPEC = [{"kind": "daemonset", "name": "otel-collector-logs", "namespace": "monitoring",
            "manifest": None}]
PATCH_CHANGE = {"kind": "patch", "type": "strategic", "doc": AFFINITY}


@pytest.fixture
def held(kube):
    """Build a payload and apply the change exactly as `begin` will (Task 6)."""
    import ah_ops
    import ah_plan

    def _held(change=PATCH_CHANGE, specs=DS_SPEC, app="otel-collector", ttl=7200, interval=60):
        plan = ah_plan.make_plan(kube, app, "argo-cd", change, specs)
        payload = ah_plan.new_payload(app, CTX, "argo-cd", change, plan, ttl, interval, time.time())
        if payload["automated_was_on"]:
            ah_ops.disable_auto_sync(kube, payload)
        for t in payload["targets"]:
            ah_ops.apply_change(kube, payload, t)
        return payload

    return _held
