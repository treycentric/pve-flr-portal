"""HTTPS-by-default bootstrap (docs/plan.md §7.3): generates a self-signed
cert/key pair on first run so the app never has to serve plain HTTP once
real login credentials are involved. Only generates when both files are
missing - an admin-supplied cert/key dropped at the same paths is used
as-is and is never overwritten, which is the whole "admin-replaceable"
story.

`ensure_data_plane_cert` does the same for the Direct Network Transfer
data-plane listener (issue #47, docs/plan.md §7.6.1), but with IP (and
optionally DNS) Subject Alternative Names for the configured data NICs,
since guests fetch from an IP literal - a CN-only cert fails verification
on every current TLS client.
"""
import datetime
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_log = logging.getLogger("pve_flr_portal.tls")


def _write_self_signed(
    cert_path: Path,
    key_path: Path,
    common_name: str,
    *,
    dns_sans: tuple[str, ...] = (),
    ip_sans: tuple[str, ...] = (),
    days: int = 825,
    is_ca: bool = False,
) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)

    san: list[x509.GeneralName] = [x509.DNSName(name) for name in dict.fromkeys((common_name, *dns_sans))]
    for ip in ip_sans:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
    )
    if is_ca:
        # Self-signed cert used as its own trust anchor (the data-plane
        # cert, issue #47): a strict validator - and this app's own
        # guest_ca.is_ca_cert() guard - wants BasicConstraints cA=True.
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    cert = builder.sign(key, hashes.SHA256())

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_self_signed_cert(cert_path: Path, key_path: Path, common_name: str = "localhost") -> None:
    if cert_path.exists() and key_path.exists():
        return
    _write_self_signed(cert_path, key_path, common_name, dns_sans=("localhost",))


def _san_covers(cert_path: Path, ip_sans: tuple[str, ...], dns_sans: tuple[str, ...]) -> bool:
    """True if the cert at `cert_path` already lists every wanted IP/DNS
    SAN. Best-effort: an unreadable cert returns False."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:
        return False
    have_ips = {str(v) for v in san.get_values_for_type(x509.IPAddress)}
    have_dns = set(san.get_values_for_type(x509.DNSName))
    return set(ip_sans) <= have_ips and set(dns_sans) <= have_dns


def ensure_data_plane_cert(
    cert_path: Path,
    key_path: Path,
    ip_sans: tuple[str, ...],
    dns_sans: tuple[str, ...] = (),
) -> None:
    """Generate a self-signed data-plane cert (issue #47) with the given
    IP/DNS SANs if cert+key are both absent. Never overwrites an existing
    pair - if one is present but doesn't cover every configured data-NIC
    address, log a clear warning rather than clobbering what might be an
    admin-supplied cert; the admin fixes it (regenerate their own, or
    delete the auto-generated pair to force a fresh one)."""
    cn = dns_sans[0] if dns_sans else (ip_sans[0] if ip_sans else "pve-flr-portal-data-plane")
    if cert_path.exists() and key_path.exists():
        if not _san_covers(cert_path, ip_sans, dns_sans):
            _log.warning(
                "data-plane cert %s does not cover every configured data NIC "
                "(want IPs=%s DNS=%s); `verify` clients will reject it. Delete the "
                "cert/key pair to regenerate, or supply your own.",
                cert_path,
                list(ip_sans),
                list(dns_sans),
            )
        return
    _write_self_signed(cert_path, key_path, cn, dns_sans=dns_sans, ip_sans=ip_sans, days=825, is_ca=True)
