import json
import signal
import subprocess
import threading
from pathlib import Path

import ah_state
from ah_paths import ABSENT, get_at
from conftest import AFFINITY, wait_for
from fakes.fakecluster import CTX, make_app

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "argo-hold"
AFF = ["spec", "template", "spec", "affinity"]
PATCH = json.dumps(AFFINITY)
START = ["start", "--app", "otel-collector", "--context", CTX, "--target", "daemonset/otel-collector-logs",
         "-n", "monitoring", "--patch", PATCH, "--type", "strategic", "--yes", "--interval", "1"]


def run(*args, **kw):
    return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, **kw)


def spawn(*args):
    p = subprocess.Popen([str(LAUNCHER), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=p.wait, daemon=True).start()     # reap, so the pid does not linger as a zombie
    return p


def ds(cluster):
    return cluster.get(CTX, "daemonset", "monitoring", "otel-collector-logs")


def app(cluster):
    return cluster.get(CTX, "applications.argoproj.io", "argo-cd", "otel-collector")


def holding(app_name="otel-collector"):
    p = ah_state.load(app_name)
    return bool(p and p.get("phase") == "holding")


def test_parse_duration():
    import argo_hold
    assert argo_hold.parse_duration("2h") == 7200
    assert argo_hold.parse_duration("90") == 90
    assert argo_hold.parse_duration("1.5m") == 90
    assert argo_hold.parse_duration("30s") == 30


def test_dry_run_prints_plan_and_changes_nothing(env, cluster):
    r = run(*START, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "add" in r.stdout and "/spec/template/spec/affinity" in r.stdout
    assert "dry-run" in r.stdout
    assert "infra-pool" not in r.stdout                 # values are never printed
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) != ABSENT
    assert ah_state.load("otel-collector") is None


def test_dry_run_names_the_default_oncall_channel(env, cluster, monkeypatch):
    monkeypatch.delenv("ARGO_HOLD_ONCALL_CHANNEL", raising=False)
    r = run(*START, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "on-call:   #on-call" in r.stdout


def test_oncall_channel_is_configurable(env, cluster, monkeypatch):
    monkeypatch.setenv("ARGO_HOLD_ONCALL_CHANNEL", "#ops-pager")
    r = run(*START, "--dry-run")
    assert "on-call:   #ops-pager" in r.stdout
    assert "#on-call" not in r.stdout


def test_blank_oncall_channel_falls_back_to_the_default(env, cluster, monkeypatch):
    monkeypatch.setenv("ARGO_HOLD_ONCALL_CHANNEL", "  ")
    assert "on-call:   #on-call" in run(*START, "--dry-run").stdout


def test_confirmation_echo_shows_the_oncall_channel(env, cluster, monkeypatch):
    monkeypatch.setenv("ARGO_HOLD_ONCALL_CHANNEL", "#ops-pager")
    args = [a for a in START if a != "--yes"]
    r = run(*args, input="n\n")
    assert "on-call:   #ops-pager" in r.stdout and "aborted" in r.stdout


def test_missing_patch_file_is_a_clean_error(env, cluster):
    args = [a for a in START if a not in ("--patch", PATCH)]
    r = run(*args, "--patch-file", "/nonexistent/patch.json", "--dry-run")
    assert r.returncode == 1
    assert r.stderr.startswith("argo-hold:") and "/nonexistent/patch.json" in r.stderr
    assert "Traceback" not in r.stderr


def test_missing_kubectl_binary_is_a_clean_error(env, cluster, monkeypatch):
    monkeypatch.setenv("ARGO_HOLD_KUBECTL", "/nonexistent/kubectl")
    r = run(*START, "--dry-run")
    assert r.returncode == 1
    assert r.stderr.startswith("argo-hold:") and "Traceback" not in r.stderr


def test_start_help_says_an_existing_object_is_merge_patched():
    r = run("start", "--help")
    assert r.returncode == 0
    help_text = " ".join(r.stdout.split())              # argparse wraps lines
    assert "merge-patched" in help_text and "server-side apply" in help_text


def test_denylisted_app_is_refused_and_allow_sensitive_overrides(env, cluster):
    cluster.seed(CTX, make_app("app-root"))
    args = [a if a != "otel-collector" else "app-root" for a in START]
    r = run(*args, "--dry-run")
    assert r.returncode == 1 and "denylist" in r.stderr
    assert run(*args, "--dry-run", "--allow-sensitive").returncode == 0


def test_secret_target_is_refused(env, cluster):
    args = [a if a != "daemonset/otel-collector-logs" else "secret/x" for a in START]
    r = run(*args, "--dry-run")
    assert r.returncode == 1 and "Secret" in r.stderr


def test_context_is_required(env, cluster):
    args = [a for a in START if a not in ("--context", CTX)]
    assert run(*args).returncode == 2


def test_answering_no_aborts_without_changes(env, cluster):
    args = [a for a in START if a != "--yes"]
    r = run(*args, input="n\n")
    assert r.returncode == 1 and "aborted" in r.stdout
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) != ABSENT


def test_foreground_hold_lifecycle_sigterm_releases(env, cluster):
    p = spawn(*START)
    try:
        assert wait_for(holding, 10)
        assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT
        assert get_at(ds(cluster), AFF) != ABSENT
        # a second hold on the same app is refused while this one runs
        second = run(*START)
        assert second.returncode == 1 and "already" in second.stderr
        # drift is repaired without waiting for any interval
        cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                       lambda o: o["spec"]["template"]["spec"].pop("affinity"))
        assert wait_for(lambda: get_at(ds(cluster), AFF) != ABSENT, 5)
        s = run("status", "--app", "otel-collector")
        assert s.returncode == 0 and "live" in s.stdout
        lst = run("list")
        assert "otel-collector" in lst.stdout
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0
    assert get_at(ds(cluster), AFF) == ABSENT
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}
    assert ah_state.load("otel-collector") is None


def test_ttl_expiry_auto_releases(env, cluster):
    p = spawn(*START, "--ttl", "2s")
    assert p.wait(timeout=30) == 0
    assert get_at(ds(cluster), AFF) == ABSENT
    assert ah_state.load("otel-collector") is None


def test_release_works_from_state_alone_after_holder_is_killed(env, cluster):
    p = spawn(*START)
    assert wait_for(holding, 10)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=10)
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == ABSENT   # left off
    st = run("status", "--app", "otel-collector")
    assert st.returncode == 3 and "dead-pid" in st.stdout and "release" in st.stdout
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0, r.stdout + r.stderr
    assert get_at(ds(cluster), AFF) == ABSENT
    assert get_at(app(cluster), ["spec", "syncPolicy", "automated"]) == {"prune": True, "selfHeal": True}
    assert ah_state.load("otel-collector") is None


def test_release_signals_a_live_holder(env, cluster):
    p = spawn(*START)
    assert wait_for(holding, 10)
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0, r.stdout + r.stderr
    assert p.wait(timeout=20) == 0
    assert get_at(ds(cluster), AFF) == ABSENT


def test_release_conflict_exits_3_keeps_state_and_force_finishes(env, cluster):
    p = spawn(*START)
    assert wait_for(holding, 10)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=10)
    cluster.mutate(CTX, "daemonset", "monitoring", "otel-collector-logs",
                   lambda o: o["spec"]["template"]["spec"].update(affinity={"theirs": True}))
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 3 and "--force" in r.stdout
    assert get_at(ds(cluster), AFF) == {"theirs": True}
    assert ah_state.load("otel-collector") is not None
    r2 = run("release", "--app", "otel-collector", "--force")
    assert r2.returncode == 0
    assert get_at(ds(cluster), AFF) == ABSENT and ah_state.load("otel-collector") is None


def test_release_keeps_state_when_cluster_unreachable_then_retry_works(env, cluster):
    p = spawn(*START)
    assert wait_for(holding, 10)
    p.send_signal(signal.SIGKILL)
    p.wait(timeout=10)
    cluster.fail_next("patch", n=2)
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 1 and ah_state.load("otel-collector") is not None
    assert run("release", "--app", "otel-collector").returncode == 0
    assert ah_state.load("otel-collector") is None


def test_release_with_no_hold_is_a_noop(env, cluster):
    r = run("release", "--app", "otel-collector")
    assert r.returncode == 0 and "nothing to release" in r.stdout


def test_status_without_hold_and_list_empty(env, cluster):
    assert run("status", "--app", "otel-collector").returncode == 1
    assert "no active holds" in run("list").stdout


def test_detach_returns_while_holder_keeps_running(env, cluster):
    r = run(*START, "--detach")
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert holding() and get_at(ds(cluster), AFF) != ABSENT
    finally:
        run("release", "--app", "otel-collector")
    assert get_at(ds(cluster), AFF) == ABSENT


def test_manifest_mode_creates_then_deletes(env, cluster, tmp_path):
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "new", "namespace": "monitoring"}, "data": {"a": "1"}}
    f = tmp_path / "cm.json"
    f.write_text(json.dumps(cm))
    p = spawn("start", "--app", "otel-collector", "--context", CTX, "-f", str(f), "--yes", "--interval", "1")
    try:
        assert wait_for(holding, 10)
        assert cluster.get(CTX, "configmap", "monitoring", "new") is not None
    finally:
        p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0
    assert cluster.get(CTX, "configmap", "monitoring", "new") is None


def test_manifest_mode_rejects_target_flag(env, cluster, tmp_path):
    f = tmp_path / "cm.json"
    f.write_text("{}")
    r = run("start", "--app", "otel-collector", "--context", CTX, "-f", str(f),
            "--target", "daemonset/x", "-n", "monitoring", "--yes")
    assert r.returncode == 1 and "derives its targets" in r.stderr
