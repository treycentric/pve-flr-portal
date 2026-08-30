import importlib

import pytest

from backend import config


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_bool_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert config._bool("SOME_FLAG", default=True) is expected


def test_bool_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert config._bool("SOME_FLAG", default=True) is True
    assert config._bool("SOME_FLAG", default=False) is False


def test_int_parsing_and_default(monkeypatch):
    monkeypatch.setenv("SOME_INT", "42")
    assert config._int("SOME_INT", default=7) == 42
    monkeypatch.delenv("SOME_INT", raising=False)
    assert config._int("SOME_INT", default=7) == 7


def test_get_required_missing_raises(monkeypatch):
    monkeypatch.delenv("NEEDED", raising=False)
    with pytest.raises(RuntimeError, match="NEEDED"):
        config._get("NEEDED", required=True)


def test_settings_reads_environment(monkeypatch):
    monkeypatch.setenv("PVE_HOST", "example.org")
    monkeypatch.setenv("PVE_STORAGE", "store1")
    monkeypatch.setenv("PVE_VERIFY_SSL", "true")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "15")
    monkeypatch.setenv("PORT", "9000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.pve_host == "example.org"
        assert reloaded.settings.pve_storage == "store1"
        assert reloaded.settings.pve_verify_ssl is True
        assert reloaded.settings.session_idle_timeout_minutes == 15
        assert reloaded.settings.port == 9000
    finally:
        # Restore the module to the shared test defaults for later tests.
        monkeypatch.setenv("PVE_HOST", "pve.test.local")
        monkeypatch.setenv("PVE_STORAGE", "pbs")
        monkeypatch.setenv("PVE_VERIFY_SSL", "false")
        monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "30")
        monkeypatch.delenv("PORT", raising=False)
        importlib.reload(config)
