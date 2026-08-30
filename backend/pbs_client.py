"""Client for the PBS admin API — used only to enumerate which snapshots
exist for a guest (see docs/plan.md §4: listing snapshots is PBS-side,
listing/reading files inside a snapshot is PVE-side).
"""
import httpx

from .config import settings

_BASE = f"https://{settings.pbs_host}:8007/api2/json/admin/datastore/{settings.pbs_datastore}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"PBSAPIToken={settings.pbs_token_id}:{settings.pbs_token_secret}"}


async def list_snapshots(backup_type: str, backup_id: str) -> list[dict]:
    async with httpx.AsyncClient(verify=settings.pbs_verify_ssl, timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE}/snapshots",
            params={"backup-type": backup_type, "backup-id": backup_id},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def list_groups() -> list[dict]:
    """All backup groups (guests) present in the datastore, for the Task picker."""
    async with httpx.AsyncClient(verify=settings.pbs_verify_ssl, timeout=15.0) as client:
        resp = await client.get(f"{_BASE}/groups", headers=_headers())
        resp.raise_for_status()
        return resp.json()["data"]
