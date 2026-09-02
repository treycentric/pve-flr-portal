import asyncio
from dataclasses import replace as _replace

import httpx
import pytest

from backend import guest_agent, pve_client, restore_bundle, restore_download, restore_runner
from backend.restore_bundle import BundleFormat, BundleItem, ManifestBuilder
from backend.restore_chunking import DEFAULT_CHUNK_SIZE_BYTES
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
        # Deliberately not what run_restore() should call anymore -
        # buffering the whole file (even just once) is exactly what got
        # a memory-constrained deployment OOM-killed on a real large
        # file (docs/plan.md §7.6's 2026-09-01 finding). Raising here
        # means any regression back to whole-file buffering fails loudly
        # in every test that exercises a download, not just a dedicated
        # one.
        raise AssertionError("run_restore() must stream via aiter_bytes(), not aread() - see the OOM finding")

    async def aiter_bytes(self, chunk_size: int):
        # Mirrors real httpx.Response.aiter_bytes(chunk_size=...): fixed-
        # size pieces, last one short, one (empty) piece for empty
        # content - matches split_into_chunks()'s old "empty content
        # still needs one write" behavior at the restore_runner.py level.
        if not self._content:
            yield b""
            return
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]

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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert len(written_files) == 2  # two chunks for content just over one chunk size
    assert all(f"/tmp/pve-flr-portal-{job.id}" in p for p in written_files)
    # ensure dest dir, create scratch dir, concat (cat ... > dest), verify
    # exists, rmdir cleanup
    concat_call = next(c for c in exec_calls if c[:2] == ["sh", "-c"])
    assert "cat" in concat_call[2] and job.destination in concat_call[2]
    assert any(c[:2] == ["test", "-f"] for c in exec_calls)
    assert exec_calls.count(["mkdir", "-p", "/etc"]) == 1  # dest dir, not just scratch
    assert exec_calls[-1][:2] == ["rm", "-rf"]
    # 2 chunk-write units + 1 concat unit, all complete
    assert (job.progress_total, job.progress_current) == (3, 3)
    log_text = "\n".join(job.log_lines)
    assert "Content needs more than one chunk" in log_text
    assert "Creating scratch directory" in log_text
    assert "Concatenation complete" in log_text


async def test_multi_chunk_write_reassembles_to_the_exact_original_bytes(manager, session_data, monkeypatch):
    # Locks down the byte-fidelity property across the streaming
    # rewrite (restore_chunking.py's module docstring) - each chunk is
    # now sliced from the raw content and converted to its wire string
    # immediately before writing, rather than pre-built as a list of
    # Chunk objects; this confirms that slicing is still exact and in
    # order, not just that the right *number* of chunks got written
    # (which test_multi_chunk_write_creates_scratch_writes_concats_and_cleans_up
    # already covers).
    content = bytes(range(256)) * 500  # ~128 KB - spans 3 chunks, full byte range
    job = _make_job(manager, session_data, destination="/etc/hosts")
    _patch_download(monkeypatch, content)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    written = []

    async def fake_write(session, guest_type, vmid, path, wire_content):
        written.append((path, wire_content))

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    # Written in scratch-filename order (part00000, part00001, ...), so
    # concatenating in list order reassembles the original file exactly.
    written.sort(key=lambda pw: pw[0])
    reassembled = b"".join(wire_content.encode("latin-1") for _path, wire_content in written)
    assert reassembled == content


async def test_progress_updates_incrementally_during_multi_chunk_write(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    _patch_download(monkeypatch, b"a" * (61440 * 2 + 1))  # 3 chunks

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    seen_progress = []

    async def fake_write(session, guest_type, vmid, path, content):
        seen_progress.append(job.progress_current)

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        return 0, "", ""

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    # Two PowerShell calls happen now (New-Item for the destination dir,
    # then this one for the mtime) - filter to the one that actually sets
    # LastWriteTime rather than assuming the first PowerShell call is it.
    ps_call = next(c for c in exec_calls if c[0] == "powershell" and "LastWriteTime" in c[-1])
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

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        # Ensuring the destination directory exists still legitimately
        # runs - only the mtime-setting command itself should be skipped.
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("no mtime to apply - skipped" in line for line in job.log_lines)
    assert not any(c[0] == "touch" for c in exec_calls)


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

    verify_call_kwargs = {}

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        # Ensuring the destination directory exists runs first; the
        # checksum verification command is the last exec call.
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        assert argv == ["sha256sum", "/etc/hosts"]
        verify_call_kwargs.update(kwargs)
        return 0, f"{expected}  /etc/hosts\n", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("Checksum verified" in line for line in job.log_lines)
    # Hashing scales with file size - confirmed live 2026-09-01 that the
    # default ~15s guest-exec budget isn't enough for a large file, so
    # this call needs the long-running timeout, not the default.
    long_timeout = restore_runner.settings.restore_long_running_exec_timeout_seconds
    assert verify_call_kwargs.get("timeout_seconds") == long_timeout


async def test_verify_mismatch_marks_failed(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts", verify=True)
    _patch_download(monkeypatch, b"hello world")

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        # Ensuring the destination directory exists runs first
        # (PowerShell New-Item); the certutil hash check is the last
        # exec call.
        if argv[0] == "powershell":
            return 0, "", ""
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


# --- Design C (network-pull, docs/plan.md §7.6, issue #22) ----------------


def _with_data_nics(monkeypatch, raw_json: str):
    monkeypatch.setattr(restore_runner, "settings", _replace(restore_runner.settings, restore_data_nics_json=raw_json))


async def test_design_c_unconfigured_falls_back_to_design_b_without_any_extra_calls(manager, session_data, monkeypatch):
    # No RESTORE_DATA_NICS set (the default, and the default test env) -
    # _try_design_c must bail out before making any live-ish call at all.
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fail_if_called(*a, **kw):
        raise AssertionError("Design C is unconfigured - should never probe the guest's network or a fetch tool")

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fail_if_called)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    # Went through the ordinary Design B path (mkdir, sh -c cat ..., test -f).
    assert any(c[:2] == ["sh", "-c"] for c in exec_calls)


async def test_design_c_no_subnet_match_falls_back_to_design_b(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["192.168.1.50"]  # doesn't match the configured 10.0.5.0/24

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any(c[:2] == ["sh", "-c"] for c in exec_calls)  # Design B's concat, not a fetch command
    assert any("no configured data NIC matches" in line for line in job.log_lines)


async def test_design_c_no_fetch_tool_falls_back_to_design_b(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["10.0.5.42"]

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    exec_calls = []

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        if argv[:2] == ["sh", "-c"] and "command -v" in argv[2]:
            return 1, "", "not found"  # every fetch-tool probe fails
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("no usable fetch tool found" in line for line in job.log_lines)
    # Still ends up doing the real Design B concat (a "cat ... > dest" call, not just probes).
    assert any(c[:2] == ["sh", "-c"] and "cat" in c[2] for c in exec_calls)


async def test_design_c_used_when_nic_and_tool_are_both_available(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["10.0.5.42"]

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    exec_calls = []
    fetch_call_kwargs = {}

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""  # _ensure_destination_dir, runs before Design C is even attempted
        if argv[:2] == ["sh", "-c"] and "command -v curl" in argv[2]:
            return 0, "/usr/bin/curl", ""  # curl is available - first POSIX candidate
        if argv[0] == "curl":
            fetch_call_kwargs.update(kwargs)
            return 0, "", ""  # the actual fetch
        if argv[:2] == ["test", "-f"]:
            return 0, "", ""  # _verify_destination_exists
        raise AssertionError(f"unexpected exec call once Design C should have taken over: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any(c[0] == "curl" for c in exec_calls)
    assert not any(c[:2] == ["sh", "-c"] and "cat" in (c[2] if len(c) > 2 else "") for c in exec_calls)
    assert any("fetching via curl" in line for line in job.log_lines)
    assert any("fetch complete" in line for line in job.log_lines)
    # The fetch itself scales with file size/network throughput, not the
    # ~15s "fast command" default - confirmed live 2026-09-01 that the
    # default timed out mid-fetch on a real file.
    long_timeout = restore_runner.settings.restore_long_running_exec_timeout_seconds
    assert fetch_call_kwargs.get("timeout_seconds") == long_timeout
    # A single-use download token was actually minted for this job -
    # nothing in this fake flow consumes it (the real consumer is the
    # download endpoint itself, exercised separately in test_endpoints.py).
    assert len(restore_download._tokens) == 1
    assert next(iter(restore_download._tokens.values())).job_id == job.id


async def test_design_c_with_verify_hashes_the_drained_stream_correctly(manager, session_data, monkeypatch):
    # Direct Network Transfer never writes the source bytes anywhere
    # itself (the guest fetches them independently) - this confirms the
    # checksum still gets computed correctly by hashing the stream while
    # draining it, not by buffering the whole file first.
    import hashlib

    content = bytes(range(256)) * 500  # ~128 KB, spans multiple chunks
    expected = hashlib.sha256(content).hexdigest()
    job = _make_job(manager, session_data, destination="/etc/hosts", verify=True)
    _patch_download(monkeypatch, content)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["10.0.5.42"]

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[:2] == ["sh", "-c"] and "command -v curl" in argv[2]:
            return 0, "/usr/bin/curl", ""
        if argv[0] == "curl":
            return 0, "", ""
        if argv[:2] == ["test", "-f"]:
            return 0, "", ""
        if argv[0] == "sha256sum":
            return 0, f"{expected}  /etc/hosts\n", ""
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE
    assert any("Checksum verified" in line for line in job.log_lines)


async def test_design_c_fetch_failure_fails_the_job_rather_than_falling_back(manager, session_data, monkeypatch):
    # Once Design C has been confidently offered (a NIC and a tool were
    # both found), a real failure during the fetch itself should be a
    # clear job failure, never a silent retry via Design B.
    job = _make_job(manager, session_data, destination="/etc/hosts")
    content = b"a" * (61440 + 100)
    _patch_download(monkeypatch, content)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["10.0.5.42"]

    async def fake_write(session, guest_type, vmid, path, content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[:2] == ["sh", "-c"] and "command -v curl" in argv[2]:
            return 0, "/usr/bin/curl", ""
        if argv[0] == "curl":
            return 1, "", "curl: (7) Failed to connect"
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "Direct Network Transfer failed" in job.error


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

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
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

    async def flaky_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["rm", "-rf"]:
            raise httpx.TimeoutException("cleanup timed out")
        return 0, "", ""

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", flaky_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.DONE


# --- multi-file/directory bundle restore (docs/plan.md §7.7, issue #24) ---


def _bundle_job(manager, session_data, **overrides):
    defaults = dict(
        items=[
            BundleItem(filepath="L2V0Yw==", name="etc", leaf=False),
            BundleItem(filepath="L2hvbWUvZmlsZQ==", name="file", leaf=True),
        ],
        source_filepath="",
        source="2 item(s)",
        destination="/home/user/restore",
    )
    defaults.update(overrides)
    return _make_job(manager, session_data, **defaults)


class _NoopTempDirCtx:
    def cleanup(self) -> None:
        pass


def _patch_build_bundle(monkeypatch, tmp_path, content: bytes, fmt=BundleFormat.TAR_GZ, manifest_len=2):
    bundle_path = tmp_path / "bundle.out"
    bundle_path.write_bytes(content)
    manifest = ManifestBuilder()
    for i in range(manifest_len):
        manifest.add(f"file{i}", "deadbeef")

    async def fake_build_bundle(session, volume, items, guest_os_family, zst_capable):
        return bundle_path, fmt, manifest, _NoopTempDirCtx()

    monkeypatch.setattr(restore_bundle, "build_bundle", fake_build_bundle)
    return bundle_path


async def test_run_restore_dispatches_to_bundle_path_when_items_is_set(manager, session_data, monkeypatch):
    job = _bundle_job(manager, session_data)
    calls = []

    async def fake_bundle(j, jobs):
        calls.append("bundle")

    async def fake_single(j, jobs):
        calls.append("single")

    monkeypatch.setattr(restore_runner, "_run_bundle_restore", fake_bundle)
    monkeypatch.setattr(restore_runner, "_run_single_file_restore", fake_single)

    await run_restore(job, manager)
    assert calls == ["bundle"]


async def test_run_restore_dispatches_to_single_file_path_when_items_is_none(manager, session_data, monkeypatch):
    job = _make_job(manager, session_data)  # no items - the ordinary single-file case
    calls = []

    async def fake_bundle(j, jobs):
        calls.append("bundle")

    async def fake_single(j, jobs):
        calls.append("single")

    monkeypatch.setattr(restore_runner, "_run_bundle_restore", fake_bundle)
    monkeypatch.setattr(restore_runner, "_run_single_file_restore", fake_single)

    await run_restore(job, manager)
    assert calls == ["single"]


async def test_bundle_restore_happy_path(manager, session_data, monkeypatch, tmp_path):
    job = _bundle_job(manager, session_data, guest_type="ct", vmid="104")
    content = b"a" * 5000
    _patch_build_bundle(monkeypatch, tmp_path, content, fmt=BundleFormat.TAR_GZ, manifest_len=2)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    written = []
    exec_calls = []

    async def fake_write(session, guest_type, vmid, path, wire_content):
        written.append((path, wire_content))

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        exec_calls.append(argv)
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""  # ensure-dest-dir and create-scratch-dir both use this
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", "tar: unsupported compression"  # zst probe fails -> targz fallback confirmed
        if argv[:2] == ["sh", "-c"] and "cat" in argv[2]:
            return 0, "", ""  # concat into the scratch bundle file
        if argv[:2] == ["tar", "-xf"]:
            return 0, "", ""  # extraction
        if argv[:2] == ["sh", "-c"] and "sha256sum -c" in argv[2]:
            return 0, "All files OK\n", ""  # manifest verify
        if argv[:2] == ["rm", "-rf"]:
            return 0, "", ""  # scratch cleanup
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert len(written) >= 1  # bundle content chunked to the guest
    assert any(c[:2] == ["tar", "-xf"] for c in exec_calls)
    assert any(c[:2] == ["sh", "-c"] and "sha256sum -c" in c[2] for c in exec_calls)
    log_text = "\n".join(job.log_lines)
    assert "Starting restore of 2 item(s)" in log_text
    assert "Bundle built" in log_text
    assert "Extraction complete" in log_text
    assert "Checksum verified for all 2 restored file(s)" in log_text


async def test_bundle_restore_progress_total_reflects_real_chunk_count_from_the_start(
    manager, session_data, monkeypatch, tmp_path
):
    # Confirmed live 2026-09-02: the write-loop's old "+1 ahead of
    # current" placeholder scheme (built for the single-file path's
    # genuinely-unknown-length streaming download) rounded up to a
    # displayed 100% after only ~200 chunks against a several-thousand-
    # chunk bundle - misreading as stuck a few percent into the real
    # work. A bundle's size is already known before writing starts, so
    # progress_total should reflect the true chunk count immediately,
    # not asymptotically approach 100% early.
    job = _bundle_job(manager, session_data, guest_type="ct", vmid="104")
    content = b"a" * (DEFAULT_CHUNK_SIZE_BYTES * 10)  # exactly 10 chunks
    _patch_build_bundle(monkeypatch, tmp_path, content, fmt=BundleFormat.TAR_GZ, manifest_len=2)

    seen_totals_during_write = []

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, wire_content):
        if "probe" not in path:  # skip the earlier tar.zst capability-probe write
            seen_totals_during_write.append(job.progress_total)

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", "tar: unsupported compression"
        if argv[:2] == ["sh", "-c"] and "cat" in argv[2]:
            return 0, "", ""
        if argv[:2] == ["tar", "-xf"]:
            return 0, "", ""
        if argv[:2] == ["sh", "-c"] and "sha256sum -c" in argv[2]:
            return 0, "All files OK\n", ""
        if argv[:2] == ["rm", "-rf"]:
            return 0, "", ""
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    # The total was already correct (10 writes + concat + extract +
    # verify = 13) before the very first chunk was written - not still
    # chasing "+1 ahead of current" and creeping up as chunks land.
    assert seen_totals_during_write == [13] * 10


async def test_bundle_restore_uses_direct_network_transfer_when_available(manager, session_data, monkeypatch, tmp_path):
    # 2026-09-02, docs/plan.md §7.7: bundle restore now tries Direct
    # Network Transfer first (same as single-file) instead of always
    # taking the chunked guest-agent-write path - confirmed live that a
    # several-thousand-chunk bundle write was impractically slow.
    job = _bundle_job(manager, session_data, guest_type="ct", vmid="104")
    content = b"a" * (DEFAULT_CHUNK_SIZE_BYTES * 10)  # >1 chunk, so DNT is even attempted
    _patch_build_bundle(monkeypatch, tmp_path, content, fmt=BundleFormat.TAR_GZ, manifest_len=2)
    _with_data_nics(monkeypatch, '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}]')

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_ips(session, guest_type, vmid):
        return ["10.0.5.42"]

    written = []
    fetch_urls = []

    async def fake_write(session, guest_type, vmid, path, wire_content):
        written.append(path)  # only the tar.zst probe write should land here, never bundle chunks

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", "tar: unsupported compression"  # zst probe fails -> targz fallback
        if argv[:2] == ["sh", "-c"] and "command -v curl" in argv[2]:
            return 0, "/usr/bin/curl", ""
        if argv[0] == "curl":
            fetch_urls.append(argv[-1])
            return 0, "", ""  # the actual fetch - a real guest would hit the download-token endpoint
        if argv[:2] == ["test", "-f"]:
            return 0, "", ""  # _verify_destination_exists
        if argv[:2] == ["tar", "-xf"]:
            return 0, "", ""  # extraction
        if argv[:2] == ["sh", "-c"] and "sha256sum -c" in argv[2]:
            return 0, "All files OK\n", ""
        if argv[:2] == ["rm", "-rf"]:
            return 0, "", ""
        raise AssertionError(f"unexpected exec call once Direct Network Transfer should have taken over: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_agent, "get_guest_ip_addresses", fake_ips)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)

    assert job.status == RestoreStatus.DONE
    assert len(fetch_urls) == 1
    assert "/api/restore-downloads/" in fetch_urls[0]
    # Only the tar.zst capability probe write happens - no bundle chunk
    # writes at all once Direct Network Transfer takes over.
    assert written == [next(iter(written))]
    assert all("probe" in p for p in written)
    assert any("via Direct Network Transfer" in line for line in job.log_lines)
    # The minted token carries the bundle's local path, not just a job_id -
    # the download endpoint needs it to serve the file directly.
    assert len(restore_download._tokens) == 1
    minted = next(iter(restore_download._tokens.values()))
    assert minted.job_id == job.id
    assert minted.local_path is not None


async def test_bundle_restore_blocked_without_design_b(manager, session_data, monkeypatch):
    job = _bundle_job(manager, session_data)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "Unrestricted" in job.error


async def test_bundle_restore_extract_failure_fails_the_job(manager, session_data, monkeypatch, tmp_path):
    job = _bundle_job(manager, session_data)
    _patch_build_bundle(monkeypatch, tmp_path, b"content", fmt=BundleFormat.TAR_GZ)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, wire_content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", ""
        if argv[:2] == ["sh", "-c"] and "cat" in argv[2]:
            return 0, "", ""
        if argv[:2] == ["tar", "-xf"]:
            return 1, "", "tar: cannot open bundle"
        if argv[:2] == ["rm", "-rf"]:
            return 0, "", ""
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "extract" in job.error.lower()


async def test_bundle_restore_verify_failure_fails_the_job_not_silently(manager, session_data, monkeypatch, tmp_path):
    job = _bundle_job(manager, session_data)
    _patch_build_bundle(monkeypatch, tmp_path, b"content", fmt=BundleFormat.TAR_GZ)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def fake_write(session, guest_type, vmid, path, wire_content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", ""
        if argv[:2] == ["sh", "-c"] and "cat" in argv[2]:
            return 0, "", ""
        if argv[:2] == ["tar", "-xf"]:
            return 0, "", ""
        if argv[:2] == ["sh", "-c"] and "sha256sum -c" in argv[2]:
            return 1, "file0: FAILED\n", ""  # a real mismatch
        if argv[:2] == ["rm", "-rf"]:
            return 0, "", ""
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert "verification failed" in job.error.lower()


async def test_bundle_restore_cleans_up_scratch_and_temp_dir_on_failure(manager, session_data, monkeypatch, tmp_path):
    job = _bundle_job(manager, session_data)
    bundle_path = tmp_path / "bundle.out"
    bundle_path.write_bytes(b"content")
    manifest = ManifestBuilder()
    manifest.add("f", "abc")
    cleanup_calls = []

    class _TrackedTempDirCtx:
        def cleanup(self):
            cleanup_calls.append(1)

    async def fake_build_bundle(session, volume, items, guest_os_family, zst_capable):
        return bundle_path, BundleFormat.TAR_GZ, manifest, _TrackedTempDirCtx()

    monkeypatch.setattr(restore_bundle, "build_bundle", fake_build_bundle)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    rmdir_calls = []

    async def fake_write(session, guest_type, vmid, path, wire_content):
        pass

    async def fake_exec(session, guest_type, vmid, argv, **kwargs):
        if argv[:2] == ["mkdir", "-p"]:
            return 0, "", ""
        if argv[0] == "tar" and "-xO" in argv:
            return 1, "", ""
        if argv[:2] == ["sh", "-c"] and "cat" in argv[2]:
            return 0, "", ""
        if argv[:2] == ["tar", "-xf"]:
            raise RuntimeError("guest connection lost mid-extract")
        if argv[:2] == ["rm", "-rf"]:
            rmdir_calls.append(argv)
            return 0, "", ""
        raise AssertionError(f"unexpected exec call: {argv}")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(pve_client, "write_guest_file", fake_write)
    monkeypatch.setattr(pve_client, "run_guest_exec", fake_exec)

    await run_restore(job, manager)
    assert job.status == RestoreStatus.FAILED
    assert len(rmdir_calls) == 1  # scratch dir cleanup still ran
    assert cleanup_calls == [1]  # local temp dir cleanup still ran
