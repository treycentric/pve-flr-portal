import httpx
import pytest
import respx

from backend import pve_client

BASE = pve_client._BASE
API = pve_client._API_ROOT


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
