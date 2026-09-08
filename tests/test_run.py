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


def test_prepare_data_plane_cert_covers_what_the_url_presents(_dp_paths):
    cert, _key = _dp_paths
    nics = [
        DataNic("10.0.5.0/24", "10.0.5.5", hostname="restore.dc1.lan"),  # URL uses the name
        DataNic("10.0.6.0/24", "10.0.6.5"),                              # URL uses the IP
    ]
    run._prepare_data_plane_cert(nics)
    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    # the hostname'd NIC contributes only its DNS name, not its IP
    assert {str(v) for v in san.get_values_for_type(x509.IPAddress)} == {"10.0.6.5"}
    assert "restore.dc1.lan" in san.get_values_for_type(x509.DNSName)


def test_data_plane_tls_enabled_follows_preferred(monkeypatch):
    monkeypatch.setattr(run, "settings", replace(config.settings, restore_data_nic_tls_preferred="plaintext"))
    assert run._data_plane_tls_enabled() is False
    monkeypatch.setattr(run, "settings", replace(config.settings, restore_data_nic_tls_preferred="verify"))
    assert run._data_plane_tls_enabled() is True


def test_min_tls_map_covers_both_configured_values():
    importlib.reload(config)
    assert set(run._MIN_TLS) == {"1.2", "1.3"}


def test_bind_error_none_for_loopback_reason_for_a_non_local_address():
    assert run._bind_error("127.0.0.1", 0) is None
    # TEST-NET-1 (RFC 5737) - never a local address on a real host.
    reason = run._bind_error("192.0.2.123", 0)
    assert reason is not None and "not a local address" in reason


def test_bind_error_reports_a_port_collision(tmp_path):
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        reason = run._bind_error("127.0.0.1", port)
        assert reason is not None and "already in use" in reason
    finally:
        s.close()
