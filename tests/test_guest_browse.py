import httpx
import pytest
import respx

from backend import guest_browse
from backend.guest_browse import UnsafePathError, list_directories

API = guest_browse._API_ROOT


def _exec_route(vmid="133"):
    return respx.post(f"{API}/nodes/localhost/qemu/{vmid}/agent/exec")


def _status_route(vmid="133"):
    return respx.get(f"{API}/nodes/localhost/qemu/{vmid}/agent/exec-status")


def test_check_path_safe_rejects_shell_metacharacters():
    for bad in ['"', "'", "`", ";", "&", "|", "$", "<", ">", "\n", "\r"]:
        with pytest.raises(UnsafePathError):
            guest_browse._check_path_safe(f"/tmp/evil{bad}here")


def test_check_path_safe_allows_ordinary_paths():
    guest_browse._check_path_safe("/etc/nginx")
    guest_browse._check_path_safe("C:\\Users\\bob\\Documents")


def test_windows_parent_of_subfolder():
    assert guest_browse._windows_parent("C:\\Users\\bob") == "C:\\Users\\"


def test_windows_parent_of_drive_root_is_none():
    assert guest_browse._windows_parent("C:\\") is None


def test_posix_parent_of_root_is_none():
    assert guest_browse._posix_parent("/") is None


def test_posix_parent_of_subfolder():
    assert guest_browse._posix_parent("/etc/nginx") == "/etc"


@respx.mock
async def test_list_directories_rejects_unsafe_path(session_data):
    with pytest.raises(UnsafePathError):
        await list_directories(session_data, "vm", "133", "linux", "/tmp/;rm -rf /")


@respx.mock
async def test_list_directories_linux_root(session_data):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 42}}))
    _status_route().mock(
        return_value=httpx.Response(
            200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "/etc\n/var\n/home\n"}}
        )
    )
    result = await list_directories(session_data, "vm", "133", "linux", None)
    assert result["path"] == "/"
    assert result["parent"] is None
    assert result["separator"] == "/"
    assert result["entries"] == [
        {"name": "etc", "path": "/etc"},
        {"name": "var", "path": "/var"},
        {"name": "home", "path": "/home"},
    ]


@respx.mock
async def test_list_directories_linux_subfolder_has_parent(session_data):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 1}}))
    _status_route().mock(
        return_value=httpx.Response(
            200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "/etc/nginx/sites-enabled\n"}}
        )
    )
    result = await list_directories(session_data, "vm", "133", "linux", "/etc/nginx")
    assert result["path"] == "/etc/nginx"
    assert result["parent"] == "/etc"


@respx.mock
async def test_list_directories_windows_drive_list(session_data):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 7}}))
    _status_route().mock(
        return_value=httpx.Response(
            200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "Caption\r\nC:\r\nD:\r\n\r\n"}}
        )
    )
    result = await list_directories(session_data, "vm", "133", "windows", None)
    assert result["path"] is None
    assert result["parent"] is None
    assert result["separator"] == "\\"
    assert result["entries"] == [{"name": "C:", "path": "C:\\"}, {"name": "D:", "path": "D:\\"}]


@respx.mock
async def test_list_directories_windows_subfolder(session_data):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 3}}))
    _status_route().mock(
        return_value=httpx.Response(
            200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "Temp\r\nSystem32\r\n"}}
        )
    )
    result = await list_directories(session_data, "vm", "133", "windows", "C:\\Windows")
    assert result["path"] == "C:\\Windows"
    assert result["parent"] == "C:\\"
    assert result["entries"] == [
        {"name": "Temp", "path": "C:\\Windows\\Temp"},
        {"name": "System32", "path": "C:\\Windows\\System32"},
    ]


@respx.mock
async def test_list_directories_nonzero_exit_raises_listing_error(session_data):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 9}}))
    _status_route().mock(
        return_value=httpx.Response(
            200, json={"data": {"exited": 1, "exitcode": 1, "err-data": "No such file or directory"}}
        )
    )
    with pytest.raises(guest_browse.ListingError, match="No such file or directory"):
        await list_directories(session_data, "vm", "133", "linux", "/does/not/exist")


@respx.mock
async def test_run_exec_polls_until_exited(session_data, monkeypatch):
    monkeypatch.setattr(guest_browse.asyncio, "sleep", lambda *_: _noop())
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 5}}))
    route = _status_route()
    route.side_effect = [
        httpx.Response(200, json={"data": {"exited": 0}}),
        httpx.Response(200, json={"data": {"exited": 0}}),
        httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "done"}}),
    ]
    exitcode, out, _err = await guest_browse._run_exec(session_data, "qemu", "133", ["echo", "hi"])
    assert exitcode == 0
    assert out == "done"


async def _noop():
    return None


@respx.mock
async def test_run_exec_retries_the_start_call_on_a_definite_error(session_data, monkeypatch):
    from backend import guest_agent_lock

    monkeypatch.setattr(guest_browse.asyncio, "sleep", lambda *_: _noop())
    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", lambda *_a, **_kw: _noop())
    start_route = _exec_route()
    start_route.side_effect = [
        httpx.Response(500, text="busy"),
        httpx.Response(200, json={"data": {"pid": 1}}),
    ]
    _status_route().mock(
        return_value=httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "ok"}})
    )

    exitcode, out, _err = await guest_browse._run_exec(session_data, "qemu", "133", ["echo", "hi"])
    assert exitcode == 0
    assert out == "ok"
    assert start_route.call_count == 2


@respx.mock
async def test_run_exec_does_not_retry_start_call_on_timeout(session_data):
    start_route = _exec_route()
    start_route.side_effect = httpx.TimeoutException("timed out")

    with pytest.raises(httpx.TimeoutException):
        await guest_browse._run_exec(session_data, "qemu", "133", ["echo", "hi"])
    assert start_route.call_count == 1


@respx.mock
async def test_run_exec_tolerates_a_transient_error_mid_poll(session_data, monkeypatch):
    monkeypatch.setattr(guest_browse.asyncio, "sleep", lambda *_: _noop())
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 5}}))
    poll_route = _status_route()
    poll_route.side_effect = [
        httpx.Response(500, text="transient"),  # bad response mid-poll - must not abort
        httpx.Response(200, json={"data": {"exited": 0}}),
        httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "done"}}),
    ]
    exitcode, out, _err = await guest_browse._run_exec(session_data, "qemu", "133", ["echo", "hi"])
    assert exitcode == 0
    assert out == "done"
