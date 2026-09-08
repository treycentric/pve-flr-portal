"""Installing the Direct Network Transfer data-plane CA into a guest's
trust store (issue #47, docs/plan.md §7.6.1), so `verify` mode works
without the admin pre-provisioning the cert on every guest.

Pure helpers only - the orchestration (probe the guest, `agent/file-write`
the PEM, run the install/update command, best-effort cleanup) lives in
`restore_runner._ensure_guest_trusts_ca`, the same split
`restore_network_pull` uses for the fetch itself. Needs the same
`VM.GuestAgent.Unrestricted` grant DNT already requires (guest-exec).

Assumes the configured CA file holds a single trust-anchor cert (the
self-signed default, or an admin's root/issuing CA - not a full leaf
chain). Multi-cert bundles: install the whole file, fingerprint the
first cert.
"""
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID

from .config import settings

# Written by agent/file-write, then handed to the install command.
_WINDOWS_SCRATCH_DIR = "C:\\Windows\\Temp"
# Debian/Ubuntu vs RHEL/Fedora/SUSE trust-anchor conventions.
_DEB_ANCHOR = "/usr/local/share/ca-certificates/pve-flr-portal-data-plane.crt"
_RHT_ANCHOR = "/etc/pki/ca-trust/source/anchors/pve-flr-portal-data-plane.crt"


def load_ca_pem() -> str:
    """The PEM to install in the guest - RESTORE_DATA_NIC_TLS_CA_FILE if
    set, else the data-plane cert file itself (the self-signed default,
    which is its own trust anchor)."""
    path = Path(settings.restore_data_nic_tls_ca_file or settings.restore_data_nic_tls_cert_file)
    return path.read_text()


def fingerprint_sha1_hex(pem: str | bytes) -> str:
    """Lowercase, no-spaces SHA-1 fingerprint of the first cert in `pem` -
    the identifier `certutil -store Root <thumb>` takes."""
    pem_b = pem.encode() if isinstance(pem, str) else pem
    cert = x509.load_pem_x509_certificate(pem_b)
    return cert.fingerprint(hashes.SHA1()).hex()


def is_ca_cert(pem: str | bytes) -> bool:
    """True if the first cert in `pem` is a CA (BasicConstraints cA=True).
    A leaf cert can't be a trust anchor - installing one wouldn't make
    `verify` pass. Best-effort: a cert with no BasicConstraints returns
    False."""
    pem_b = pem.encode() if isinstance(pem, str) else pem
    cert = x509.load_pem_x509_certificate(pem_b)
    try:
        bc = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    except x509.ExtensionNotFound:
        return False
    return bool(bc.ca)


def windows_scratch_path(short_id: str) -> str:
    return f"{_WINDOWS_SCRATCH_DIR}\\pve-flr-portal-ca-{short_id}.crt"


def windows_check_argv(sha1_thumbprint: str) -> list[str]:
    """Exit 0 iff a cert with this thumbprint is already in LocalMachine\\Root."""
    return ["certutil", "-store", "Root", sha1_thumbprint]


def windows_install_argv(scratch_path: str) -> list[str]:
    return ["certutil", "-addstore", "-f", "Root", scratch_path]


def windows_cleanup_argv(scratch_path: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"Remove-Item -LiteralPath '{scratch_path}' -Force -ErrorAction SilentlyContinue",
    ]


def linux_anchor_path(*, has_update_ca_certificates: bool) -> str:
    return _DEB_ANCHOR if has_update_ca_certificates else _RHT_ANCHOR


def linux_update_argv(*, has_update_ca_certificates: bool) -> list[str]:
    return ["update-ca-certificates"] if has_update_ca_certificates else ["update-ca-trust", "extract"]
