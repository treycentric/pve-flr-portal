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
import httpx

from .auth import SessionData, pve_headers
from .config import settings

_BASE = f"https://{settings.pve_host}:8006/api2/json/nodes/localhost/storage/{settings.pve_storage}/file-restore"
_API_ROOT = f"https://{settings.pve_host}:8006/api2/json"


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


async def list_backup_archives(session: SessionData) -> list[dict]:
    """All backup archives on the configured storage, straight from PVE
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
    """
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        resp = await client.get(
            f"{_API_ROOT}/nodes/localhost/storage/{settings.pve_storage}/content",
            params={"content": "backup"},
            headers=pve_headers(session),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def list_path(session: SessionData, volume: str, filepath: str = "/") -> list[dict]:
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=30.0) as client:
        resp = await client.get(
            f"{_BASE}/list",
            params={"volume": volume, "filepath": filepath},
            headers=pve_headers(session),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def open_download(
    session: SessionData, volume: str, filepath: str, tar: bool = False
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streaming download. Caller owns the returned client/response and must close both."""
    params = {"volume": volume, "filepath": filepath}
    if tar:
        params["tar"] = 1
    # No timeout: cold lookups + large archives can legitimately take a while (§3).
    client = httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=None)
    request = client.build_request("GET", f"{_BASE}/download", params=params, headers=pve_headers(session))
    response = await client.send(request, stream=True)
    if response.status_code >= 400:
        await response.aread()
        await response.aclose()
        await client.aclose()
        response.raise_for_status()
    return client, response
