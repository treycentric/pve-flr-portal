"""End-to-end-ish tests for the FastAPI routes.

The PVE client layer is stubbed out per-test (monkeypatched async fakes),
so nothing here touches the network. Auth is bypassed by overriding the
`get_session` dependency, except where the test is specifically about the
unauthenticated path.
"""

import io
import zipfile

import httpx
import pytest

main = pytest.importorskip("backend.main", reason="backend.main needs FastAPI")
from fastapi.testclient import TestClient

from backend import auth, pve_client

ARCHIVES = [
    {"volid": "pbs:backup/vm/133/2026-08-30T02:03:57Z", "ctime": 200, "size": 10, "verification": {"state": "ok"}},
    {"volid": "pbs:backup/vm/133/2026-08-29T02:03:57Z", "ctime": 100, "size": 9, "verification": {"state": "failed"}},
    {"volid": "pbs:backup/ct/104/2026-08-30T05:00:00Z", "ctime": 150, "size": 5, "verification": {}},
]


@pytest.fixture
def client(session_data, monkeypatch):
    async def fake_archives(session):
        return ARCHIVES

    async def fake_names(session):
        return {"133": "webserver"}

    monkeypatch.setattr(pve_client, "list_backup_archives", fake_archives)
    monkeypatch.setattr(pve_client, "list_guest_names", fake_names)

    main.app.dependency_overrides[auth.get_session] = lambda: session_data
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_index_lists_groups_and_defaults_to_first(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Groups sort by (type, vmid): ct:104 comes first and is selected by default.
    assert "2026-08-30T05:00:00Z" in body
    # The vm/133 group (and its resolved name) still ships in the task-picker JSON.
    assert "webserver" in body


def test_index_respects_task_query(client):
    resp = client.get("/", params={"task": "vm:133"})
    assert resp.status_code == 200
    body = resp.text
    # vm:133 has two snapshots; the newer one only, sorted first.
    assert "2026-08-30T02:03:57Z" in body
    assert "2026-08-29T02:03:57Z" in body


def test_index_renders_version_and_repo_link_in_about_box(client, project_root):
    from backend.version import REPO_URL

    resp = client.get("/")
    body = resp.text
    assert f"v{(project_root / 'VERSION').read_text().strip()}" in body
    assert REPO_URL in body


def test_index_requires_auth():
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_htmx_unauthorized_returns_hx_redirect():
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        resp = c.get("/api/browse", params={"volume": "v"}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/login"


def test_browse_renders_file_grid(client, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "etc", "leaf": False, "filepath": "L2V0Yw=="},
            {"text": "hosts", "leaf": True, "filepath": "L2V0Yy9ob3N0cw==", "size": 12, "mtime": 0},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/browse", params={"volume": "vol", "filepath": "/"})
    assert resp.status_code == 200
    assert "etc" in resp.text and "hosts" in resp.text


def test_browse_error_partial_on_pve_failure(client, monkeypatch):
    async def boom(session, volume, filepath="/"):
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
        )

    monkeypatch.setattr(pve_client, "list_path", boom)
    resp = client.get("/api/browse", params={"volume": "vol"})
    assert resp.status_code == 200
    assert "can't be browsed" in resp.text


def test_tree_lists_only_directories(client, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "etc", "leaf": False, "filepath": "a"},
            {"text": "file.txt", "leaf": True, "filepath": "b"},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/tree", params={"volume": "vol", "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert "etc" in resp.text
    assert "file.txt" not in resp.text


def test_restore_capabilities_rejects_unknown_guest_type(client):
    resp = client.get("/api/restore-capabilities", params={"type": "bogus", "vmid": "133"})
    assert resp.status_code == 400


def test_restore_capabilities_returns_capability_json(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        assert guest_type == "vm"
        assert vmid == "133"
        return guest_agent.RestoreCapabilities(
            agent_running=True,
            pve_version_ok=True,
            guest_os_family="linux",
            design_a=guest_agent.PathAvailability(True),
            design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted privilege"),
            verify_supported=False,
        )

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_running"] is True
    assert body["design_a"] == {"available": True, "reason": None}
    assert body["design_b"]["available"] is False
    assert "Unrestricted" in body["design_b"]["reason"]


def test_restore_capabilities_degrades_on_pve_error_instead_of_500(client, monkeypatch):
    from backend import guest_agent

    async def boom(session, guest_type, vmid):
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
        )

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", boom)
    resp = client.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["design_a"]["available"] is False
    assert body["design_b"]["available"] is False
    assert body["design_a"]["reason"]


def _available_caps(**overrides):
    from backend import guest_agent

    defaults = dict(
        agent_running=True,
        pve_version_ok=True,
        guest_os_family="windows",
        design_a=guest_agent.PathAvailability(True),
        design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted privilege"),
        verify_supported=False,
    )
    defaults.update(overrides)
    return guest_agent.RestoreCapabilities(**defaults)


def _restore_form(**overrides):
    defaults = dict(
        volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        filepath="L2V0Yy9ob3N0cw==",
        name="hosts",
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        snapshot_time="2026-08-30T14:48:06Z",
        dest_dir="C:\\Windows\\Temp",
        overwrite="true",
    )
    defaults.update(overrides)
    return defaults


def test_restore_submits_a_queued_job(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        # submit() launches this as a real asyncio task in the running
        # TestClient event loop - keep it inert so the test only asserts
        # on the synchronous "job was queued" response, not job completion.
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_content_only_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["destination"] == "C:\\Windows\\Temp\\hosts"
    assert restore_jobs.manager.get(body["id"]) is not None


def test_restore_rejects_unknown_guest_type(client):
    resp = client.post("/api/restore", data=_restore_form(guest_type="bogus"))
    assert resp.status_code == 400


def test_restore_requires_explicit_overwrite_confirmation(client):
    resp = client.post("/api/restore", data=_restore_form(overwrite="false"))
    assert resp.status_code == 400


def test_restore_blocked_when_capability_unavailable(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_a=guest_agent.PathAvailability(False, "missing VM.GuestAgent.FileWrite"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.post("/api/restore", data=_restore_form())
    assert resp.status_code == 403
    assert "FileWrite" in resp.json()["detail"]


def test_restore_uses_posix_separator_for_non_windows_guest(client, monkeypatch):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_content_only_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form(dest_dir="/etc", name="hosts"))
    assert resp.status_code == 200
    assert resp.json()["destination"] == "/etc/hosts"


def test_restore_requires_auth():
    with TestClient(main.app) as c:
        resp = c.post("/api/restore", data=_restore_form(), follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_capabilities_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_browse_rejects_unknown_guest_type(client):
    resp = client.get("/api/restore-browse", params={"type": "bogus", "vmid": "133"})
    assert resp.status_code == 400


def test_restore_browse_blocked_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        reason = "missing VM.GuestAgent.Unrestricted privilege"
        return _available_caps(design_b=guest_agent.PathAvailability(False, reason))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 403
    assert "Unrestricted" in resp.json()["detail"]


def test_restore_browse_returns_listing(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True), guest_os_family="linux")

    async def fake_list(session, guest_type, vmid, guest_os_family, path):
        assert guest_os_family == "linux"
        assert path == "/etc"
        return {"path": "/etc", "parent": "/", "separator": "/", "entries": [{"name": "nginx", "path": "/etc/nginx"}]}

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133", "path": "/etc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == [{"name": "nginx", "path": "/etc/nginx"}]


def test_restore_browse_unsafe_path_returns_400(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_list(session, guest_type, vmid, guest_os_family, path):
        raise guest_browse.UnsafePathError("nope")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133", "path": "/tmp/;rm"})
    assert resp.status_code == 400


def test_restore_browse_listing_error_returns_502(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_list(session, guest_type, vmid, guest_os_family, path):
        raise guest_browse.ListingError("No such file or directory")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 502


def test_restore_browse_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-browse", params={"type": "vm", "vmid": "133"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_download_streams_with_content_disposition(client, monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "application/octet-stream"}

        async def aiter_bytes(self):
            yield b"hello "
            yield b"world"

        async def aclose(self):
            pass

    class FakeClient:
        async def aclose(self):
            pass

    async def fake_open(session, volume, filepath, tar=False):
        return FakeClient(), FakeResponse()

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    resp = client.get("/api/download", params={"volume": "v", "filepath": "f", "name": "out.txt"})
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert 'filename="out.txt"' in resp.headers["content-disposition"]


def test_download_bundle_rejects_unknown_format(client):
    resp = client.get("/api/download-bundle", params={"volume": "v", "item": ["{}"], "format": "rar"})
    assert resp.status_code == 400


def test_download_bundle_builds_zip(client, monkeypatch):
    async def fake_open(session, volume, filepath, tar=False):
        class FakeResponse:
            async def aread(self):
                return b"file-content"

            async def aclose(self):
                pass

        class FakeClient:
            async def aclose(self):
                pass

        return FakeClient(), FakeResponse()

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    item = '{"filepath": "abc", "name": "a.txt", "leaf": true}'
    resp = client.get(
        "/api/download-bundle",
        params={"volume": "v", "item": [item], "name": "bundle", "format": "zip"},
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["a.txt"]
        assert zf.read("a.txt") == b"file-content"


def test_login_page_renders_even_if_realms_fail(monkeypatch):
    async def boom():
        raise RuntimeError("pve down")

    monkeypatch.setattr(auth, "list_realms", boom)
    with TestClient(main.app) as c:
        resp = c.get("/login")
    assert resp.status_code == 200
    assert "Log in" in resp.text


def test_login_submit_invalid_credentials(monkeypatch):
    from fastapi import HTTPException

    async def bad_login(username, password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    async def realms():
        return []

    monkeypatch.setattr(auth, "login", bad_login)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "x", "realm": "pam", "password": "y"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text


def test_login_submit_success_sets_cookie(monkeypatch):
    async def ok_login(username, password):
        assert username == "x@pam"
        return "session-abc"

    monkeypatch.setattr(auth, "login", ok_login)
    with TestClient(main.app) as c:
        resp = c.post(
            "/login",
            data={"username": "x", "realm": "pam", "password": "y"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session_id=session-abc" in resp.headers["set-cookie"]


def test_logout_clears_cookie_and_session(session_data):
    auth._sessions["session-xyz"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "session-xyz")
        resp = c.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "session-xyz" not in auth._sessions
