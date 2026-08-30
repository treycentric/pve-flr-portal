from cryptography import x509
from cryptography.x509.oid import NameOID

from backend.tls import ensure_self_signed_cert


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
