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


def _float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


_THEMES = ("auto", "light", "dark", "proxmox-dark")


def _theme(name: str, default: str) -> str:
    val = (os.environ.get(name) or default).strip().lower()
    if val not in _THEMES:
        raise RuntimeError(f"{name} must be one of {'|'.join(_THEMES)}, got: {val!r}")
    return val


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

    # PH.5: minimum gap this app waits between the *end* of one
    # guest-agent command and the *start* of its own next one, on the
    # same guest (docs/plan.md §7.5). guest_agent_lock.py already
    # prevents this app's own commands from overlapping (correctness);
    # this is about not monopolizing the channel once it's free - a
    # scheduled PBS backup's fs-freeze or another admin's `qm agent`
    # call still needs a turn, especially once a multi-chunk restore is
    # sending many sequential guest-agent commands back to back.
    # Defaults to 0 (disabled) since the right value is workload-
    # dependent and there's no evidence yet of what a typical homelab
    # needs - tune up via GUEST_AGENT_MIN_COMMAND_GAP_SECONDS if a
    # restore is observed crowding out other guest-agent users.
    guest_agent_min_command_gap_seconds: float

    # Design C (docs/plan.md §7.6, issue #22 - not yet wired into a live
    # restore): the data-plane NIC(s) a restore's network-pull download
    # endpoint may be served from, one entry per non-routable subnet a
    # target guest might live in. Raw JSON here, parsed by
    # restore_network_pull.parse_data_nics() - kept as a plain string
    # rather than parsed eagerly so a malformed value fails where it's
    # used (with a clear error) instead of crashing the whole app at
    # import time over a feature most deployments won't configure.
    # Empty by default - Design C is simply never offered until an admin
    # opts in by setting this.
    restore_data_nics_json: str
    # How long a single-use network-pull download token stays valid
    # before it's treated as expired - long enough for guest-exec to
    # kick off the bootstrap script and for it to start the fetch, short
    # enough that a leaked/logged URL isn't useful for long.
    restore_download_token_ttl_seconds: float
    # The port a Design C download URL points at on the chosen data NIC.
    # 0 (default) means "same as PORT" - only worth overriding once the
    # actual per-interface bind work (docs/plan.md §7.6, still not
    # started) stands up a listener on the data NIC(s) that isn't just
    # the same process/port the main UI+PVE-API listener already uses.
    # Deliberately plain HTTP, never HTTPS, on this one route/NIC pair -
    # see the "why HTTP, not HTTPS" note in restore_runner.py's
    # _try_design_c().
    restore_data_nic_port: int

    # pve_client.run_guest_exec()'s default ~15s poll budget is sized for
    # commands that don't scale with file size (mkdir, an exists check).
    # Confirmed live 2026-09-01: nowhere near enough for one that does -
    # Direct Network Transfer's actual fetch, or hashing/concatenating a
    # large file. Used explicitly for just those calls, not as a new
    # blanket default.
    restore_long_running_exec_timeout_seconds: float

    # Issue #29: the colour theme a browser gets before the visitor has
    # made their own choice (stored client-side in localStorage). One of
    # auto|light|dark; "auto" follows the OS but resolves to dark when
    # the OS states no preference. Purely a default - a logged-in user
    # can still switch themes from the user menu.
    default_theme: str


settings = Settings(
    pve_host=_get("PVE_HOST", required=True),
    pve_storage=_get("PVE_STORAGE", required=True),
    pve_verify_ssl=_bool("PVE_VERIFY_SSL", True),
    session_idle_timeout_minutes=_int("SESSION_IDLE_TIMEOUT_MINUTES", 30),
    port=_int("PORT", 8008),
    tls_cert_file=_get("TLS_CERT_FILE", "certs/portal.crt"),
    tls_key_file=_get("TLS_KEY_FILE", "certs/portal.key"),
    guest_agent_min_command_gap_seconds=_float("GUEST_AGENT_MIN_COMMAND_GAP_SECONDS", 0.0),
    restore_data_nics_json=_get("RESTORE_DATA_NICS", "[]"),
    restore_download_token_ttl_seconds=_float("RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS", 120.0),
    restore_data_nic_port=_int("RESTORE_DATA_NIC_PORT", 0),
    restore_long_running_exec_timeout_seconds=_float("RESTORE_LONG_RUNNING_EXEC_TIMEOUT_SECONDS", 1800.0),
    default_theme=_theme("DEFAULT_THEME", "auto"),
)
