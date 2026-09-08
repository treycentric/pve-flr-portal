"""HTTPS-by-default bootstrap (docs/plan.md §7.3): generates a self-signed
cert/key pair on first run so the app never has to serve plain HTTP once
real login credentials are involved.

Both `ensure_self_signed_cert` (main UI listener) and
`ensure_data_plane_cert` (Direct Network Transfer data plane, issue #47
§7.6.1) leave an admin-supplied, still-valid cert/key alone, but
**regenerate a broken one** (mismatched cert/key, unreadable, expired) -
a broken pair only ever crashes uvicorn's SSL context at startup, so
replacing it with a working self-signed cert is strictly better than
failing to boot. The data-plane cert additionally regenerates its own
(auto-generated) cert when the configured data-NIC IP/DNS SANs change.

Auto-generated certs carry a recognisable Organization name so this
code can tell "ours, safe to refresh" from "the admin's, hands off".
"""
import datetime
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID

_log = logging.getLogger("pve_flr_portal.tls")

_AUTOGEN_ORG = "pve-flr-portal (auto-generated)"


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
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _AUTOGEN_ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)

    san: list[x509.GeneralName] = [x509.DNSName(n) for n in dict.fromkeys((common_name, *dns_sans))]
    for ip in ip_sans:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))

    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
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


def _is_autogen(cert: x509.Certificate) -> bool:
    orgs = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return bool(orgs) and orgs[0].value == _AUTOGEN_ORG


def _spki(pubkey) -> bytes:
    return pubkey.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _pair_broken_reason(cert_path: Path, key_path: Path) -> str | None:
    """None if (cert, key) is a usable pair; else a short reason. A
    broken pair would make uvicorn's `create_ssl_context` raise at
    startup, so callers regenerate rather than propagate that."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:  # unreadable, wrong format, encrypted key, ...
        return f"unreadable ({exc.__class__.__name__})"
    if _spki(cert.public_key()) != _spki(key.public_key()):
        return "certificate and private key do not match"
    if cert.not_valid_after_utc < datetime.datetime.now(datetime.UTC):
        return "expired"
    return None


def ensure_self_signed_cert(cert_path: Path, key_path: Path, common_name: str = "localhost") -> None:
    if cert_path.exists() and key_path.exists():
        reason = _pair_broken_reason(cert_path, key_path)
        if reason is None:
            return
        _log.warning("regenerating self-signed cert %s: %s", cert_path, reason)
    _write_self_signed(cert_path, key_path, common_name, dns_sans=("localhost",))


def _san_covers(cert_path: Path, ip_sans: tuple[str, ...], dns_sans: tuple[str, ...]) -> bool:
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
    """Ensure a usable data-plane cert/key at these paths (issue #47).
    Generates a self-signed one (IP/DNS SANs for the configured data
    NICs) if absent. If a pair exists:
      - broken (mismatched, unreadable, expired) -> regenerate;
      - valid, auto-generated, but missing a configured SAN ->
        regenerate to add it;
      - valid, admin-supplied, missing a SAN -> keep, warn (their call).
    """
    cn = dns_sans[0] if dns_sans else (ip_sans[0] if ip_sans else "pve-flr-portal-data-plane")

    if cert_path.exists() and key_path.exists():
        reason = _pair_broken_reason(cert_path, key_path)
        if reason is not None:
            _log.warning("regenerating data-plane cert %s: %s", cert_path, reason)
        elif not _san_covers(cert_path, ip_sans, dns_sans):
            try:
                autogen = _is_autogen(x509.load_pem_x509_certificate(cert_path.read_bytes()))
            except Exception:
                autogen = False
            if autogen:
                _log.info(
                    "regenerating data-plane cert %s to cover IPs=%s DNS=%s",
                    cert_path,
                    list(ip_sans),
                    list(dns_sans),
                )
            else:
                _log.warning(
                    "data-plane cert %s does not cover every configured data NIC (want IPs=%s DNS=%s); "
                    "`verify` clients will reject it. Fix its SANs or delete the pair to regenerate.",
                    cert_path,
                    list(ip_sans),
                    list(dns_sans),
                )
                return
        else:
            return

    _write_self_signed(cert_path, key_path, cn, dns_sans=dns_sans, ip_sans=ip_sans, days=825, is_ca=True)
