"""PH.5: capability detection for push-to-guest restore
(docs/plan.md §7.5). Answers "which restore paths can this guest and
this user actually use right now" from four independent, empirically-
checked facts - never an OS-family guess:

- The VM config's own `agent` flag (is QGA wired up at all).
- The guest agent's own `agent/info` response (wraps QMP `guest-info`),
  whose `supported_commands[]` says what the agent itself allows -
  ground truth for whether guest-exec is blocked, confirmed per-guest
  rather than assumed from the guest OS family.
- The caller's own `VM.GuestAgent.*` privileges on this guest
  (`/access/permissions`) - confirmed set, from the Proxmox
  access-control patch that introduced them: Audit, FileRead,
  FileWrite, FileSystemMgmt, Unrestricted. Only Unrestricted covers
  guest-exec; there is no narrower "exec" privilege.
- The PVE version - 8 only has the coarse VM.Monitor, no granular
  VM.GuestAgent.* at all, so restore is unavailable there regardless
  of the other three checks.

Design A ("quick restore") needs only FileWrite. Design B ("full
restore" - anything needing guest-exec: multi-chunk concatenation,
metadata restore, checksum verify) needs Unrestricted. Reading
agent/info itself needs Audit, so a caller with none of the five
grants gets a clean "unavailable", not a bubbled-up 403.
"""
from dataclasses import dataclass

import httpx

from .auth import SessionData, pve_headers
from .config import settings
from .pve_client import api_node_type

_API_ROOT = f"https://{settings.pve_host}:8006/api2/json"

_MIN_GUESTAGENT_PRIVS_PVE_VERSION = 9


@dataclass(frozen=True)
class PathAvailability:
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class RestoreCapabilities:
    agent_running: bool
    pve_version_ok: bool
    guest_os_family: str | None  # "windows" | "linux" | "bsd" | "macos" | None (unknown)
    design_a: PathAvailability
    design_b: PathAvailability
    verify_supported: bool  # sha256-via-guest-exec verification available (implies design_b)


def _guest_os_family(osinfo: dict | None) -> str | None:
    if not osinfo:
        return None
    # get-osinfo's "id" is the closest thing to a stable machine-readable
    # family marker; kernel-name is a Windows/Linux/... fallback.
    os_id = (osinfo.get("id") or "").lower()
    kernel = (osinfo.get("kernel-name") or "").lower()
    if "windows" in os_id or "windows" in kernel or "mswin" in kernel:
        return "windows"
    if "darwin" in os_id or "darwin" in kernel:
        return "macos"
    if "bsd" in os_id or "bsd" in kernel:
        return "bsd"
    if os_id or "linux" in kernel:
        return "linux"
    return None


def _command_enabled(supported_commands: list[dict], name: str) -> bool | None:
    """None means "not reported" - agent/info's supported_commands isn't
    guaranteed to enumerate every command on every QGA version, so absence
    isn't the same as confirmed-disabled."""
    for entry in supported_commands or []:
        if entry.get("name") == name:
            return bool(entry.get("enabled"))
    return None


def parse_capabilities(
    *,
    vm_config: dict | None,
    agent_info: dict | None,
    permissions: dict | None,
    pve_version_major: int | None,
    osinfo: dict | None = None,
) -> RestoreCapabilities:
    """Pure function: builds the capability decision from already-fetched
    raw API responses. Kept separate from the async fetching below so the
    decision logic is unit-testable without a live PVE/guest."""
    agent_flag = (vm_config or {}).get("agent")
    agent_configured = bool(agent_flag) and str(agent_flag) not in ("0", "0,enabled=0")
    agent_running = agent_configured and agent_info is not None

    pve_version_ok = pve_version_major is not None and pve_version_major >= _MIN_GUESTAGENT_PRIVS_PVE_VERSION

    perms = permissions or {}
    has_file_write = bool(perms.get("VM.GuestAgent.FileWrite")) or bool(perms.get("VM.GuestAgent.Unrestricted"))
    has_unrestricted = bool(perms.get("VM.GuestAgent.Unrestricted"))

    supported = (agent_info or {}).get("supported_commands", [])
    file_write_enabled = _command_enabled(supported, "guest-file-write")
    guest_exec_enabled = _command_enabled(supported, "guest-exec")

    if not pve_version_ok:
        reason = "PVE 8 has no granular VM.GuestAgent.* privileges (VM.Monitor is all-or-nothing)"
        design_a = PathAvailability(False, reason)
        design_b = PathAvailability(False, reason)
    elif not agent_running:
        reason = "qemu-guest-agent is not enabled or not responding for this guest"
        design_a = PathAvailability(False, reason)
        design_b = PathAvailability(False, reason)
    else:
        if not has_file_write:
            design_a = PathAvailability(False, "missing VM.GuestAgent.FileWrite (or .Unrestricted) privilege")
        elif file_write_enabled is False:
            design_a = PathAvailability(False, "guest-file-write is disabled in this guest's agent config")
        else:
            design_a = PathAvailability(True)

        if not has_unrestricted:
            design_b = PathAvailability(False, "missing VM.GuestAgent.Unrestricted privilege")
        elif guest_exec_enabled is False:
            design_b = PathAvailability(False, "guest-exec is disabled in this guest's agent config")
        else:
            design_b = PathAvailability(True)

    return RestoreCapabilities(
        agent_running=agent_running,
        pve_version_ok=pve_version_ok,
        guest_os_family=_guest_os_family(osinfo),
        design_a=design_a,
        design_b=design_b,
        verify_supported=design_b.available,
    )


async def get_restore_capabilities(session: SessionData, guest_type: str, vmid: str) -> RestoreCapabilities:
    """Live orchestration: fetches the four raw facts, then hands them to
    parse_capabilities(). agent/info and get-osinfo failures (guest agent
    not running, or the caller lacking VM.GuestAgent.Audit) are treated as
    "no info available" rather than propagated - a missing grant should
    degrade to "unavailable", not a 500. `guest_type` is the app-internal
    "vm"/"ct" value (matching the rest of the app - task picker, groups,
    etc.), translated to PVE's "qemu"/"lxc" API node segment here."""
    node_type = api_node_type(guest_type)
    headers = pve_headers(session)
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        config_resp = await client.get(
            f"{_API_ROOT}/nodes/localhost/{node_type}/{vmid}/config", headers=headers
        )
        config_resp.raise_for_status()
        vm_config = config_resp.json()["data"]

        perms_resp = await client.get(
            f"{_API_ROOT}/access/permissions", params={"path": f"/vms/{vmid}"}, headers=headers
        )
        perms_resp.raise_for_status()
        permissions = perms_resp.json()["data"]

        version_resp = await client.get(f"{_API_ROOT}/version", headers=headers)
        version_resp.raise_for_status()
        version_str = str(version_resp.json()["data"].get("version", ""))
        pve_version_major = None
        if version_str:
            try:
                pve_version_major = int(version_str.split(".")[0])
            except ValueError:
                pve_version_major = None

        agent_info = None
        osinfo = None
        if node_type == "qemu":
            try:
                info_resp = await client.get(
                    f"{_API_ROOT}/nodes/localhost/qemu/{vmid}/agent/info", headers=headers
                )
                info_resp.raise_for_status()
                agent_info = info_resp.json()["data"]
            except httpx.HTTPStatusError:
                agent_info = None
            try:
                osinfo_resp = await client.get(
                    f"{_API_ROOT}/nodes/localhost/qemu/{vmid}/agent/get-osinfo", headers=headers
                )
                osinfo_resp.raise_for_status()
                osinfo = osinfo_resp.json()["data"].get("result")
            except httpx.HTTPStatusError:
                osinfo = None

    return parse_capabilities(
        vm_config=vm_config,
        agent_info=agent_info,
        permissions=permissions,
        pve_version_major=pve_version_major,
        osinfo=osinfo,
    )
