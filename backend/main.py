import dataclasses
import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
import zstandard
from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from . import auth, guest_agent, pve_client
from .auth import SessionData
from .version import REPO_URL, __version__

app = FastAPI(title="pve-flr-portal")

_TEMPLATES_DIR = "backend/templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["fromtimestamp"] = lambda ts: (
    datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if ts is not None else ""
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


@app.exception_handler(HTTPException)
async def unauthorized_handler(request: Request, exc: HTTPException):
    """401s from the auth.get_session dependency become a redirect to
    /login instead of a bare JSON error - htmx requests get an
    HX-Redirect header (a plain 302 would just have htmx swap the login
    page's HTML into whatever partial target was requested)."""
    if exc.status_code == 401:
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/login")
async def login_page(request: Request):
    # Broad except deliberate: the login page must always render even if
    # PVE itself is unreachable (connection error, DNS failure, etc, not
    # just a non-2xx response) - it just falls back to an empty realm
    # dropdown rather than a 500.
    try:
        realms = await auth.list_realms()
    except Exception:
        realms = []
    return templates.TemplateResponse(request, "login.html", {"error": None, "realms": realms})


@app.post("/login")
async def login_submit(
    request: Request, username: str = Form(...), realm: str = Form(...), password: str = Form(...)
):
    full_username = username if "@" in username else f"{username}@{realm}"
    try:
        session_id = await auth.login(full_username, password)
    except HTTPException:
        try:
            realms = await auth.list_realms()
        except Exception:
            realms = []
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password", "realms": realms},
            status_code=401,
        )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=60 * 60 * 24,
    )
    return response


@app.get("/logout")
async def logout_route(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        auth.logout(session_id)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_id")
    return response


def _parse_volid(volid: str) -> tuple[str, str, str]:
    """"pbs:backup/vm/133/2026-08-30T02:03:57Z" -> ("vm", "133", "2026-08-30T02:03:57Z")."""
    _, rest = volid.split(":", 1)
    _, guest_type, vmid, iso = rest.split("/", 3)
    return guest_type, vmid, iso


@app.get("/")
async def index(request: Request, task: str | None = None, session: SessionData = Depends(auth.get_session)):
    archives = await pve_client.list_backup_archives(session)
    try:
        guest_names = await pve_client.list_guest_names(session)
    except httpx.HTTPStatusError:
        guest_names = {}

    parsed = []
    for a in archives:
        guest_type, vmid, iso = _parse_volid(a["volid"])
        verification = a.get("verification") or {}
        parsed.append(
            {
                "type": guest_type,
                "vmid": vmid,
                "volume": a["volid"],
                "time": iso,
                "ctime": a.get("ctime", 0),
                "size": a.get("size"),
                "verified": verification.get("state") == "ok",
            }
        )

    groups_map: dict[tuple[str, str], dict] = {}
    for p in parsed:
        key = (p["type"], p["vmid"])
        if key not in groups_map or p["ctime"] > groups_map[key]["last_backup"]:
            groups_map[key] = {
                "type": p["type"],
                "vmid": p["vmid"],
                "last_backup": p["ctime"],
                "name": guest_names.get(p["vmid"]),
            }
    groups = sorted(groups_map.values(), key=lambda g: (g["type"], g["vmid"]))

    if task and ":" in task and any(f"{g['type']}:{g['vmid']}" == task for g in groups):
        guest_type, guest_vmid = task.split(":", 1)
    elif groups:
        guest_type, guest_vmid = groups[0]["type"], groups[0]["vmid"]
    else:
        guest_type, guest_vmid = None, None

    snapshots = []
    if guest_vmid:
        for p in parsed:
            if p["type"] == guest_type and p["vmid"] == guest_vmid:
                snapshots.append(
                    {"volume": p["volume"], "time": p["time"], "size": p["size"], "verified": p["verified"]}
                )
        snapshots.sort(key=lambda s: s["time"], reverse=True)

    guest_name = groups_map.get((guest_type, guest_vmid), {}).get("name") if guest_vmid else None
    if guest_vmid:
        guest_label = f"{guest_name} ({guest_vmid})" if guest_name else f"{guest_type.upper()} {guest_vmid}"
    else:
        guest_label = None

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "snapshots": snapshots,
            "snapshots_json": json.dumps(snapshots),
            "guest_vmid": guest_vmid,
            "guest_type": guest_type,
            "guest_label": guest_label,
            "groups_json": json.dumps(groups),
            "current_identity": session.username,
            "app_version": __version__,
            "repo_url": REPO_URL,
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
async def browse(request: Request, volume: str, filepath: str = "/", session: SessionData = Depends(auth.get_session)):
    try:
        entries = await pve_client.list_path(session, volume, filepath)
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


def _content_disposition(filename: str) -> str:
    safe = filename.replace('"', "'").replace("\r", "").replace("\n", "")
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


@app.get("/api/tree")
async def tree(
    request: Request,
    volume: str,
    filepath: str = "/",
    crumbs: str = "[]",
    session: SessionData = Depends(auth.get_session),
):
    try:
        entries = await pve_client.list_path(session, volume, filepath)
    except httpx.HTTPStatusError:
        entries = []
    at_root = filepath == "/"
    parent_crumbs = json.loads(crumbs)
    nodes = []
    for entry in entries:
        if bool(entry.get("leaf", True)):
            continue
        text = entry.get("text", "")
        child_crumbs = [*parent_crumbs, {"label": text, "filepath": entry["filepath"]}]
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


@app.get("/api/restore-capabilities")
async def restore_capabilities(
    type: str,
    vmid: str,
    session: SessionData = Depends(auth.get_session),
):
    """PH.5 (docs/plan.md §7.5): what push-to-guest restore this specific
    user can actually use on this specific guest, right now. A caller
    lacking even VM.Audit on the guest (so /config itself 403s) degrades
    to "nothing available" rather than a 500 - restore is opt-in extra
    access on top of the browse permission every visible guest already
    implies, not something that should ever crash the button."""
    if type not in ("qemu", "lxc"):
        raise HTTPException(status_code=400, detail=f"Unknown guest type: {type}")
    try:
        caps = await guest_agent.get_restore_capabilities(session, type, vmid)
    except httpx.HTTPStatusError:
        reason = "could not read this guest's configuration/permissions"
        unavailable = guest_agent.PathAvailability(False, reason)
        caps = guest_agent.RestoreCapabilities(
            agent_running=False,
            pve_version_ok=False,
            guest_os_family=None,
            design_a=unavailable,
            design_b=unavailable,
            verify_supported=False,
        )
    return JSONResponse(dataclasses.asdict(caps))


@app.get("/api/download")
async def download(
    volume: str,
    filepath: str,
    tar: bool = False,
    name: str | None = None,
    session: SessionData = Depends(auth.get_session),
):
    client, response = await pve_client.open_download(session, volume, filepath, tar)

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


_ARCHIVE_FORMATS = {
    "zip": ("zip", "application/zip"),
    "targz": ("tar.gz", "application/gzip"),
    "tarzst": ("tar.zst", "application/zstd"),
}


@app.get("/api/download-bundle")
async def download_bundle(
    volume: str,
    item: list[str] = Query(...),
    name: str = "download",
    format: str = "zip",
    session: SessionData = Depends(auth.get_session),
):
    if format not in _ARCHIVE_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unknown archive format: {format}")
    extension, media_type = _ARCHIVE_FORMATS[format]

    entries: list[tuple[str, bytes]] = []
    for raw in item:
        spec = json.loads(raw)
        filepath = spec["filepath"]
        item_name = spec["name"]
        is_dir = not spec.get("leaf", True)
        client, response = await pve_client.open_download(session, volume, filepath, tar=False)
        try:
            content = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        if is_dir:
            with zipfile.ZipFile(io.BytesIO(content)) as sub:
                for info in sub.infolist():
                    entries.append((f"{item_name}/{info.filename}", sub.read(info.filename)))
        else:
            entries.append((item_name, content))

    buffer = io.BytesIO()
    if format == "zip":
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for arcname, content in entries:
                bundle.writestr(arcname, content)
    else:
        # .tar.gz uses tarfile's built-in gzip support; .tar.zst uses the
        # zstandard package (not stdlib compression.zstd, which is
        # 3.14+ only and won't exist on a normal deployment's Python).
        tar_mode = "w:gz" if format == "targz" else "w|"
        outer = zstandard.ZstdCompressor().stream_writer(buffer, closefd=False) if format == "tarzst" else buffer
        try:
            with tarfile.open(fileobj=outer, mode=tar_mode) as bundle:
                for arcname, content in entries:
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(content)
                    bundle.addfile(info, io.BytesIO(content))
        finally:
            if outer is not buffer:
                outer.close()

    headers = {"content-disposition": _content_disposition(f"{name}.{extension}")}
    return StreamingResponse(iter([buffer.getvalue()]), media_type=media_type, headers=headers)
