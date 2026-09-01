import httpx
import pytest
import respx

from backend import pve_client

BASE = pve_client._BASE
API = pve_client._API_ROOT


def test_api_node_type_maps_app_internal_names():
    assert pve_client.api_node_type("vm") == "qemu"
    assert pve_client.api_node_type("ct") == "lxc"


def test_api_node_type_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown guest type"):
        pve_client.api_node_type("qemu")  # already-translated value isn't valid input


def test_check_path_safe_rejects_shell_metacharacters():
    for bad in ['"', "'", "`", ";", "&", "|", "$", "<", ">", "\n", "\r"]:
        with pytest.raises(pve_client.UnsafePathError):
            pve_client.check_path_safe(f"/tmp/evil{bad}here")


def test_check_path_safe_allows_ordinary_paths():
    pve_client.check_path_safe("/etc/nginx")
    pve_client.check_path_safe("C:\\Users\\bob\\Documents")


@respx.mock
async def test_run_guest_exec_returns_exit_out_err(session_data):
    respx.post(f"{API}/nodes/localhost/qemu/133/agent/exec").mock(
        return_value=httpx.Response(200, json={"data": {"pid": 1}})
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/exec-status").mock(
        return_value=httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "hi", "err-data": ""}})
    )
    exitcode, out, err = await pve_client.run_guest_exec(session_data, "vm", "133", ["echo", "hi"])
    assert (exitcode, out, err) == (0, "hi", "")


@respx.mock
async def test_run_guest_exec_times_out(session_data, monkeypatch):
    monkeypatch.setattr(pve_client.asyncio, "sleep", lambda *_a, **_kw: _noop())
    respx.post(f"{API}/nodes/localhost/qemu/133/agent/exec").mock(
        return_value=httpx.Response(200, json={"data": {"pid": 1}})
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/exec-status").mock(
        return_value=httpx.Response(200, json={"data": {"exited": 0}})
    )
    with pytest.raises(pve_client.GuestExecTimeout):
        await pve_client.run_guest_exec(session_data, "vm", "133", ["echo", "hi"])


async def _noop():
    return None


@respx.mock
async def test_list_guest_names_drops_entries_without_name(session_data):
    respx.get(f"{API}/cluster/resources").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"vmid": 100, "name": "web"},
                    {"vmid": 101},  # no name -> skipped
                    {"vmid": 102, "name": ""},  # empty name -> skipped
                ]
            },
        )
    )
    names = await pve_client.list_guest_names(session_data)
    assert names == {"100": "web"}


@respx.mock
async def test_list_guest_names_sends_auth_headers(session_data):
    route = respx.get(f"{API}/cluster/resources").mock(return_value=httpx.Response(200, json={"data": []}))
    await pve_client.list_guest_names(session_data)
    req = route.calls.last.request
    assert req.headers["Cookie"] == f"PVEAuthCookie={session_data.ticket}"
    assert req.headers["CSRFPreventionToken"] == session_data.csrf_token
    assert req.url.params["type"] == "vm"


@respx.mock
async def test_list_backup_archives_returns_data(session_data):
    payload = [{"vmid": 133, "volid": "pbs:backup/vm/133/2026-08-30T02:03:57Z", "ctime": 1}]
    route = respx.get(f"{API}/nodes/localhost/storage/pbs/content").mock(
        return_value=httpx.Response(200, json={"data": payload})
    )
    out = await pve_client.list_backup_archives(session_data)
    assert out == payload
    assert route.calls.last.request.url.params["content"] == "backup"


@respx.mock
async def test_list_path_passes_volume_and_filepath(session_data):
    route = respx.get(f"{BASE}/list").mock(
        return_value=httpx.Response(200, json={"data": [{"text": "etc", "leaf": False}]})
    )
    out = await pve_client.list_path(session_data, "pbs:backup/vm/133/x", "/")
    assert out == [{"text": "etc", "leaf": False}]
    params = route.calls.last.request.url.params
    assert params["volume"] == "pbs:backup/vm/133/x"
    assert params["filepath"] == "/"


@respx.mock
async def test_list_path_raises_on_http_error(session_data):
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        await pve_client.list_path(session_data, "vol", "/")


@respx.mock
async def test_write_guest_file_posts_file_and_content(session_data):
    route = respx.post(f"{API}/nodes/localhost/qemu/133/agent/file-write").mock(
        return_value=httpx.Response(200, json={"data": None})
    )
    await pve_client.write_guest_file(session_data, "vm", "133", "C:\\Windows\\Temp\\x.txt", "hello")
    req = route.calls.last.request
    assert req.headers["Cookie"] == f"PVEAuthCookie={session_data.ticket}"
    body = req.content.decode()
    assert "file=C%3A%5CWindows%5CTemp%5Cx.txt" in body or "hello" in body


@respx.mock
async def test_write_guest_file_raises_on_http_error(session_data, monkeypatch):
    from backend import guest_agent_lock

    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", lambda *_a, **_kw: _noop())
    respx.post(f"{API}/nodes/localhost/qemu/133/agent/file-write").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        await pve_client.write_guest_file(session_data, "vm", "133", "/etc/x", "hello")


async def _noop():
    return None


@respx.mock
async def test_write_guest_file_retries_a_definite_error_then_succeeds(session_data, monkeypatch):
    from backend import guest_agent_lock

    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", lambda *_a, **_kw: _noop())
    route = respx.post(f"{API}/nodes/localhost/qemu/133/agent/file-write").mock(
        side_effect=[httpx.Response(500, text="busy"), httpx.Response(200, json={"data": None})]
    )
    await pve_client.write_guest_file(session_data, "vm", "133", "/etc/x", "hello")
    assert route.call_count == 2


@respx.mock
async def test_write_guest_file_does_not_retry_on_timeout(session_data):
    route = respx.post(f"{API}/nodes/localhost/qemu/133/agent/file-write").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    with pytest.raises(httpx.TimeoutException):
        await pve_client.write_guest_file(session_data, "vm", "133", "/etc/x", "hello")
    assert route.call_count == 1


@respx.mock
async def test_open_download_streams_and_sets_tar_param(session_data):
    route = respx.get(f"{BASE}/download").mock(
        return_value=httpx.Response(200, content=b"file-bytes", headers={"content-type": "application/x-tar"})
    )
    client, response = await pve_client.open_download(session_data, "vol", "Zm9v", tar=True)
    try:
        assert await response.aread() == b"file-bytes"
        assert route.calls.last.request.url.params["tar"] == "1"
    finally:
        await response.aclose()
        await client.aclose()


@respx.mock
async def test_open_download_error_response_is_raised_and_cleaned_up(session_data):
    respx.get(f"{BASE}/download").mock(return_value=httpx.Response(404, text="missing"))
    with pytest.raises(httpx.HTTPStatusError):
        await pve_client.open_download(session_data, "vol", "bad", tar=False)
