import ipaddress

from cryptography import x509
from cryptography.x509.oid import NameOID

from backend.tls import ensure_data_plane_cert, ensure_self_signed_cert


def test_generates_cert_and_key(tmp_path):
    cert = tmp_path / "certs" / "portal.crt"
    key = tmp_path / "certs" / "portal.key"
    ensure_self_signed_cert(cert, key, common_name="pve.example")
    assert cert.exists() and key.exists()

    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    cn = parsed.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "pve.example"
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert "pve.example" in san.value.get_values_for_type(x509.DNSName)
    assert "localhost" in san.value.get_values_for_type(x509.DNSName)


def test_does_not_overwrite_existing(tmp_path):
    cert = tmp_path / "portal.crt"
    key = tmp_path / "portal.key"
    cert.write_bytes(b"existing-cert")
    key.write_bytes(b"existing-key")
    ensure_self_signed_cert(cert, key, common_name="whatever")
    assert cert.read_bytes() == b"existing-cert"
    assert key.read_bytes() == b"existing-key"


# --- data-plane cert (issue #47) ---------------------------------------

def test_data_plane_cert_carries_ip_and_dns_sans(tmp_path):
    cert = tmp_path / "certs" / "data-plane.crt"
    key = tmp_path / "certs" / "data-plane.key"
    ensure_data_plane_cert(cert, key, ip_sans=("10.0.5.5", "10.0.6.5"), dns_sans=("restore.dc1.lan",))
    assert cert.exists() and key.exists()

    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    ips = set(san.get_values_for_type(x509.IPAddress))
    assert ips == {ipaddress.ip_address("10.0.5.5"), ipaddress.ip_address("10.0.6.5")}
    assert "restore.dc1.lan" in san.get_values_for_type(x509.DNSName)


def test_data_plane_cert_not_overwritten_but_warns_on_insufficient_sans(tmp_path, caplog):
    cert = tmp_path / "data-plane.crt"
    key = tmp_path / "data-plane.key"
    ensure_data_plane_cert(cert, key, ip_sans=("10.0.5.5",))
    original = cert.read_bytes()

    # A newly-added NIC isn't covered -> warn, never rewrite.
    with caplog.at_level("WARNING"):
        ensure_data_plane_cert(cert, key, ip_sans=("10.0.5.5", "10.0.9.9"))
    assert cert.read_bytes() == original
    assert any("does not cover" in r.message for r in caplog.records)


def test_data_plane_cert_regenerates_when_absent_after_config_change(tmp_path):
    cert = tmp_path / "data-plane.crt"
    key = tmp_path / "data-plane.key"
    ensure_data_plane_cert(cert, key, ip_sans=("10.0.5.5",))
    cert.unlink()
    key.unlink()
    ensure_data_plane_cert(cert, key, ip_sans=("10.0.5.5", "10.0.9.9"))
    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert ipaddress.ip_address("10.0.9.9") in set(san.get_values_for_type(x509.IPAddress))
