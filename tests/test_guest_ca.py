from dataclasses import replace

from backend import config, guest_ca
from backend.tls import _write_self_signed


def _ca_pem(tmp_path):
    _write_self_signed(tmp_path / "ca.crt", tmp_path / "ca.key", "data-plane", ip_sans=("10.0.5.5",), is_ca=True)
    return (tmp_path / "ca.crt").read_text()


def _leaf_pem(tmp_path):
    _write_self_signed(tmp_path / "leaf.crt", tmp_path / "leaf.key", "leaf", ip_sans=("10.0.5.5",), is_ca=False)
    return (tmp_path / "leaf.crt").read_text()


def test_is_ca_cert_true_for_ca_false_for_leaf(tmp_path):
    assert guest_ca.is_ca_cert(_ca_pem(tmp_path)) is True
    assert guest_ca.is_ca_cert(_leaf_pem(tmp_path)) is False


def test_fingerprint_sha1_hex_is_lowercase_hex(tmp_path):
    fp = guest_ca.fingerprint_sha1_hex(_ca_pem(tmp_path))
    assert len(fp) == 40 and fp == fp.lower()
    int(fp, 16)  # valid hex


def test_load_ca_pem_prefers_ca_file_then_falls_back_to_cert_file(tmp_path, monkeypatch):
    (tmp_path / "cert.pem").write_text("CERT")
    (tmp_path / "ca.pem").write_text("CA")
    monkeypatch.setattr(
        guest_ca,
        "settings",
        replace(
            config.settings,
            restore_data_nic_tls_cert_file=str(tmp_path / "cert.pem"),
            restore_data_nic_tls_ca_file=str(tmp_path / "ca.pem"),
        ),
    )
    assert guest_ca.load_ca_pem() == "CA"
    monkeypatch.setattr(
        guest_ca,
        "settings",
        replace(
            config.settings,
            restore_data_nic_tls_cert_file=str(tmp_path / "cert.pem"),
            restore_data_nic_tls_ca_file="",
        ),
    )
    assert guest_ca.load_ca_pem() == "CERT"


def test_windows_argv_builders():
    assert guest_ca.windows_check_argv("abc123") == ["certutil", "-store", "Root", "abc123"]
    p = guest_ca.windows_scratch_path("deadbeef")
    assert p == "C:\\Windows\\Temp\\pve-flr-portal-ca-deadbeef.crt"
    assert guest_ca.windows_install_argv(p) == ["certutil", "-addstore", "-f", "Root", p]
    assert "Remove-Item" in guest_ca.windows_cleanup_argv(p)[-1]


def test_linux_anchor_and_update_by_distro_family():
    assert guest_ca.linux_anchor_path(has_update_ca_certificates=True).startswith("/usr/local/share/ca-certificates/")
    assert guest_ca.linux_anchor_path(has_update_ca_certificates=False).startswith("/etc/pki/ca-trust/source/anchors/")
    assert guest_ca.linux_update_argv(has_update_ca_certificates=True) == ["update-ca-certificates"]
    assert guest_ca.linux_update_argv(has_update_ca_certificates=False) == ["update-ca-trust", "extract"]
