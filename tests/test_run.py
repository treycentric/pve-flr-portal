"""Light coverage for run.py's data-plane wiring (issue #47). The actual
socket bind / uvicorn serve isn't exercised here - just the cert-prep
and the plaintext/HTTPS decision."""
import importlib
from dataclasses import replace

import pytest
from cryptography import x509

import run
from backend import config
from backend.restore_network_pull import DataNic


@pytest.fixture
def _dp_paths(tmp_path, monkeypatch):
    cert = tmp_path / "data-plane.crt"
    key = tmp_path / "data-plane.key"
    monkeypatch.setattr(
        run,
        "settings",
        replace(
            config.settings,
            restore_data_nic_tls_cert_file=str(cert),
            restore_data_nic_tls_key_file=str(key),
        ),
    )
    return cert, key


def test_prepare_data_plane_cert_collects_ip_and_hostname_sans(_dp_paths):
    cert, key = _dp_paths
    nics = [
        DataNic("10.0.5.0/24", "10.0.5.5", hostname="restore.dc1.lan"),
        DataNic("10.0.6.0/24", "10.0.6.5"),
    ]
    got_cert, got_key = run._prepare_data_plane_cert(nics)
    assert (got_cert, got_key) == (cert, key)
    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert {str(v) for v in san.get_values_for_type(x509.IPAddress)} == {"10.0.5.5", "10.0.6.5"}
    assert "restore.dc1.lan" in san.get_values_for_type(x509.DNSName)


def test_data_plane_tls_enabled_follows_preferred(monkeypatch):
    monkeypatch.setattr(run, "settings", replace(config.settings, restore_data_nic_tls_preferred="plaintext"))
    assert run._data_plane_tls_enabled() is False
    monkeypatch.setattr(run, "settings", replace(config.settings, restore_data_nic_tls_preferred="verify"))
    assert run._data_plane_tls_enabled() is True


def test_min_tls_map_covers_both_configured_values():
    importlib.reload(config)
    assert set(run._MIN_TLS) == {"1.2", "1.3"}
