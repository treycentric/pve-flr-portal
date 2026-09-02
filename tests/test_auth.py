import time

import httpx
import pytest
import respx
from conftest import make_request
from fastapi import HTTPException

from backend import auth

API = auth._API_ROOT


@respx.mock
async def test_list_realms_sorted():
    respx.get(f"{API}/access/domains").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"realm": "pve", "comment": "PVE"},
                    {"realm": "pam", "comment": "Linux PAM"},
                ]
            },
        )
    )
    realms = await auth.list_realms()
    assert [r["realm"] for r in realms] == ["pam", "pve"]


@respx.mock
async def test_list_realms_retries_then_succeeds(monkeypatch):
    """A transient failure of /access/domains is retried, not surfaced
    as an empty dropdown (issue #31)."""
    monkeypatch.setattr(auth, "_LIST_REALMS_BACKOFF_SECONDS", 0)
    route = respx.get(f"{API}/access/domains").mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"data": [{"realm": "pve"}, {"realm": "pam"}]}),
        ]
    )
    realms = await auth.list_realms()
    assert [r["realm"] for r in realms] == ["pam", "pve"]
    assert route.call_count == 2


@respx.mock
async def test_list_realms_falls_back_to_pam_pve_when_pve_unreachable(monkeypatch, caplog):
    """Retries exhausted -> the two realms PVE always ships, never an
    empty list (issue #31)."""
    monkeypatch.setattr(auth, "_LIST_REALMS_BACKOFF_SECONDS", 0)
    route = respx.get(f"{API}/access/domains").mock(side_effect=httpx.ConnectError("down"))
    with caplog.at_level("WARNING"):
        realms = await auth.list_realms()
    assert [r["realm"] for r in realms] == ["pam", "pve"]
    assert route.call_count == auth._LIST_REALMS_ATTEMPTS
    assert "fallback" in caplog.text.lower()


@respx.mock
async def test_login_success_stores_session():
    respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:TICKET",
                    "CSRFPreventionToken": "csrf-1",
                }
            },
        )
    )
    session_id = await auth.login("alice@pam", "hunter2")
    assert session_id in auth._sessions
    stored = auth._sessions[session_id]
    assert stored.username == "alice@pam"
    assert stored.ticket == "PVE:alice@pam:TICKET"
    assert stored.csrf_token == "csrf-1"


@respx.mock
async def test_login_bad_credentials_raises_401():
    respx.post(f"{API}/access/ticket").mock(return_value=httpx.Response(401, json={"data": None}))
    with pytest.raises(HTTPException) as exc:
        await auth.login("alice@pam", "wrong")
    assert exc.value.status_code == 401
    assert auth._sessions == {}


def test_pve_headers_shape(session_data):
    headers = auth.pve_headers(session_data)
    assert headers["Cookie"] == f"PVEAuthCookie={session_data.ticket}"
    assert headers["CSRFPreventionToken"] == session_data.csrf_token


def test_logout_removes_session(session_data):
    auth._sessions["sid"] = session_data
    auth.logout("sid")
    assert "sid" not in auth._sessions
    auth.logout("sid")  # no error on second call


async def test_get_session_missing_cookie_raises():
    with pytest.raises(HTTPException) as exc:
        await auth.get_session(make_request())
    assert exc.value.status_code == 401


async def test_get_session_unknown_id_raises():
    with pytest.raises(HTTPException) as exc:
        await auth.get_session(make_request(cookies={"session_id": "nope"}))
    assert exc.value.status_code == 401


async def test_get_session_idle_timeout_evicts(session_data):
    session_data.last_activity_at = time.time() - (31 * 60)
    auth._sessions["sid"] = session_data
    with pytest.raises(HTTPException) as exc:
        await auth.get_session(make_request(cookies={"session_id": "sid"}))
    assert exc.value.status_code == 401
    assert "sid" not in auth._sessions


async def test_get_session_refreshes_activity(session_data):
    session_data.last_activity_at = time.time() - 60
    auth._sessions["sid"] = session_data
    out = await auth.get_session(make_request(cookies={"session_id": "sid"}))
    assert out is session_data
    assert time.time() - out.last_activity_at < 1


@respx.mock
async def test_ensure_fresh_ticket_refreshes_when_stale(session_data):
    """The standalone helper background restore jobs use (docs/plan.md
    §7.5) - same policy as get_session()'s inline check, callable without
    a request/session-store round trip."""
    route = respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:FRESH2",
                    "CSRFPreventionToken": "csrf-fresh2",
                }
            },
        )
    )
    session_data.ticket_issued_at = time.time() - (auth._TICKET_REFRESH_AGE_SECONDS + 60)
    await auth.ensure_fresh_ticket(session_data)
    assert route.called
    assert session_data.ticket == "PVE:alice@pam:FRESH2"


@respx.mock
async def test_ensure_fresh_ticket_no_ops_when_fresh(session_data):
    session_data.ticket_issued_at = time.time()
    original_ticket = session_data.ticket
    await auth.ensure_fresh_ticket(session_data)
    assert session_data.ticket == original_ticket  # no HTTP call was even mocked/needed


@respx.mock
async def test_get_session_refreshes_stale_ticket(session_data):
    route = respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:FRESH",
                    "CSRFPreventionToken": "csrf-fresh",
                }
            },
        )
    )
    session_data.ticket_issued_at = time.time() - (auth._TICKET_REFRESH_AGE_SECONDS + 60)
    auth._sessions["sid"] = session_data
    await auth.get_session(make_request(cookies={"session_id": "sid"}))
    assert route.called
    assert session_data.ticket == "PVE:alice@pam:FRESH"
    assert session_data.csrf_token == "csrf-fresh"
