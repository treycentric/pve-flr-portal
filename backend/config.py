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


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    val = (os.environ.get(name) or default).strip().lower()
    if val not in allowed:
        raise RuntimeError(f"{name} must be one of {'|'.join(allowed)}, got: {val!r}")
    return val


# Data-plane TLS modes for Direct Network Transfer (issue #47), weakest
# to strongest. `restore_network_pull` depends on this exact ordering to
# walk the PREFERRED->MINIMUM downgrade ladder.
TLS_MODES: tuple[str, ...] = ("plaintext", "insecure", "verify")


def _csv(name: str, default: str) -> tuple[str, ...]:
    """Comma-separated env var -> tuple of trimmed, non-empty entries,
    order preserved, duplicates dropped. Used for PVE_STORAGE, which
    accepts one *or more* storage ids (issue #43)."""
    raw = os.environ.get(name)
    raw = default if raw is None else raw
    seen: dict[str, None] = {}
    for part in raw.split(","):
        part = part.strip()
        if part:
            seen.setdefault(part, None)
    return tuple(seen)


_THEMES = ("auto", "light", "dark", "proxmox-dark")


def _theme(name: str, default: str) -> str:
    val = (os.environ.get(name) or default).strip().lower()
    if val not in _THEMES:
        raise RuntimeError(f"{name} must be one of {'|'.join(_THEMES)}, got: {val!r}")
    return val


@dataclass(frozen=True)
class Settings:
    pve_host: str
    # One or more PBS-backed storage ids (issue #43). My cluster has 3
    # PBS storages, each on a different PBS namespace; PVE hides the
    # namespace inside the storage config, so from here "3 namespaces" is
    # just "3 storage ids". Enumeration queries every entry and merges;
    # per-snapshot calls key the /storage/{id}/ URL segment off the
    # volid's own prefix, not this list (pve_client.py).
    pve_storages: tuple[str, ...]
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

    # Design C / "Direct Network Transfer" (docs/plan.md §7.6, issue #22,
    # shipped v1.1.0): the data-plane NIC(s) a restore's network-pull
    # download endpoint may be served from, one entry per non-routable subnet a
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
    # The port a Direct Network Transfer download URL points at on the
    # chosen data NIC, and that run.py binds the data-plane listener(s)
    # on. 0 (default) resolves to PORT+1 - a specific-IP listener on the
    # same port as the main 0.0.0.0 bind collides (EADDRINUSE) on most
    # kernels. Set it explicitly to share the port if your setup allows.
    restore_data_nic_port: int

    # Direct Network Transfer data-plane TLS (issue #47, docs/plan.md
    # §7.6.1). `plaintext` = HTTP listener (pre-#47 behaviour);
    # `insecure`/`verify` = HTTPS listener. The ladder: per guest,
    # resolve the strongest mode in
    # [minimum, preferred] that the detected fetch tool can actually do
    # (curl/wget/python/Invoke-WebRequest/WinHttpRequest can skip-verify;
    # certutil/bitsadmin can't; bash /dev/tcp has no TLS at all). If none
    # qualifies, `on_unmet` decides: `fallback` (use Design B - the
    # chunked write over QMP, which never touches the data network) or
    # `fail`.
    #
    # `verify` (the default): the guest validates the data-plane cert.
    # `install_ca` controls whether this app puts the cert into the
    # guest's trust store first (guest_ca.py + restore_runner) - `never`
    # (the guest must already trust it), `if-missing` (check, install
    # only if absent), or `always`. If install is needed but fails, the
    # job steps down to `insecure` when the ladder allows, else on_unmet.
    #
    # When `preferred` is not `plaintext` the data listener is HTTPS, so
    # a `plaintext` rung below it can't be served over the network (no
    # second HTTP port) - `minimum` is effectively clamped up to
    # `insecure` for the network path. `preferred=plaintext` keeps the
    # exact pre-#47 behaviour.
    restore_data_nic_tls_preferred: str  # plaintext | insecure | verify
    restore_data_nic_tls_minimum: str  # plaintext | insecure | verify
    restore_data_nic_tls_on_unmet: str  # fallback | fail
    restore_data_nic_tls_min_version: str  # 1.2 | 1.3
    restore_data_nic_tls_install_ca: str  # never | if-missing | always
    # Data-plane cert/key/CA. Auto-generated self-signed (with IP SANs
    # for every RESTORE_DATA_NICS entry, plus any per-NIC `hostname`) if
    # cert+key are both absent; an admin-supplied pair at these paths is
    # used as-is and never overwritten. ca_file (blank -> the cert file
    # itself) is the chain the listener presents and the cert
    # `install_ca` puts into guest trust stores for `verify`.
    restore_data_nic_tls_cert_file: str
    restore_data_nic_tls_key_file: str
    restore_data_nic_tls_ca_file: str

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

    # Issue #30: one writable directory for the app's own small, durable
    # state. Nothing writes here yet - it exists so the features that
    # will need persistence (#29's per-user prefs, #14's session store,
    # PH.6's dir_cache) share one deploy-provisioned location instead of
    # each inventing their own. Default "data" for a source checkout;
    # the systemd unit and docker-compose point it at a real volume.
    # Still no database and no extra service (CLAUDE.md) - at most a
    # single small file written from the request path.
    data_dir: Path

    @property
    def pve_storage(self) -> str:
        """The first configured storage id. Kept for the handful of
        callers/tests that only need "a" storage; new code that lists
        backups should iterate `pve_storages` (issue #43)."""
        return self.pve_storages[0]


def _storages_required() -> tuple[str, ...]:
    val = _csv("PVE_STORAGE", "")
    if not val:
        raise RuntimeError("Missing required env var: PVE_STORAGE")
    return val


def _tls_preferred_and_minimum() -> tuple[str, str]:
    """PREFERRED and MINIMUM data-plane TLS modes (issue #47). MINIMUM
    must not be stricter than PREFERRED - caught here rather than as a
    confusing "no mode qualifies" at restore time."""
    preferred = _choice("RESTORE_DATA_NIC_TLS_PREFERRED", "verify", TLS_MODES)
    minimum = _choice("RESTORE_DATA_NIC_TLS_MINIMUM", "insecure", TLS_MODES)
    if TLS_MODES.index(minimum) > TLS_MODES.index(preferred):
        raise RuntimeError(
            f"RESTORE_DATA_NIC_TLS_MINIMUM ({minimum}) cannot be stricter than "
            f"RESTORE_DATA_NIC_TLS_PREFERRED ({preferred})"
        )
    return preferred, minimum


_tls_preferred, _tls_minimum = _tls_preferred_and_minimum()


settings = Settings(
    pve_host=_get("PVE_HOST", required=True),
    pve_storages=_storages_required(),
    pve_verify_ssl=_bool("PVE_VERIFY_SSL", True),
    session_idle_timeout_minutes=_int("SESSION_IDLE_TIMEOUT_MINUTES", 30),
    port=_int("PORT", 8008),
    tls_cert_file=_get("TLS_CERT_FILE", "certs/portal.crt"),
    tls_key_file=_get("TLS_KEY_FILE", "certs/portal.key"),
    guest_agent_min_command_gap_seconds=_float("GUEST_AGENT_MIN_COMMAND_GAP_SECONDS", 0.0),
    restore_data_nics_json=_get("RESTORE_DATA_NICS", "[]"),
    restore_download_token_ttl_seconds=_float("RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS", 120.0),
    restore_data_nic_port=_int("RESTORE_DATA_NIC_PORT", 0),
    restore_data_nic_tls_preferred=_tls_preferred,
    restore_data_nic_tls_minimum=_tls_minimum,
    restore_data_nic_tls_on_unmet=_choice("RESTORE_DATA_NIC_TLS_ON_UNMET", "fallback", ("fallback", "fail")),
    restore_data_nic_tls_min_version=_choice("RESTORE_DATA_NIC_TLS_MIN_VERSION", "1.2", ("1.2", "1.3")),
    restore_data_nic_tls_install_ca=_choice(
        "RESTORE_DATA_NIC_TLS_INSTALL_CA", "never", ("never", "if-missing", "always")
    ),
    restore_data_nic_tls_cert_file=_get("RESTORE_DATA_NIC_TLS_CERT_FILE", "certs/data-plane.crt"),
    restore_data_nic_tls_key_file=_get("RESTORE_DATA_NIC_TLS_KEY_FILE", "certs/data-plane.key"),
    restore_data_nic_tls_ca_file=_get("RESTORE_DATA_NIC_TLS_CA_FILE", ""),
    restore_long_running_exec_timeout_seconds=_float("RESTORE_LONG_RUNNING_EXEC_TIMEOUT_SECONDS", 1800.0),
    default_theme=_theme("DEFAULT_THEME", "auto"),
    data_dir=_path("PFR_DATA_DIR", "data"),
)


def ensure_data_dir() -> Path:
    """Create PFR_DATA_DIR if it doesn't exist yet, and return it.

    Called from run.py rather than at import time, so `pytest` and a bare
    `uvicorn backend.main:app` don't leave a stray directory behind. Any
    feature that persists state should also call this before its first
    write.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir
