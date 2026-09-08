"""Client for the PVE file-restore API and, as of PH.4, backup content
listing - everything the app needs now comes from PVE alone (docs/plan.md
§7.1). Auth is per-request: every call takes the caller's `SessionData`
(PH.4 ticket-auth session) and sends it as PVE expects
(`Cookie: PVEAuthCookie=...` + `CSRFPreventionToken`), rather than a
static service-account token.

Contract confirmed in docs/plan.md §3: the node segment is always the
literal string "localhost" regardless of the real hostname; filepath is
the literal string "/" for root and base64 for anything deeper. The
list response already hands back ready-to-use base64 filepath tokens
for each entry, so callers should treat filepath as an opaque token
from the API rather than re-deriving it from a display path.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from .auth import SessionData, pve_headers
from .config import settings
from .guest_agent_lock import call_with_retries, guest_agent_command

_API_ROOT = f"https://{settings.pve_host}:8006/api2/json"

_log = logging.getLogger("pve_flr_portal.pve_client")


def _storage_of(volume: str) -> str:
    """"pbs-ns-a:backup/vm/133/2026-…Z" -> "pbs-ns-a". The volid's own
    prefix is the storage a snapshot lives on - with multiple configured
    storages (issue #43) the /storage/{id}/ URL segment for file-restore
    has to come from here, not from a single global setting."""
    return volume.split(":", 1)[0]


def _file_restore_base(volume: str) -> str:
    return f"{_API_ROOT}/nodes/localhost/storage/{_storage_of(volume)}/file-restore"

# Conservative denylist, not a full shell-safety abstraction - acceptable
# for a single-admin homelab tool where a path reaching this check is
# either empty, a value guest_browse.py itself returned on a prior
# browse call, or a user-typed destination directory that this same
# check gates before it ever reaches a shell-interpreted guest-exec
# command (concatenation, metadata restore) - never arbitrary external
# input (docs/plan.md §7.5).
_UNSAFE_PATH_CHARS = re.compile(r"[\"'`;&|$<>\r\n]")


class UnsafePathError(ValueError):
    pass


def check_path_safe(path: str) -> None:
    if _UNSAFE_PATH_CHARS.search(path):
        raise UnsafePathError(f"Path contains unsupported characters: {path!r}")

# App-internal guest type ("vm"/"ct", the second volid path segment - see
# the docstring above) vs. PVE's actual API node-path segment ("qemu"/
# "lxc", confirmed live against a real guest - docs/plan.md §7.5). PH.5's
# guest-agent calls hit /nodes/localhost/{qemu|lxc}/{vmid}/... directly,
# so every such call needs this translated first - everywhere else in the
# app (grouping, the task picker, display) stays in "vm"/"ct" throughout.
API_NODE_TYPE = {"vm": "qemu", "ct": "lxc"}


def api_node_type(guest_type: str) -> str:
    try:
        return API_NODE_TYPE[guest_type]
    except KeyError:
        raise ValueError(f"Unknown guest type: {guest_type}") from None


async def list_guest_names(session: SessionData) -> dict[str, str]:
    """Best-effort vmid -> guest name map from /cluster/resources. This
    endpoint has no explicit privilege gate (allowtoken, permissions:
    user=all), but PVE field-filters what it returns based on what the
    caller can already see, so a narrowly-scoped account (VM.Backup only,
    no VM.Audit) may get entries back with no 'name' field. Callers should
    treat a missing/absent name as "unknown", not an error.
    """
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        resp = await client.get(f"{_API_ROOT}/cluster/resources", params={"type": "vm"}, headers=pve_headers(session))
        resp.raise_for_status()
        return {str(item["vmid"]): item["name"] for item in resp.json()["data"] if item.get("name")}


@dataclass
class StorageError:
    """One configured storage the current user couldn't enumerate."""

    storage: str
    detail: str


@dataclass
class BackupListing:
    archives: list[dict] = field(default_factory=list)
    errors: list[StorageError] = field(default_factory=list)


def _storage_error_detail(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 403:
            return "permission denied — grant the FileRestoreReader role on this storage (see the README)"
        if code == 404:
            return "no such storage on this PVE node — check the id in PVE_STORAGE"
        return f"PVE returned HTTP {code}"
    return f"could not reach PVE ({exc.__class__.__name__})"


async def _list_backup_archives_one(client: httpx.AsyncClient, session: SessionData, storage: str) -> list[dict]:
    resp = await client.get(
        f"{_API_ROOT}/nodes/localhost/storage/{storage}/content",
        params={"content": "backup"},
        headers=pve_headers(session),
    )
    resp.raise_for_status()
    return resp.json()["data"]


async def list_backup_archives(session: SessionData) -> "BackupListing":
    """All backup archives across every configured storage, straight from PVE
    (GET /nodes/localhost/storage/{storage}/content?content=backup) -
    replaces the old direct-to-PBS admin API calls entirely (docs/plan.md
    §7.1). Confirmed response shape against the real environment
    (2026-08-30): one entry per archive, e.g.

        {"vmid": 133, "size": ..., "volid": "pbs:backup/vm/133/<iso>Z",
         "verification": {"state": "ok", "upid": "..."}, "format": "pbs-vm",
         "notes": "<guest hostname>", "content": "backup",
         "ctime": <unix ts>, "subtype": "qemu"}

    `volid` already embeds the guest type ("vm"/"ct") as its second path
    segment, so callers should parse type from `volid` rather than the
    "subtype" field (which uses PVE's "qemu"/"lxc" naming instead).
    Results are gated by the same VM.Backup permission file-restore
    itself requires, so a caller only ever sees archives for guests
    their own PVE account has that permission on.

    Issue #43: iterates every configured storage (my cluster runs 3, one
    per PBS namespace) and merges. A storage the caller can't read (403)
    or that is momentarily unreachable is recorded in the returned
    `.errors` and skipped, never raised — a single misconfigured or
    inaccessible storage must not 500 the whole portal (that was the
    regression the first cut of this feature shipped). The one exception
    is a 401: that means the ticket is bad and belongs to the auth
    handler (re-login), so it propagates.
    """
    listing = BackupListing()
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        for storage in settings.pve_storages:
            try:
                listing.archives.extend(await _list_backup_archives_one(client, session, storage))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise
                _log.warning("skipping storage %r: %s", storage, exc)
                listing.errors.append(StorageError(storage, _storage_error_detail(exc)))
            except httpx.HTTPError as exc:
                _log.warning("skipping storage %r: %s", storage, exc)
                listing.errors.append(StorageError(storage, _storage_error_detail(exc)))
    return listing


async def list_path(session: SessionData, volume: str, filepath: str = "/") -> list[dict]:
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=30.0) as client:
        resp = await client.get(
            f"{_file_restore_base(volume)}/list",
            params={"volume": volume, "filepath": filepath},
            headers=pve_headers(session),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def write_guest_file(session: SessionData, guest_type: str, vmid: str, path: str, content: str) -> None:
    """One `agent/file-write` call (PH.5, docs/plan.md §7.5) - a genuine
    one-shot: no handle/offset, truncates and overwrites whatever was at
    `path`. `content` must already be the wire-ready string
    (`restore_chunking.py`'s Latin-1 mapping of raw bytes, confirmed live
    against a real guest to round-trip losslessly - NOT base64, which
    this endpoint does not decode). `guest_type` is the app-internal
    "vm"/"ct" value - translated to PVE's "qemu"/"lxc" node segment here.
    Runs under guest_agent_lock's per-vmid lock (serializes this app's own
    overlapping requests) and retries via call_with_retries (rides out a
    legitimate busy response from something else using the same channel -
    safe to resend here since a repeat of the exact same truncate-and-
    write is idempotent in effect) - see guest_agent_lock's module
    docstring for the full rationale on both."""
    node_type = api_node_type(guest_type)

    async def do_write():
        resp = await client.post(
            f"{_API_ROOT}/nodes/localhost/{node_type}/{vmid}/agent/file-write",
            data={"file": path, "content": content},
            headers=pve_headers(session),
        )
        resp.raise_for_status()

    async with (
        guest_agent_command(vmid),
        httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=30.0) as client,
    ):
        await call_with_retries(do_write)


class GuestExecTimeout(RuntimeError):
    pass


async def run_guest_exec(
    session: SessionData, guest_type: str, vmid: str, argv: list[str], timeout_seconds: float = 15.0
) -> tuple[int, str, str]:
    """Runs one guest-exec command to completion (polling exec-status) and
    returns (exitcode, stdout, stderr). `guest_type` is the app-internal
    "vm"/"ct" value, translated here like every other guest-agent call.

    The whole exec-then-poll sequence runs under guest_agent_lock's
    per-vmid lock (see that module) - the exec-status polls are
    themselves guest-agent commands sharing the same single-command-at-
    a-time virtio-serial channel, so nothing else from *this app* may
    interleave for the full duration, not just the initial POST.

    The initial POST starts a new in-guest command, so - like every
    other "start something new" call in this app - it only retries via
    call_with_retries on a definite HTTP error response (something else
    was legitimately using the channel), never on an ambiguous timeout.
    Polling exec-status is different: it only ever asks about a pid that
    already exists, so re-querying it is safe regardless of *why* the
    previous poll failed - a transient error there just means "try again
    next tick" rather than "not done yet", reusing the same overall
    budget rather than aborting the whole command over one bad poll.

    `timeout_seconds` defaults to ~15s - fine for the vast majority of
    calls this app makes (mkdir, an exists check, listing a directory),
    which don't scale with file size. Confirmed live 2026-09-01 that this
    default is nowhere near enough for a command whose duration DOES
    scale with content size - Direct Network Transfer's actual fetch,
    or hashing/concatenating a large file - restore_runner.py passes a
    much longer budget explicitly for exactly those calls."""
    node_type = api_node_type(guest_type)
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

        poll_interval = 0.5
        max_polls = max(1, round(timeout_seconds / poll_interval))
        for _ in range(max_polls):
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
            await asyncio.sleep(poll_interval)
        raise GuestExecTimeout("Guest command timed out")


async def open_download(
    session: SessionData, volume: str, filepath: str, tar: bool = False
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streaming download. Caller owns the returned client/response and must close both."""
    params = {"volume": volume, "filepath": filepath}
    if tar:
        params["tar"] = 1
    # No timeout: cold lookups + large archives can legitimately take a while (§3).
    client = httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=None)
    request = client.build_request(
        "GET", f"{_file_restore_base(volume)}/download", params=params, headers=pve_headers(session)
    )
    response = await client.send(request, stream=True)
    if response.status_code >= 400:
        await response.aread()
        await response.aclose()
        await client.aclose()
        response.raise_for_status()
    return client, response
