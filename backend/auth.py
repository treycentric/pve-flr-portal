"""PH.4 per-user auth: PVE ticket login replacing the old shared API tokens.

Pattern borrowed from PVE's own web UI (docs/plan.md §7.1): POST
credentials to /access/ticket once, then keep the session alive by
re-POSTing the *current* ticket in place of a password well before its
~2h expiry, rather than forcing a full re-login. Our backend does this
refresh server-side, transparently - the browser only ever holds our
own opaque session cookie, never the real PVE ticket.

Session store is a plain in-memory dict: this is a single-process
homelab tool (CLAUDE.md - no extra services), so a backend restart
logging everyone out is an acceptable tradeoff.
"""
import secrets
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

from .config import settings

_API_ROOT = f"https://{settings.pve_host}:8006/api2/json"

# Refresh a session's PVE ticket once it's this old, well inside the ~2h
# PVE ticket lifetime.
_TICKET_REFRESH_AGE_SECONDS = 90 * 60


@dataclass
class SessionData:
    username: str  # "user@realm"
    ticket: str
    csrf_token: str
    ticket_issued_at: float
    last_activity_at: float


_sessions: dict[str, SessionData] = {}


def pve_headers(session: SessionData) -> dict[str, str]:
    return {
        "Cookie": f"PVEAuthCookie={session.ticket}",
        "CSRFPreventionToken": session.csrf_token,
    }


async def list_realms() -> list[dict]:
    """Unauthenticated - PVE's own login page calls this the same way to
    populate its realm dropdown."""
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        resp = await client.get(f"{_API_ROOT}/access/domains")
        resp.raise_for_status()
        return sorted(resp.json()["data"], key=lambda d: d["realm"])


async def login(username: str, password: str) -> str:
    """Authenticate against PVE's ticket endpoint; returns our opaque session id."""
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        resp = await client.post(
            f"{_API_ROOT}/access/ticket",
            data={"username": username, "password": password},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    data = resp.json()["data"]
    now = time.time()
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = SessionData(
        username=data["username"],
        ticket=data["ticket"],
        csrf_token=data["CSRFPreventionToken"],
        ticket_issued_at=now,
        last_activity_at=now,
    )
    return session_id


async def _refresh_ticket(session: SessionData) -> None:
    async with httpx.AsyncClient(verify=settings.pve_verify_ssl, timeout=15.0) as client:
        resp = await client.post(
            f"{_API_ROOT}/access/ticket",
            data={"username": session.username, "password": session.ticket},
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    session.ticket = data["ticket"]
    session.csrf_token = data["CSRFPreventionToken"]
    session.ticket_issued_at = time.time()


async def ensure_fresh_ticket(session: SessionData) -> None:
    """Public wrapper around the same staleness check get_session() applies
    to interactive requests, exposed for callers that hold a SessionData
    outside the request/response cycle - namely PH.5's background restore
    jobs (docs/plan.md §7.5). A restore can run long past any single HTTP
    request, so a job needs to refresh its own ticket on this same policy
    rather than only ever refreshing on the next browser request (which
    may not come for a while, or ever, if the job was fire-and-forget)."""
    if time.time() - session.ticket_issued_at > _TICKET_REFRESH_AGE_SECONDS:
        await _refresh_ticket(session)


def logout(session_id: str) -> None:
    _sessions.pop(session_id, None)


async def get_session(request: Request) -> SessionData:
    session_id = request.cookies.get("session_id")
    session = _sessions.get(session_id) if session_id else None
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in")

    now = time.time()
    idle_limit = settings.session_idle_timeout_minutes * 60
    if now - session.last_activity_at > idle_limit:
        _sessions.pop(session_id, None)
        raise HTTPException(status_code=401, detail="Session expired (idle timeout)")

    session.last_activity_at = now
    await ensure_fresh_ticket(session)
    return session
