import os
import socket
import stat
import time

import ah_state as st
import pytest


def payload(**kw):
    base = {"app": "x", "pid": os.getpid(), "hostname": socket.gethostname(),
            "heartbeat_at": time.time(), "interval": 60, "expires_at": time.time() + 3600,
            "context": "c", "phase": "holding"}
    base.update(kw)
    return base


def test_create_is_exclusive_one_hold_per_app(env):
    st.create("x", payload())
    with pytest.raises(st.HoldExists):
        st.create("x", payload())
    st.create("y", payload(app="y"))      # a different app is fine


def test_state_files_are_private(env):
    st.create("x", payload())
    assert stat.S_IMODE(os.stat(st.state_path("x")).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(st.state_dir()).st_mode) == 0o700


def test_save_replaces_and_load_roundtrips(env):
    st.create("x", payload())
    st.save("x", payload(phase="releasing"))
    assert st.load("x")["phase"] == "releasing"
    assert st.load("nope") is None


def test_list_holds_ignores_temp_files(env):
    st.create("x", payload())
    st.save("x", payload())
    assert [p["app"] for p in st.list_holds()] == ["x"]


def test_verdicts(env):
    now = time.time()
    assert st.verdict(payload(), now) == "live"
    assert st.verdict(payload(heartbeat_at=now - 200), now) == "stale"      # > 2.5 x 60
    assert st.verdict(payload(pid=2 ** 22 + 12345), now) == "dead-pid"


def test_ttl_remaining(env):
    now = time.time()
    assert st.ttl_remaining(payload(expires_at=now + 100), now) == pytest.approx(100)
    assert st.ttl_remaining(payload(expires_at=now - 5), now) == pytest.approx(-5)


def test_holder_alive_checks_host_and_pid(env):
    assert st.holder_alive(payload())
    assert not st.holder_alive(payload(hostname="elsewhere"))
    assert not st.holder_alive(payload(pid=2 ** 22 + 12345))


def test_append_log_writes_timestamped_private_lines(env):
    st.append_log("x", "hello")
    text = st.log_path("x").read_text()
    assert text.endswith("hello\n") and text[:4].isdigit()
    assert stat.S_IMODE(os.stat(st.log_path("x")).st_mode) == 0o600
