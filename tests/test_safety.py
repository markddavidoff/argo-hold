import pytest
from ah_safety import (
    DEFAULT_DENYLIST,
    SafetyError,
    check_app_allowed,
    check_target_allowed,
    confirm,
    denylist,
    parse_target,
    validate_app_name,
)


@pytest.mark.parametrize("app", sorted(DEFAULT_DENYLIST))
def test_denylisted_apps_are_refused_without_allow_sensitive(app):
    with pytest.raises(SafetyError, match="--allow-sensitive"):
        check_app_allowed(app, allow_sensitive=False)
    check_app_allowed(app, allow_sensitive=True)  # explicit override passes


def test_default_denylist_is_exactly_the_brief():
    assert DEFAULT_DENYLIST == {"argo-cd", "app-root", "external-secrets"}


@pytest.mark.parametrize("value", ["secret-store,vault-sync", "secret-store vault-sync", " secret-store ,, vault-sync "])
def test_extra_denylist_comes_from_the_environment(monkeypatch, value):
    monkeypatch.setenv("ARGO_HOLD_DENYLIST_EXTRA", value)
    assert denylist() == DEFAULT_DENYLIST | {"secret-store", "vault-sync"}
    with pytest.raises(SafetyError, match="--allow-sensitive"):
        check_app_allowed("secret-store", allow_sensitive=False)
    check_app_allowed("secret-store", allow_sensitive=True)


def test_extra_denylist_unset_or_blank_adds_nothing(monkeypatch):
    monkeypatch.delenv("ARGO_HOLD_DENYLIST_EXTRA", raising=False)
    assert denylist() == DEFAULT_DENYLIST
    monkeypatch.setenv("ARGO_HOLD_DENYLIST_EXTRA", "  ")
    assert denylist() == DEFAULT_DENYLIST


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "A", ".hidden", "x y", "x\n"])
def test_validate_app_name_rejects_traversal(bad):
    with pytest.raises(SafetyError):
        validate_app_name(bad)


def test_validate_app_name_accepts_dns_names():
    validate_app_name("otel-collector")
    validate_app_name("a.b-c1")


def test_secret_targets_are_refused():
    for kind in ("secret", "secrets", "Secret"):
        with pytest.raises(SafetyError, match="Secret"):
            check_target_allowed(kind)
    check_target_allowed("daemonset")


def test_parse_target():
    assert parse_target("DaemonSet/otel-collector-logs") == ("daemonset", "otel-collector-logs")
    for bad in ("daemonset", "a/b/c", "/x", "x/"):
        with pytest.raises(SafetyError):
            parse_target(bad)


def test_confirm_requires_yes_unless_assumed():
    out = []
    assert confirm(["line"], assume_yes=True, input_fn=lambda _: "n", out=out.append) is True
    assert confirm(["line"], False, input_fn=lambda _: "y", out=out.append) is True
    assert confirm(["line"], False, input_fn=lambda _: "", out=out.append) is False

    def eof(_):
        raise EOFError

    assert confirm(["line"], False, input_fn=eof, out=out.append) is False
    assert "line" in out
