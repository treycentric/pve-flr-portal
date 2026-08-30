import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pbs_client, pve_client
from .config import settings

app = FastAPI(title="pve-backup-portal")

_TEMPLATES_DIR = "backend/templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["fromtimestamp"] = lambda ts: (
    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts is not None else ""
)

app.mount("/static", StaticFiles(directory="backend/static"), name="static")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _static_version(filename: str) -> int:
    """File mtime, used as a cache-busting query param so browsers pick up
    edits to app.js/style.css immediately instead of serving a stale copy."""
    try:
        return int((_STATIC_DIR / filename).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["static_version"] = _static_version


def _iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _volid_for(guest_type: str, guest_vmid: str, backup_time_iso: str) -> str:
    return f"{settings.pve_storage}:backup/{guest_type}/{guest_vmid}/{backup_time_iso}"


@app.get("/")
async def index(request: Request, task: str | None = None):
    raw_groups = await pbs_client.list_groups()
    try:
        guest_names = await pve_client.list_guest_names()
    except httpx.HTTPStatusError:
        guest_names = {}
    groups = sorted(
        (
            {
                "type": g["backup-type"],
                "vmid": g["backup-id"],
                "last_backup": g["last-backup"],
                "name": guest_names.get(g["backup-id"]),
            }
            for g in raw_groups
        ),
        key=lambda g: (g["type"], g["vmid"]),
    )

    if task and ":" in task and any(f"{g['type']}:{g['vmid']}" == task for g in groups):
        guest_type, guest_vmid = task.split(":", 1)
    elif groups:
        guest_type, guest_vmid = groups[0]["type"], groups[0]["vmid"]
    else:
        guest_type, guest_vmid = settings.guest_type, settings.guest_vmid

    snapshots = []
    if guest_vmid:
        raw_snapshots = await pbs_client.list_snapshots(guest_type, guest_vmid)
        for snap in sorted(raw_snapshots, key=lambda s: s["backup-time"], reverse=True):
            iso = _iso_from_unix(snap["backup-time"])
            snapshots.append(
                {
                    "volume": _volid_for(guest_type, guest_vmid, iso),
                    "time": iso,
                    "size": snap.get("size"),
                    "verified": bool(snap.get("verification")),
                }
            )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "snapshots": snapshots,
            "snapshots_json": json.dumps(snapshots),
            "guest_vmid": guest_vmid,
            "guest_type": guest_type,
            "groups_json": json.dumps(groups),
        },
    )


def _type_label(entry: dict, at_root: bool) -> str:
    if not bool(entry.get("leaf", True)):
        return "Drive" if at_root else "Folder"
    suffix = PurePosixPath(entry.get("text", "")).suffix
    return f"{suffix[1:].upper()} File" if suffix else "File"


def _pve_error_message(exc: httpx.HTTPStatusError) -> str:
    reason = exc.response.reason_phrase
    if reason and reason.strip().lower() not in ("bad request", ""):
        return reason
    try:
        data = exc.response.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:
        pass
    return str(exc)


@app.get("/api/browse")
async def browse(request: Request, volume: str, filepath: str = "/"):
    try:
        entries = await pve_client.list_path(volume, filepath)
    except httpx.HTTPStatusError as exc:
        return templates.TemplateResponse(
            request,
            "partials/browse_error.html",
            {"detail": _pve_error_message(exc)},
        )
    at_root = filepath == "/"
    for entry in entries:
        entry.setdefault("mtime", None)
        entry.setdefault("size", None)
        leaf = bool(entry.get("leaf", True))
        text = entry.get("text", "")
        entry["download_name"] = text + (".zip" if not leaf else "")
        entry["item_json"] = json.dumps({"filepath": entry["filepath"], "leaf": leaf, "name": text})
        entry["type_label"] = _type_label(entry, at_root)
    entries.sort(key=lambda e: (bool(e.get("leaf", True)), e.get("text", "").lower()))
    return templates.TemplateResponse(
        request,
        "partials/file_grid.html",
        {"entries": entries, "volume": volume},
    )


@app.get("/api/tree")
async def tree(request: Request, volume: str, filepath: str = "/", crumbs: str = "[]"):
    try:
        entries = await pve_client.list_path(volume, filepath)
    except httpx.HTTPStatusError:
        entries = []
    at_root = filepath == "/"
    parent_crumbs = json.loads(crumbs)
    nodes = []
    for entry in entries:
        if bool(entry.get("leaf", True)):
            continue
        text = entry.get("text", "")
        child_crumbs = parent_crumbs + [{"label": text, "filepath": entry["filepath"]}]
        nodes.append(
            {
                "filepath": entry["filepath"],
                "text": text,
                "type_label": _type_label(entry, at_root),
                "crumbs_json": json.dumps(child_crumbs),
            }
        )
    return templates.TemplateResponse(
        request,
        "partials/tree_nodes.html",
        {"nodes": nodes, "volume": volume},
    )


def _content_disposition(filename: str) -> str:
    safe = filename.replace('"', "'").replace("\r", "").replace("\n", "")
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


@app.get("/api/download")
async def download(volume: str, filepath: str, tar: bool = False, name: str | None = None):
    client, response = await pve_client.open_download(volume, filepath, tar)

    async def body():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    headers = {}
    if name:
        headers["content-disposition"] = _content_disposition(name)
    elif content_disposition := response.headers.get("content-disposition"):
        headers["content-disposition"] = content_disposition
    media_type = response.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(body(), media_type=media_type, headers=headers)


@app.get("/api/download-bundle")
async def download_bundle(volume: str, item: list[str] = Query(...)):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for raw in item:
            spec = json.loads(raw)
            filepath = spec["filepath"]
            name = spec["name"]
            is_dir = not spec.get("leaf", True)
            client, response = await pve_client.open_download(volume, filepath, tar=False)
            try:
                content = await response.aread()
            finally:
                await response.aclose()
                await client.aclose()
            if is_dir:
                with zipfile.ZipFile(io.BytesIO(content)) as sub:
                    for info in sub.infolist():
                        bundle.writestr(f"{name}/{info.filename}", sub.read(info.filename))
            else:
                bundle.writestr(name, content)
    headers = {"content-disposition": _content_disposition("selected-files.zip")}
    return StreamingResponse(iter([buffer.getvalue()]), media_type="application/zip", headers=headers)
