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

from . import auth, guest_agent, guest_browse, pve_client, restore_jobs, restore_runner
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
            "guest_json": json.dumps({"type": guest_type, "vmid": guest_vmid, "label": guest_label}),
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
        entry["item_json"] = json.dumps(
            {"filepath": entry["filepath"], "leaf": leaf, "name": text, "mtime": entry["mtime"]}
        )
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
    implies, not something that should ever crash the button. `type` is
    the app-internal "vm"/"ct" value (matching the task picker/groups
    elsewhere) - guest_agent.py translates it to PVE's "qemu"/"lxc" API
    node segment."""
    if type not in ("vm", "ct"):
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


@app.post("/api/restore")
async def restore(
    volume: str = Form(...),
    filepath: str = Form(...),
    name: str = Form(...),
    guest_type: str = Form(...),
    vmid: str = Form(...),
    guest_label: str = Form(...),
    snapshot_time: str = Form(...),
    dest_dir: str = Form(...),
    overwrite: bool = Form(False),
    restore_metadata: bool = Form(False),
    verify: bool = Form(False),
    source_mtime: int | None = Form(None),
    session: SessionData = Depends(auth.get_session),
):
    """PH.5 restore (docs/plan.md §7.5): submits a background job and
    returns immediately - the actual write (plus, when needed, multi-
    chunk assembly / metadata restore / verify) runs out-of-band
    (restore_runner.run_restore), independent of this request's
    lifetime; the job itself fails clearly (not this endpoint) if
    something later turns out to need guest-exec but the account lacks
    it. `guest_type` is the app-internal "vm"/"ct" value - see
    restore_capabilities() above."""
    if guest_type not in ("vm", "ct"):
        raise HTTPException(status_code=400, detail=f"Unknown guest type: {guest_type}")
    if not overwrite:
        raise HTTPException(status_code=400, detail="Restore must be explicitly confirmed to overwrite the destination")

    # Re-checked server-side regardless of what the UI already showed -
    # the capability response is a UI convenience, never trusted for the
    # actual write (docs/plan.md §7.5).
    caps = await guest_agent.get_restore_capabilities(session, guest_type, vmid)
    if not caps.design_a.available:
        raise HTTPException(status_code=403, detail=caps.design_a.reason or "Restore is not available for this guest")
    if (restore_metadata or verify) and not caps.design_b.available:
        raise HTTPException(
            status_code=403,
            detail=caps.design_b.reason or "Restoring metadata/verifying needs VM.GuestAgent.Unrestricted",
        )

    sep = "\\" if caps.guest_os_family == "windows" else "/"
    destination = dest_dir.rstrip("\\/") + sep + name

    job = restore_jobs.manager.create(
        session=session,
        guest_type=guest_type,
        vmid=vmid,
        guest_label=guest_label,
        task_name=f"Restore {name} → {destination}",
        snapshot_time=snapshot_time,
        source_volume=volume,
        source_filepath=filepath,
        source=name,
        destination=destination,
        restore_metadata=restore_metadata,
        verify=verify,
        source_mtime=source_mtime,
    )
    restore_jobs.manager.submit(job, lambda j: restore_runner.run_restore(j, restore_jobs.manager))
    return JSONResponse(job.to_dict())


@app.get("/api/restore-jobs")
async def restore_jobs_list(session: SessionData = Depends(auth.get_session)):
    """PH.5 (docs/plan.md §7.5): the running-jobs indicator's data source,
    polled from the top bar. Jobs are visible to any logged-in user, not
    scoped per-requester - a single-admin homelab tool with one shared
    task list, same as the rest of this design."""
    return JSONResponse([job.to_dict() for job in restore_jobs.manager.list_jobs()])


@app.get("/api/restore-jobs/{job_id}")
async def restore_jobs_detail(job_id: str, session: SessionData = Depends(auth.get_session)):
    """A single job's full detail, including its step-by-step log
    (restore_runner.py's job.log() calls) - kept out of the list endpoint
    above to keep that one light on every poll; fetched on demand when a
    user opens the log viewer for one specific job."""
    job = restore_jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such restore job")
    return JSONResponse(job.to_detail_dict())


@app.post("/api/restore-jobs/{job_id}/cancel")
async def restore_jobs_cancel(job_id: str, session: SessionData = Depends(auth.get_session)):
    job = restore_jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such restore job")
    restore_jobs.manager.cancel(job_id)
    return JSONResponse(job.to_dict())


@app.get("/api/restore-browse")
async def restore_browse(
    type: str,
    vmid: str,
    path: str = "",
    session: SessionData = Depends(auth.get_session),
):
    """PH.5 (docs/plan.md §7.5): lists subdirectories inside the guest via
    guest-exec, so the restore destination can be browsed rather than
    typed blind. Needs VM.GuestAgent.Unrestricted - there's no dedicated
    QGA directory-listing command - so this is gated on design_b, same as
    metadata restore/verify, and re-checked here regardless of what the
    capabilities response already showed."""
    if type not in ("vm", "ct"):
        raise HTTPException(status_code=400, detail=f"Unknown guest type: {type}")
    caps = await guest_agent.get_restore_capabilities(session, type, vmid)
    if not caps.design_b.available:
        raise HTTPException(
            status_code=403, detail=caps.design_b.reason or "Browsing this guest's filesystem is not available"
        )
    try:
        result = await guest_browse.list_directories(session, type, vmid, caps.guest_os_family, path or None)
    except guest_browse.UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (httpx.HTTPStatusError, guest_browse.ListingError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return JSONResponse(result)


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
