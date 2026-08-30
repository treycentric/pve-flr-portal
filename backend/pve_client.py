"""Client for the PVE file-restore API (nodes/localhost/storage/{storage}/file-restore/*).

Contract confirmed in docs/plan.md §3: the node segment is always the
literal string "localhost" regardless of the real hostname; filepath is
the literal string "/" for root and base64 for anything deeper. The
list response already hands back ready-to-use base64 filepath tokens
for each entry, so callers should treat filepath as an opaque token
from the API rather than re-deriving it from a display path.
"""
import httpx

from .config import settings

_BASE = f"https://{settings.pve_host}:8006/api2/json/nodes/localhost/storage/{settings.pve_storage}/file-restore"


def _headers() -> dict[str, str]:
    return {"Authorization": f"PVEAPIToken={settings.pve_token_id}={settings.pve_token_secret}"}


async def list_path(volume: str, filepath: str = "/") -> list[dict]:
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=30.0) as client:
        resp = await client.get(
            f"{_BASE}/list",
            params={"volume": volume, "filepath": filepath},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def open_download(volume: str, filepath: str, tar: bool = False) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streaming download. Caller owns the returned client/response and must close both."""
    params = {"volume": volume, "filepath": filepath}
    if tar:
        params["tar"] = 1
    # No timeout: cold lookups + large archives can legitimately take a while (§3).
    client = httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=None)
    request = client.build_request("GET", f"{_BASE}/download", params=params, headers=_headers())
    response = await client.send(request, stream=True)
    if response.status_code >= 400:
        await response.aread()
        await response.aclose()
        await client.aclose()
        response.raise_for_status()
    return client, response
