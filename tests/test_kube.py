import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from ah_kube import KubectlError, iter_json_docs
from fakes.fakecluster import CTX, make_daemonset

RECORDED = Path(__file__).parent / "fixtures" / "recorded"


def test_get_returns_object_and_none_for_notfound(kube):
    assert kube.get("daemonset", "otel-collector-logs", "monitoring")["kind"] == "DaemonSet"
    assert kube.get("daemonset", "nope", "monitoring") is None


def test_patch_dry_run_does_not_persist(kube, cluster):
    out = kube.patch("daemonset", "otel-collector-logs", "monitoring", "merge",
                     {"spec": {"minReadySeconds": 9}}, "argo-hold", dry_run=True)
    assert out["spec"]["minReadySeconds"] == 9
    assert cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")["spec"]["minReadySeconds"] == 5


def test_patch_persists_with_field_manager(kube, cluster):
    kube.patch("daemonset", "otel-collector-logs", "monitoring", "merge",
               {"spec": {"minReadySeconds": 9}}, "argo-hold")
    obj = cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")
    assert obj["spec"]["minReadySeconds"] == 9
    assert {m["manager"] for m in obj["metadata"]["managedFields"]} == {"argo-hold"}


def test_apply_ssa_creates_and_uses_server_side(kube, cluster):
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"a": "1"}}
    kube.apply_ssa(cm, "argo-hold")
    assert cluster.get(CTX, "configmap", "monitoring", "c")["data"] == {"a": "1"}
    argv = [c for c in cluster.calls() if c[2] == "apply"][0]
    assert "--server-side" in argv and "--field-manager" in argv


def test_kubectl_error_message_never_contains_the_patch(kube):
    with pytest.raises(KubectlError) as e:
        kube.patch("daemonset", "nope", "monitoring", "merge", {"spec": {"token": "s3cr3t"}}, "argo-hold")
    assert "s3cr3t" not in str(e.value)
    assert e.value.not_found


def test_annotate_and_delete(kube, cluster):
    kube.annotate("daemonset", "otel-collector-logs", "monitoring", "k", "v")
    obj = cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")
    assert obj["metadata"]["annotations"]["k"] == "v"
    kube.delete("daemonset", "otel-collector-logs", "monitoring")
    assert kube.get("daemonset", "otel-collector-logs", "monitoring") is None


def test_iter_json_docs_accepts_compact_ndjson():
    stream = io.StringIO('{"type":"ADDED","object":{"a":1}}\n{"type":"MODIFIED","object":{"a":2}}\n')
    assert [d["type"] for d in iter_json_docs(stream)] == ["ADDED", "MODIFIED"]


def test_iter_json_docs_splits_back_to_back_indented_documents():
    stream = io.StringIO('{\n  "type": "ADDED",\n  "object": {"a": 1}\n}\n{\n  "type": "MODIFIED",\n  "object": {"a": 2}\n}\n')
    assert [d["type"] for d in iter_json_docs(stream)] == ["ADDED", "MODIFIED"]


def test_recorded_watch_stream_parses():
    events = list(iter_json_docs(open(RECORDED / "watch_stream.txt")))
    assert events[0]["type"] == "ADDED"
    assert {"type", "object"} == set(events[0])
    assert any(e["type"] == "MODIFIED" for e in events)


def test_fake_watch_matches_recorded_event_shape(kube, cluster):
    proc = kube.watch("daemonset", "otel-collector-logs", "monitoring")
    try:
        events = iter_json_docs(proc.stdout)
        first = next(events)
        assert first["type"] == "ADDED" and set(first) == {"type", "object"}
        cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                       lambda o: o["spec"].update(minReadySeconds=6))
        assert next(events)["type"] == "MODIFIED"
    finally:
        proc.terminate()
        proc.wait()


def test_fake_notfound_matches_recorded_stderr(kube):
    recorded = (RECORDED / "notfound.stderr").read_text()
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "fakes" / "kubectl"),
                           "--context", CTX, "get", "daemonset", "nope", "-o", "json"],
                          capture_output=True, text=True)   # inherits FAKE_KUBECTL_STORE from `env`
    shape = re.compile(r'^Error from server \(NotFound\): \S+ "nope" not found$', re.MULTILINE)
    assert shape.search(recorded.strip())
    assert shape.search(proc.stderr.strip())
    assert proc.returncode == int((RECORDED / "notfound.exit").read_text())


def _patch_ok(kube, n):
    return kube.patch("daemonset", "otel-collector-logs", "monitoring", "merge",
                      {"spec": {"minReadySeconds": n}}, "argo-hold")


def test_fail_next_skip_then_n_counts_across_processes(kube, cluster):
    cluster.fail_next("patch", n=1, skip=1)
    assert _patch_ok(kube, 10)["spec"]["minReadySeconds"] == 10
    with pytest.raises(KubectlError):
        _patch_ok(kube, 11)
    assert _patch_ok(kube, 12)["spec"]["minReadySeconds"] == 12
    assert len([c for c in cluster.calls() if c[2] == "patch"]) == 3
    # the failed patch was not applied
    obj = cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")
    assert obj["spec"]["minReadySeconds"] == 12


def test_fail_next_is_per_verb(kube, cluster):
    cluster.fail_next("get", n=2)
    for _ in range(2):
        with pytest.raises(KubectlError):
            kube.get("daemonset", "otel-collector-logs", "monitoring")
        assert _patch_ok(kube, 20)["spec"]["minReadySeconds"] == 20   # patch unaffected
    assert kube.get("daemonset", "otel-collector-logs", "monitoring")["kind"] == "DaemonSet"


def test_convert_manifest_reads_json_through_create_dry_run(kube, tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(make_daemonset("n", "monitoring")))
    assert kube.convert_manifest(str(f))[0]["metadata"]["name"] == "n"
