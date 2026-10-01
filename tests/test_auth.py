import dataclasses
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


@respx.mock
async def test_login_with_2fa_raises_tfa_required(monkeypatch):
    """Issue #15: PVE's own AccessControl.pm returns NeedTFA plus an
    opaque intermediate ticket (never a real session) rather than a
    401/200 login - confirmed against PVE's own source, not guessed."""
    respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "!tfa!opaque-challenge-blob",
                    "CSRFPreventionToken": "csrf-1",
                    "NeedTFA": 1,
                }
            },
        )
    )
    with pytest.raises(auth.TFARequired) as exc:
        await auth.login("alice@pam", "hunter2")
    assert exc.value.username == "alice@pam"
    assert exc.value.challenge == "!tfa!opaque-challenge-blob"
    assert auth._sessions == {}


@respx.mock
async def test_finish_tfa_login_totp_success_stores_session():
    route = respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:REALTICKET",
                    "CSRFPreventionToken": "csrf-2",
                }
            },
        )
    )
    session_id = await auth.finish_tfa_login("alice@pam", "123456", "!tfa!opaque-challenge-blob")
    assert session_id in auth._sessions
    assert auth._sessions[session_id].ticket == "PVE:alice@pam:REALTICKET"
    sent = route.calls.last.request.read().decode()
    # A live-reported real bug (2026-09-30): PVE's own web UI
    # (proxmox-widget-toolkit's TfaWindow.js) prefixes the response with
    # which method it's for - a bare code is rejected outright,
    # regardless of correctness. The colon is form-urlencoded as %3A.
    assert "password=totp%3A123456" in sent
    assert "tfa-challenge=" in sent
    assert "otp=" not in sent


@respx.mock
async def test_finish_tfa_login_recovery_key_success_stores_session():
    """A recovery key (xxxx-xxxx-xxxx-xxxx hex groups, never a bare
    6-8 digit number - per the same widget's own input validation) gets
    the 'recovery:' prefix instead of 'totp:'."""
    route = respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:REALTICKET",
                    "CSRFPreventionToken": "csrf-2",
                }
            },
        )
    )
    await auth.finish_tfa_login("alice@pam", "a1b2-c3d4-e5f6-0789", "!tfa!opaque-challenge-blob")
    sent = route.calls.last.request.read().decode()
    assert "password=recovery%3Aa1b2-c3d4-e5f6-0789" in sent


@respx.mock
async def test_finish_tfa_login_bad_code_raises_401():
    respx.post(f"{API}/access/ticket").mock(return_value=httpx.Response(401, json={"data": None}))
    with pytest.raises(HTTPException) as exc:
        await auth.finish_tfa_login("alice@pam", "000000", "!tfa!opaque-challenge-blob")
    assert exc.value.status_code == 401
    assert auth._sessions == {}


@respx.mock
async def test_oidc_auth_url_returns_the_identity_provider_url():
    route = respx.post(f"{API}/access/openid/auth-url").mock(
        return_value=httpx.Response(200, json={"data": "https://idp.example.com/authorize?state=abc"})
    )
    url = await auth.oidc_auth_url("keycloak", "https://portal.example.com/login/oidc/callback")
    assert url == "https://idp.example.com/authorize?state=abc"
    sent = route.calls.last.request.read().decode()
    assert "realm=keycloak" in sent
    assert "redirect-url=https" in sent


@respx.mock
async def test_oidc_auth_url_raises_401_on_pve_error():
    respx.post(f"{API}/access/openid/auth-url").mock(return_value=httpx.Response(400, json={"data": None}))
    with pytest.raises(HTTPException) as exc:
        await auth.oidc_auth_url("bogus", "https://portal.example.com/login/oidc/callback")
    assert exc.value.status_code == 401


@respx.mock
async def test_oidc_login_success_stores_session():
    respx.post(f"{API}/access/openid/login").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@keycloak",
                    "ticket": "PVE:alice@keycloak:TICKET",
                    "CSRFPreventionToken": "csrf-oidc",
                }
            },
        )
    )
    session_id = await auth.oidc_login("state1", "code1", "https://portal.example.com/login/oidc/callback")
    assert session_id in auth._sessions
    stored = auth._sessions[session_id]
    assert stored.username == "alice@keycloak"
    assert stored.ticket == "PVE:alice@keycloak:TICKET"
    assert stored.csrf_token == "csrf-oidc"


@respx.mock
async def test_oidc_login_raises_401_when_pve_rejects_the_exchange():
    respx.post(f"{API}/access/openid/login").mock(return_value=httpx.Response(401, json={"data": None}))
    with pytest.raises(HTTPException) as exc:
        await auth.oidc_login("bad-state", "bad-code", "https://portal.example.com/login/oidc/callback")
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


async def test_get_session_keepalive_does_not_extend_the_idle_clock(session_data):
    """A background poll validates the session but is not user activity
    (issue #27) - last_activity_at is left untouched."""
    before = time.time() - 600
    session_data.last_activity_at = before
    auth._sessions["sid"] = session_data
    out = await auth.get_session_keepalive(make_request(cookies={"session_id": "sid"}))
    assert out is session_data
    assert out.last_activity_at == before


async def test_get_session_keepalive_still_evicts_on_idle_timeout(session_data):
    session_data.last_activity_at = time.time() - (31 * 60)
    auth._sessions["sid"] = session_data
    with pytest.raises(HTTPException) as exc:
        await auth.get_session_keepalive(make_request(cookies={"session_id": "sid"}))
    assert exc.value.status_code == 401
    assert "sid" not in auth._sessions


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
async def test_ensure_fresh_ticket_refreshes_cap(session_data):
    """Issue #122: `cap` (used by is_job_admin) must be kept current on
    every ticket refresh, not just captured once at login - a role
    change on PVE's side should take effect on the next refresh."""
    respx.post(f"{API}/access/ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "username": "alice@pam",
                    "ticket": "PVE:alice@pam:FRESH3",
                    "CSRFPreventionToken": "csrf-fresh3",
                    "cap": {"dc": {"Sys.Audit": 1}},
                }
            },
        )
    )
    session_data.ticket_issued_at = time.time() - (auth._TICKET_REFRESH_AGE_SECONDS + 60)
    await auth.ensure_fresh_ticket(session_data)
    assert session_data.cap == {"dc": {"Sys.Audit": 1}}


@respx.mock
async def test_get_session_evicts_and_401s_when_ticket_refresh_rejected(session_data):
    """A stale session whose ticket PVE won't renew is expired, not a 500
    (issue #27) - get_session evicts it and raises 401 so the caller
    redirects to /login."""
    respx.post(f"{API}/access/ticket").mock(return_value=httpx.Response(401, json={"data": None}))
    session_data.ticket_issued_at = time.time() - (auth._TICKET_REFRESH_AGE_SECONDS + 60)
    auth._sessions["sid"] = session_data
    with pytest.raises(HTTPException) as exc:
        await auth.get_session(make_request(cookies={"session_id": "sid"}))
    assert exc.value.status_code == 401
    assert exc.value.detail != "Not logged in"
    assert "sid" not in auth._sessions


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


def test_is_job_admin_false_with_no_cap(session_data):
    assert session_data.cap == {}
    assert auth.is_job_admin(session_data) is False


def test_is_job_admin_false_when_privilege_elsewhere(session_data):
    # Granted on a VM/storage subtree, not at the bare root "/" - compute_api_permission
    # buckets that under its resource type (e.g. "vms"), never "dc".
    session_data.cap = {"vms": {"Sys.Audit": 1}}
    assert auth.is_job_admin(session_data) is False


def test_is_job_admin_true_with_dc_privilege(session_data):
    session_data.cap = {"dc": {"Sys.Audit": 1}}
    assert auth.is_job_admin(session_data) is True


def test_is_job_admin_respects_configured_privilege_name(session_data, monkeypatch):
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, job_admin_privilege="Sys.Modify"))
    session_data.cap = {"dc": {"Sys.Audit": 1}}
    assert auth.is_job_admin(session_data) is False
    session_data.cap = {"dc": {"Sys.Modify": 1}}
    assert auth.is_job_admin(session_data) is True
