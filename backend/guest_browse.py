"""PH.5: browse a guest's filesystem via `guest-exec`, so a restore
destination directory can be picked rather than typed blind
(docs/plan.md §7.5). There is no dedicated QGA directory-listing
command - only `guest-exec` running a platform listing tool - so this
needs `VM.GuestAgent.Unrestricted`, the same gate as metadata restore
and verify.
"""
import asyncio
import ntpath
import posixpath
import re

import httpx

from .auth import SessionData, pve_headers
from .config import settings
from .guest_agent_lock import call_with_retries, guest_agent_command
from .pve_client import api_node_type

_API_ROOT = f"https://{settings.pve_host}:8006/api2/json"

# Conservative denylist, not a full shell-safety abstraction - acceptable
# for a single-admin homelab tool where the path being browsed is either
# empty or a value this same endpoint itself returned on a prior call,
# never arbitrary external input.
_UNSAFE_PATH_CHARS = re.compile(r"[\"'`;&|$<>\r\n]")


class UnsafePathError(ValueError):
    pass


class ListingError(RuntimeError):
    pass


def _check_path_safe(path: str) -> None:
    if _UNSAFE_PATH_CHARS.search(path):
        raise UnsafePathError(f"Path contains unsupported characters: {path!r}")


async def _run_exec(session: SessionData, node_type: str, vmid: str, argv: list[str]) -> tuple[int, str, str]:
    """Runs one guest-exec command to completion (polling exec-status) and
    returns (exitcode, stdout, stderr). The whole exec-then-poll sequence
    runs under guest_agent_lock's per-vmid lock (see that module) - the
    exec-status polls are themselves guest-agent commands sharing the
    same single-command-at-a-time virtio-serial channel, so nothing else
    from *this app* may interleave for the full duration, not just the
    initial POST.

    The initial POST starts a new in-guest command, so - like every other
    "start something new" call in this app - it only retries via
    call_with_retries on a definite HTTP error response (something else
    was legitimately using the channel), never on an ambiguous timeout.
    Polling exec-status is different: it only ever asks about a pid that
    already exists, so re-querying it is safe regardless of *why* the
    previous poll failed - a transient error there just means "try again
    next tick" rather than "not done yet", reusing the same ~15s budget
    rather than aborting the whole listing over one bad poll."""
    headers = pve_headers(session)

    async def start():
        resp = await client.post(
            f"{_API_ROOT}/nodes/localhost/{node_type}/{vmid}/agent/exec",
            data={"command": argv},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()["data"]["pid"]

    async with (
        guest_agent_command(vmid),
        httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=20.0) as client,
    ):
        pid = await call_with_retries(start)

        for _ in range(30):  # ~15s of polling - a listing is a fast command
            try:
                status_resp = await client.get(
                    f"{_API_ROOT}/nodes/localhost/{node_type}/{vmid}/agent/exec-status",
                    params={"pid": pid},
                    headers=headers,
                )
                status_resp.raise_for_status()
                data = status_resp.json()["data"]
                if data.get("exited"):
                    return data.get("exitcode", -1), data.get("out-data", ""), data.get("err-data", "")
            except httpx.HTTPError:
                pass  # transient - try again next tick, see docstring
            await asyncio.sleep(0.5)
        raise ListingError("Directory listing timed out")


def _windows_parent(path: str) -> str | None:
    normalized = path.rstrip("\\") or path
    parent_raw = ntpath.dirname(normalized)
    if not parent_raw or parent_raw.rstrip("\\").upper() == normalized.rstrip("\\").upper():
        return None  # already at a drive root - "up" means back to the drive list
    return parent_raw if parent_raw.endswith("\\") else parent_raw + "\\"


def _posix_parent(target: str) -> str | None:
    if target == "/":
        return None
    return posixpath.dirname(target.rstrip("/")) or "/"


async def list_directories(
    session: SessionData, guest_type: str, vmid: str, guest_os_family: str | None, path: str | None
) -> dict:
    """{"path", "parent", "separator", "entries": [{"name", "path"}, ...]}.
    `path` of None/"" means "show the top level" - drives on Windows,
    "/" on everything else."""
    node_type = api_node_type(guest_type)
    is_windows = guest_os_family == "windows"
    sep = "\\" if is_windows else "/"

    if path:
        _check_path_safe(path)

    if is_windows:
        if not path:
            exitcode, out, err = await _run_exec(
                session, node_type, vmid, ["cmd", "/c", "wmic", "logicaldisk", "get", "caption"]
            )
            if exitcode != 0:
                raise ListingError(err.strip() or out.strip() or f"Listing failed (exit {exitcode})")
            names = [ln.strip() for ln in out.splitlines() if ln.strip() and ln.strip().lower() != "caption"]
            entries = [{"name": n, "path": n + "\\"} for n in names]
            return {"path": None, "parent": None, "separator": sep, "entries": entries}

        exitcode, out, err = await _run_exec(session, node_type, vmid, ["cmd", "/c", "dir", path, "/b", "/ad"])
        if exitcode != 0:
            raise ListingError(err.strip() or out.strip() or f"Listing failed (exit {exitcode})")
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        base = path if path.endswith("\\") else path + "\\"
        entries = [{"name": n, "path": base + n} for n in names]
        return {"path": path, "parent": _windows_parent(path), "separator": sep, "entries": entries}

    target = path or "/"
    exitcode, out, err = await _run_exec(
        session, node_type, vmid, ["find", target, "-mindepth", "1", "-maxdepth", "1", "-type", "d"]
    )
    if exitcode != 0:
        raise ListingError(err.strip() or out.strip() or f"Listing failed (exit {exitcode})")
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append({"name": posixpath.basename(line), "path": line})
    return {"path": target, "parent": _posix_parent(target), "separator": sep, "entries": entries}
