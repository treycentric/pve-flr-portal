import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


@dataclass(frozen=True)
class Settings:
    pve_host: str
    pve_storage: str
    pve_verify_ssl: bool

    # PH.4: per-user PVE ticket auth replaces the old shared PVE/PBS API
    # tokens (docs/plan.md §7.1) - no PBS credentials or static PVE token
    # exist anymore, everything goes through the logged-in user's own
    # PVE session.
    session_idle_timeout_minutes: int

    port: int
    tls_cert_file: str
    tls_key_file: str


settings = Settings(
    pve_host=_get("PVE_HOST", required=True),
    pve_storage=_get("PVE_STORAGE", required=True),
    pve_verify_ssl=_bool("PVE_VERIFY_SSL", True),
    session_idle_timeout_minutes=_int("SESSION_IDLE_TIMEOUT_MINUTES", 30),
    port=_int("PORT", 8008),
    tls_cert_file=_get("TLS_CERT_FILE", "certs/portal.crt"),
    tls_key_file=_get("TLS_KEY_FILE", "certs/portal.key"),
)
