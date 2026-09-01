"""Shared test setup.

`backend.config` reads env vars at import time and raises if PVE_HOST /
PVE_STORAGE are missing, so they have to be in the environment before any
`backend.*` module is imported. Setting them here (conftest is imported
before test collection) covers every test module.
"""

import os

os.environ.setdefault("PVE_HOST", "pve.test.local")
os.environ.setdefault("PVE_STORAGE", "pbs")
os.environ.setdefault("PVE_VERIFY_SSL", "false")
os.environ.setdefault("SESSION_IDLE_TIMEOUT_MINUTES", "30")

import time
from pathlib import Path

import pytest
from starlette.requests import Request


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def make_request(cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Request:
    """Minimal ASGI Request for exercising dependencies directly."""
    raw_headers: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_value = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_value.encode()))
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))
    return Request({"type": "http", "http_version": "1.1", "method": "GET", "headers": raw_headers})


@pytest.fixture
def session_data():
    from backend.auth import SessionData

    now = time.time()
    return SessionData(
        username="alice@pam",
        ticket="PVE:alice@pam:AAAA",
        csrf_token="CSRF123",
        ticket_issued_at=now,
        last_activity_at=now,
    )


@pytest.fixture(autouse=True)
def clear_sessions():
    """Keep the in-memory session store from leaking between tests."""
    from backend import auth

    auth._sessions.clear()
    yield
    auth._sessions.clear()


@pytest.fixture(autouse=True)
def clear_restore_jobs():
    """Keep the process-wide restore job registry from leaking between
    tests that exercise it via the FastAPI app (endpoint tests) -
    tests that construct their own RestoreJobManager() directly aren't
    affected either way."""
    from backend import restore_jobs

    restore_jobs.manager.clear()
    yield
    restore_jobs.manager.clear()


@pytest.fixture(autouse=True)
def clear_guest_agent_locks():
    """asyncio.Lock is bound to the event loop that created it, and tests
    widely reuse the same vmid ("133") under a fresh event loop per test
    - a lock left over from a previous test would raise "bound to a
    different event loop" if reacquired here. Same clear-between-tests
    convention as sessions/restore jobs above."""
    from backend import guest_agent_lock

    guest_agent_lock.clear()
    yield
    guest_agent_lock.clear()


@pytest.fixture
def api_base():
    from backend.config import settings

    return f"https://{settings.pve_host}:8006/api2/json"
