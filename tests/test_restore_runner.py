import asyncio

import httpx
import pytest

from backend import pve_client
from backend.restore_jobs import RestoreJobManager, RestoreStatus
from backend.restore_runner import run_content_only_restore


@pytest.fixture
def manager():
    m = RestoreJobManager()
    yield m
    m.clear()


def _make_job(manager, session_data, **overrides):
    defaults = dict(
        session=session_data,
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        task_name="Restore hosts -> C:\\Windows\\Temp\\hosts",
        snapshot_time="2026-08-30T14:48:06Z",
        source_volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        source_filepath="L2V0Yy9ob3N0cw==",
        source="/etc/hosts",
        destination="C:\\Windows\\Temp\\hosts",
    )
    defaults.update(overrides)
    return manager.create(**defaults)


class FakeDownloadResponse:
    def __init__(self, content: bytes):
        self._content = content
        self.aclose_called = False

    async def aread(self) -> bytes:
        return self._content

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeDownloadClient:
    def __init__(self):
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


async def test_small_file_writes_and_marks_done(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    fake_response = FakeDownloadResponse(b"127.0.0.1 localhost")
    fake_client = FakeDownloadClient()
    written = {}

    async def fake_open_download(session, volume, filepath, tar=False):
        assert volume == job.source_volume
        assert filepath == job.source_filepath
        return fake_client, fake_response

    async def fake_write_guest_file(session, guest_type, vmid, path, content):
        written["guest_type"] = guest_type
        written["vmid"] = vmid
        written["path"] = path
        written["content"] = content

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write_guest_file)

    await run_content_only_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert written["path"] == job.destination
    assert written["content"] == "127.0.0.1 localhost"
    assert fake_response.aclose_called
    assert fake_client.aclose_called


async def test_oversized_file_fails_with_clear_message(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    big_content = b"a" * (61440 + 1)

    async def fake_open_download(session, volume, filepath, tar=False):
        return FakeDownloadClient(), FakeDownloadResponse(big_content)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("write_guest_file should not be called for an oversized file")

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    monkeypatch.setattr(pve_client, "write_guest_file", fail_if_called)

    await run_content_only_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "too large" in job.error


async def test_pve_error_during_write_marks_failed(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)

    async def fake_open_download(session, volume, filepath, tar=False):
        return FakeDownloadClient(), FakeDownloadResponse(b"small")

    async def fake_write_guest_file(session, guest_type, vmid, path, content):
        # 400 with an empty/generic reason phrase is what makes
        # _pve_error_message fall back to the JSON "message" field -
        # matches main.py's identical helper (see docs/plan.md §3's PVE
        # error-shape notes).
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(
                400, json={"message": "guest-file-write disabled"}, request=httpx.Request("POST", "http://x")
            ),
        )

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write_guest_file)

    await run_content_only_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "guest-file-write disabled" in job.error


async def test_cancel_before_write_marks_cancelled_not_written(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)

    async def fake_open_download(session, volume, filepath, tar=False):
        job.cancel_requested = True  # simulate a cancel arriving mid-download
        return FakeDownloadClient(), FakeDownloadResponse(b"small")

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("write_guest_file should not run after a cancel request")

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    monkeypatch.setattr(pve_client, "write_guest_file", fail_if_called)

    await run_content_only_restore(job, manager)

    assert job.status == RestoreStatus.CANCELLED


async def test_task_cancel_mid_run_settles_job_as_cancelled(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    started = asyncio.Event()

    async def hangs_forever(session, volume, filepath, tar=False):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(pve_client, "open_download", hangs_forever)

    task = asyncio.create_task(run_content_only_restore(job, manager))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert job.status == RestoreStatus.CANCELLED


async def test_unexpected_error_marks_failed_not_hung(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)

    async def boom(session, volume, filepath, tar=False):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(pve_client, "open_download", boom)

    await run_content_only_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "something unexpected" in job.error
