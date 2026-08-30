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


@dataclass(frozen=True)
class Settings:
    pve_host: str
    pve_storage: str
    pve_token_id: str
    pve_token_secret: str
    pve_verify_ssl: bool

    pbs_host: str
    pbs_datastore: str
    pbs_token_id: str
    pbs_token_secret: str
    pbs_verify_ssl: bool

    # Phase 1 targets exactly one hardcoded guest; multi-guest support is PH.3.
    guest_vmid: str
    guest_type: str


settings = Settings(
    pve_host=_get("PVE_HOST", required=True),
    pve_storage=_get("PVE_STORAGE", required=True),
    pve_token_id=_get("PVE_TOKEN_ID", required=True),
    pve_token_secret=_get("PVE_TOKEN_SECRET", required=True),
    pve_verify_ssl=_bool("PVE_VERIFY_SSL", True),
    pbs_host=_get("PBS_HOST", required=True),
    pbs_datastore=_get("PBS_DATASTORE", required=True),
    pbs_token_id=_get("PBS_TOKEN_ID", required=True),
    pbs_token_secret=_get("PBS_TOKEN_SECRET", required=True),
    pbs_verify_ssl=_bool("PBS_VERIFY_SSL", True),
    guest_vmid=_get("GUEST_VMID", required=True),
    guest_type=_get("GUEST_TYPE", "vm"),
)
