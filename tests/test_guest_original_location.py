from urllib.parse import unquote_plus

import httpx
import respx

from backend import guest_original_location as gol
from backend import pve_client

API = pve_client._API_ROOT


def _crumbs(*labels):
    return [{"label": "Root", "filepath": "/"}, *[{"label": lbl, "filepath": lbl} for lbl in labels]]


def _exec_route(vmid="133"):
    return respx.post(f"{API}/nodes/localhost/qemu/{vmid}/agent/exec")


def _status_route(vmid="133"):
    return respx.get(f"{API}/nodes/localhost/qemu/{vmid}/agent/exec-status")


def _mock_exec(out: str, exitcode: int = 0):
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 1}}))
    status_data = {"exited": 1, "exitcode": exitcode, "out-data": out, "err-data": ""}
    _status_route().mock(return_value=httpx.Response(200, json={"data": status_data}))


async def test_too_short_crumbs_are_unavailable(session_data):
    result = await gol.resolve_original_directory(session_data, "133", "windows", "localhost", "vol", _crumbs("disk0"))
    assert result.available is False
    assert result.directory is None


async def test_unrecognized_structure_is_unavailable(session_data):
    result = await gol.resolve_original_directory(
        session_data, "133", "windows", "localhost", "vol", _crumbs("disk0", "somethingelse", "x")
    )
    assert result.available is False


@respx.mock
async def test_disk_ordinal_lookup_failure_is_unavailable(session_data):
    respx.get(f"{API}/nodes/localhost/storage/pbs/file-restore/list").mock(return_value=httpx.Response(500))
    result = await gol.resolve_original_directory(
        session_data, "133", "windows", "localhost", "pbs:backup/vm/133/x", _crumbs("disk0", "part", "3", "Windows")
    )
    assert result.available is False
    assert "disk" in result.reason.lower()


@respx.mock
async def test_windows_partition_resolves_to_drive_letter(session_data, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "disk0", "leaf": False, "filepath": "d0"},
            {"text": "disk1", "leaf": False, "filepath": "d1"},
        ]

    monkeypatch.setattr(gol, "list_path", fake_list_path)
    _mock_exec("C\n")
    result = await gol.resolve_original_directory(
        session_data,
        "133",
        "windows",
        "localhost",
        "vol",
        _crumbs("disk1", "part", "3", "Windows", "System32"),
    )
    assert result.available is True
    assert result.directory == "C:\\Windows\\System32"
    sent = unquote_plus(_exec_route().calls.last.request.content.decode())
    assert "DiskNumber 1" in sent
    assert "PartitionNumber 3" in sent


@respx.mock
async def test_flattened_partition_resolves_to_drive_letter(session_data, monkeypatch):
    """Issue #66 regression: a disk whose `part` folder was flattened
    away (the common case - `part` was its only child) has no literal
    "part" crumb at all, just the partition number directly under the
    disk. This used to fall through to "unavailable" until this shape
    was recognized too."""

    async def fake_list_path(session, volume, filepath="/"):
        return [{"text": "disk0", "leaf": False, "filepath": "d0"}]

    monkeypatch.setattr(gol, "list_path", fake_list_path)
    _mock_exec("D\n")
    result = await gol.resolve_original_directory(
        session_data, "133", "windows", "localhost", "vol", _crumbs("disk0", "3", "Users", "alice")
    )
    assert result.available is True
    assert result.directory == "D:\\Users\\alice"
    sent = unquote_plus(_exec_route().calls.last.request.content.decode())
    assert "PartitionNumber 3" in sent


@respx.mock
async def test_elevated_lvm_resolves_via_vg_lv_crumb(session_data):
    """Issue #66 regression: LVM volume groups are elevated to root-level
    "LVM <vg>" entries - browsing into one lands directly on its logical
    volumes, with no disk/`lvm` crumb prefix at all anymore. This used to
    fall through to "unavailable" until this shape was recognized too."""
    _mock_exec("/home\n")
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("LVM rlm", "home", "alice")
    )
    assert result.available is True
    assert result.directory == "/home/alice"
    sent = unquote_plus(_exec_route().calls.last.request.content.decode())
    assert "/dev/rlm/home" in sent


@respx.mock
async def test_windows_partition_with_no_drive_letter_is_unavailable(session_data, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [{"text": "disk0", "leaf": False, "filepath": "d0"}]

    monkeypatch.setattr(gol, "list_path", fake_list_path)
    _mock_exec("")  # empty output - e.g. a Recovery/EFI partition with no letter
    result = await gol.resolve_original_directory(
        session_data, "133", "windows", "localhost", "vol", _crumbs("disk0", "part", "1", "EFI")
    )
    assert result.available is False
    assert "drive letter" in result.reason.lower()


@respx.mock
async def test_linux_partition_resolves_to_mountpoint(session_data, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [{"text": "disk0", "leaf": False, "filepath": "d0"}]

    monkeypatch.setattr(gol, "list_path", fake_list_path)
    calls = iter(["sda\n", "sda \nsda1 /boot\nsda2 /\nsda3 /home\n"])

    async def fake_run_guest_exec(session, guest_type, vmid, argv, node="localhost", **kwargs):
        return 0, next(calls), ""

    monkeypatch.setattr(gol, "run_guest_exec", fake_run_guest_exec)
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "part", "3", "user", "docs")
    )
    assert result.available is True
    assert result.directory == "/home/user/docs"


@respx.mock
async def test_linux_partition_not_mounted_is_unavailable(session_data, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [{"text": "disk0", "leaf": False, "filepath": "d0"}]

    monkeypatch.setattr(gol, "list_path", fake_list_path)
    calls = iter(["sda\n", "sda \nsda1 \n"])  # partition 1 has no mountpoint column

    async def fake_run_guest_exec(session, guest_type, vmid, argv, node="localhost", **kwargs):
        return 0, next(calls), ""

    monkeypatch.setattr(gol, "run_guest_exec", fake_run_guest_exec)
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "part", "1", "x")
    )
    assert result.available is False
    assert "mounted" in result.reason.lower()


@respx.mock
async def test_lvm_resolves_via_dev_vg_lv(session_data):
    _mock_exec("/home\n")
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "lvm", "rlm", "home", "alice")
    )
    assert result.available is True
    assert result.directory == "/home/alice"
    sent = unquote_plus(_exec_route().calls.last.request.content.decode())
    assert "/dev/rlm/home" in sent


@respx.mock
async def test_lvm_falls_back_to_mapper_device(session_data):
    _status_route().side_effect = [
        httpx.Response(200, json={"data": {"exited": 1, "exitcode": 1, "out-data": "", "err-data": ""}}),
        httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "/home\n", "err-data": ""}}),
    ]
    _exec_route().mock(return_value=httpx.Response(200, json={"data": {"pid": 1}}))
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "lvm", "rlm", "home")
    )
    assert result.available is True
    assert result.directory == "/home"


@respx.mock
async def test_lvm_not_mounted_is_unavailable(session_data):
    _mock_exec("", exitcode=1)
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "lvm", "rlm", "swap")
    )
    assert result.available is False
    assert "mounted" in result.reason.lower()


async def test_lvm_rejects_unsafe_labels_without_calling_the_guest(session_data, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("must not reach the guest with an unsafe label")

    monkeypatch.setattr(gol, "run_guest_exec", boom)
    result = await gol.resolve_original_directory(
        session_data, "133", "linux", "localhost", "vol", _crumbs("disk0", "lvm", "rlm; rm -rf /", "home")
    )
    assert result.available is False
