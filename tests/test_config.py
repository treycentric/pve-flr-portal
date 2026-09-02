import importlib
from pathlib import Path

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


def test_float_parsing_and_default(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "0.5")
    assert config._float("SOME_FLOAT", default=1.0) == 0.5
    monkeypatch.delenv("SOME_FLOAT", raising=False)
    assert config._float("SOME_FLOAT", default=1.0) == 1.0


def test_theme_parsing_default_and_case(monkeypatch):
    monkeypatch.delenv("DEFAULT_THEME", raising=False)
    assert config._theme("DEFAULT_THEME", "auto") == "auto"
    monkeypatch.setenv("DEFAULT_THEME", "Dark")
    assert config._theme("DEFAULT_THEME", "auto") == "dark"
    monkeypatch.setenv("DEFAULT_THEME", "Proxmox-Dark")
    assert config._theme("DEFAULT_THEME", "auto") == "proxmox-dark"


def test_theme_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("DEFAULT_THEME", "solarized")
    with pytest.raises(RuntimeError, match="DEFAULT_THEME"):
        config._theme("DEFAULT_THEME", "auto")


def test_path_parsing_and_default(monkeypatch):
    monkeypatch.delenv("SOME_DIR", raising=False)
    assert config._path("SOME_DIR", "data") == Path("data")
    monkeypatch.setenv("SOME_DIR", "/srv/state")
    assert config._path("SOME_DIR", "data") == Path("/srv/state")


def test_data_dir_default_and_ensure_creates_it(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "state"
    monkeypatch.setenv("PFR_DATA_DIR", str(target))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.data_dir == target
        assert not target.exists()  # not created at import time
        assert reloaded.ensure_data_dir() == target
        assert target.is_dir()
        reloaded.ensure_data_dir()  # idempotent, no error
    finally:
        monkeypatch.delenv("PFR_DATA_DIR", raising=False)
        importlib.reload(config)


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
    monkeypatch.setenv("GUEST_AGENT_MIN_COMMAND_GAP_SECONDS", "0.25")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.pve_host == "example.org"
        assert reloaded.settings.pve_storage == "store1"
        assert reloaded.settings.pve_verify_ssl is True
        assert reloaded.settings.session_idle_timeout_minutes == 15
        assert reloaded.settings.port == 9000
        assert reloaded.settings.guest_agent_min_command_gap_seconds == 0.25
    finally:
        # Restore the module to the shared test defaults for later tests.
        monkeypatch.setenv("PVE_HOST", "pve.test.local")
        monkeypatch.setenv("PVE_STORAGE", "pbs")
        monkeypatch.setenv("PVE_VERIFY_SSL", "false")
        monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "30")
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("GUEST_AGENT_MIN_COMMAND_GAP_SECONDS", raising=False)
        importlib.reload(config)
