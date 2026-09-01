import asyncio

import httpx
import pytest

from backend import guest_agent, pve_client
from backend.restore_jobs import RestoreJobManager, RestoreStatus
from backend.restore_runner import run_restore


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


def _available_caps(**overrides):
    defaults = dict(
        agent_running=True,
        pve_version_ok=True,
        guest_os_family="linux",
        design_a=guest_agent.PathAvailability(True),
        design_b=guest_agent.PathAvailability(True),
        verify_supported=True,
    )
    defaults.update(overrides)
    return guest_agent.RestoreCapabilities(**defaults)


def _patch_download(monkeypatch, content: bytes):
    async def fake_open_download(session, volume, filepath, tar=False):
        return FakeDownloadClient(), FakeDownloadResponse(content)

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)


# --- fast path (no exec needed) ------------------------------------------


async def test_small_file_no_flags_uses_fast_path_no_capability_check(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    _patch_download(monkeypatch, b"127.0.0.1 localhost")
    written = {}

    async def fake_write(session, guest_type, vmid, path, content):
        written.update(path=path, content=content)

    async def fail_if_called(*a, **kw):
        raise AssertionError("capability check should not run when no exec is needed")

    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fail_if_called)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert written["path"] == job.destination
    assert written["content"] == "127.0.0.1 localhost"
    assert (job.progress_total, job.progress_current) == (1, 1)
    log_text = "\n".join(job.log_lines)
    assert "Starting restore" in log_text
    assert "no guest-exec" in log_text
    assert "completed successfully" in log_text


async def test_oversized_file_fails_with_clear_message_when_no_exec_available(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    _patch_download(monkeypatch, b"a" * (61440 + 1))

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "Unrestricted" in job.error


# --- exec-needed path: multi-chunk, metadata, verify ----------------------


async def test_multi_chunk_write_creates_scratch_writes_concats_and_cleans_up(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    written_files = []
    exec_calls = []

    async def fake_write(session, guest_type, vmid, path, content):
        written_files.append(path)

    async def fake_exec(session, guest_type, vmid, argv):
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert len(written_files) == 2  # two chunks for content just over one chunk size
    assert all(f"/tmp/pve-flr-portal-{job.id}" in p for p in written_files)
    # mkdir, concat (cat ... > dest), rmdir cleanup
    assert exec_calls[0][:2] == ["mkdir", "-p"]
    assert exec_calls[1][:2] == ["sh", "-c"]
    assert "cat" in exec_calls[1][2] and job.destination in exec_calls[1][2]
    assert exec_calls[-1][:2] == ["rm", "-rf"]
    # 2 chunk-write units + 1 concat unit, all complete
    assert (job.progress_total, job.progress_current) == (3, 3)
    log_text = "\n".join(job.log_lines)
    assert "needs 2 chunks" in log_text
    assert "Creating scratch directory" in log_text
    assert "Concatenation complete" in log_text


async def test_progress_updates_incrementally_during_multi_chunk_write(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    _patch_download(monkeypatch, b"a" * (61440 * 2 + 1))  # 3 chunks

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    seen_progress = []

    async def fake_write(session, guest_type, vmid, path, content):
        seen_progress.append(job.progress_current)

    async def fake_exec(session, guest_type, vmid, argv):
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.progress_total == 4  # 3 chunks + 1 concat
    # progress_current was 0, 1, 2 respectively *before* each of the three
    # writes completed (incremented right after) - confirms it climbs
    # incrementally rather than jumping straight to the final value.
    assert seen_progress == [0, 1, 2]
    assert job.progress_current == 4


async def test_progress_total_includes_metadata_and_verify_units(manager, session_data, monkeypatch):
    import hashlib

    content = b"small"
    job = _make_job(
        manager, session_data, destination="/etc/hosts", restore_metadata=True, verify=True, source_mtime=123
    )
    _patch_download(monkeypatch, content)
    expected = hashlib.sha256(content).hexdigest()

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv):
        if argv[0] == "sha256sum":
            return 0, f"{expected}  /etc/hosts\n", ""
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    # 1 write unit + 1 metadata unit + 1 verify unit
    assert (job.progress_total, job.progress_current) == (3, 3)


async def test_restore_metadata_runs_touch_on_linux(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts", restore_metadata=True, source_mtime=1700000000)
    _patch_download(monkeypatch, b"small")

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv):
        exec_calls.append(argv)
        return 0, "", ""

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    touch_call = next(c for c in exec_calls if c[0] == "touch")
    assert touch_call == ["touch", "-d", "@1700000000", "/etc/hosts"]


async def test_restore_metadata_runs_powershell_on_windows(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, restore_metadata=True, source_mtime=1700000000)
    _patch_download(monkeypatch, b"small")

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="windows")

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv):
        exec_calls.append(argv)
        return 0, "", ""

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    ps_call = next(c for c in exec_calls if c[0] == "powershell")
    assert "LastWriteTime" in ps_call[-1]
    assert "FromUnixTimeSeconds(1700000000)" in ps_call[-1]
    assert job.destination in ps_call[-1]


async def test_restore_metadata_without_mtime_is_a_no_op(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, restore_metadata=True, source_mtime=None)
    _patch_download(monkeypatch, b"small")

    async def fake_caps(session, guest_type, vmid):
        # design_b available since restore_metadata=True triggers the exec path
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fail_if_called(*a, **kw):
        raise AssertionError("no exec command should run when there's no mtime to apply")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fail_if_called)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("no mtime to apply - skipped" in line for line in job.log_lines)


async def test_verify_success_linux_marks_done(manager, session_data, monkeypatch):
    import hashlib

    content = b"hello world"
    expected = hashlib.sha256(content).hexdigest()
    job = _make_job(manager, session_data, destination="/etc/hosts", verify=True)
    _patch_download(monkeypatch, content)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv):
        assert argv == ["sha256sum", "/etc/hosts"]
        return 0, f"{expected}  /etc/hosts\n", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("Checksum verified" in line for line in job.log_lines)


async def test_verify_mismatch_marks_failed(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts", verify=True)
    _patch_download(monkeypatch, b"hello world")

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv):
        return 0, "0000000000000000000000000000000000000000000000000000000000000000  /etc/hosts\n", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "checksum verification failed" in job.error


async def test_verify_windows_parses_certutil_output(manager, session_data, monkeypatch):
    import hashlib

    content = b"hello world"
    expected = hashlib.sha256(content).hexdigest()
    job = _make_job(manager, session_data, verify=True)
    _patch_download(monkeypatch, content)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="windows")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    certutil_output = (
        f"SHA256 hash of {job.destination}:\n"
        + " ".join(expected[i : i + 2] for i in range(0, len(expected), 2))
        + "\nCertUtil: -hashfile command completed successfully.\n"
    )

    async def fake_exec(session, guest_type, vmid, argv):
        assert argv[0] == "certutil"
        return 0, certutil_output, ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE


async def test_unsafe_destination_fails_before_any_exec_call(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts; rm -rf /", verify=True)
    _patch_download(monkeypatch, b"small")

    async def fail_if_called(*a, **kw):
        raise AssertionError("should never reach a capability check or exec call")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fail_if_called)
    monkeypatch.setattr(pve_client, "run_guest_exec", fail_if_called)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "unsupported characters" in job.error.lower()


# --- cancellation / errors / cleanup ---------------------------------------


async def test_cancel_before_write_marks_cancelled_not_written(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)

    async def fake_open_download(session, volume, filepath, tar=False):
        job.cancel_requested = True  # simulate a cancel arriving mid-download
        return FakeDownloadClient(), FakeDownloadResponse(b"small")

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("write_guest_file should not run after a cancel request")

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    monkeypatch.setattr(pve_client, "write_guest_file", fail_if_called)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.CANCELLED


async def test_cancel_mid_multi_chunk_write_still_cleans_up_scratch(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    _patch_download(monkeypatch, b"a" * (61440 * 2 + 1))  # 3 chunks

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    write_count = 0

    async def fake_write(session, guest_type, vmid, path, content):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            job.cancel_requested = True  # cancel arrives after the first chunk

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv):
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.CANCELLED
    assert write_count == 2  # stopped early, didn't write the third chunk
    assert exec_calls[0][:2] == ["mkdir", "-p"]  # scratch dir was created
    assert exec_calls[-1][:2] == ["rm", "-rf"]  # ...and still cleaned up
    # no concat attempted after a cancel
    assert not any(c[:2] == ["sh", "-c"] for c in exec_calls)


async def test_task_cancel_mid_run_settles_job_as_cancelled(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    started = asyncio.Event()

    async def hangs_forever(session, volume, filepath, tar=False):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(pve_client, "open_download", hangs_forever)

    task = asyncio.create_task(run_restore(job, manager))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert job.status == RestoreStatus.CANCELLED


async def test_pve_error_during_write_marks_failed(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)
    _patch_download(monkeypatch, b"small")

    async def fake_write_guest_file(session, guest_type, vmid, path, content):
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(
                400, json={"message": "guest-file-write disabled"}, request=httpx.Request("POST", "http://x")
            ),
        )

    monkeypatch.setattr(pve_client, "write_guest_file", fake_write_guest_file)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "guest-file-write disabled" in job.error


async def test_unexpected_error_marks_failed_not_hung(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)

    async def boom(session, volume, filepath, tar=False):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(pve_client, "open_download", boom)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.FAILED
    assert "something unexpected" in job.error


async def test_scratch_cleanup_failure_does_not_mask_a_successful_restore(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    _patch_download(monkeypatch, b"a" * (61440 + 1))

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def flaky_exec(session, guest_type, vmid, argv):
        if argv[:2] == ["rm", "-rf"]:
            raise httpx.TimeoutException("cleanup timed out")
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", flaky_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
