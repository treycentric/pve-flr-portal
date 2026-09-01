"""PH.5: runs a submitted RestoreJob's actual work (docs/plan.md §7.5).

Three independent facts decide what a given restore actually needs,
worked out at runtime rather than guessed at submission time: whether
the content fits in a single `agent/file-write` call, whether metadata
restore was requested, and whether verify was requested. None of them
means guest-exec is needed; any one of them does. When it is, the
caller's `VM.GuestAgent.Unrestricted` grant is re-checked here (never
trusted from the earlier `/api/restore-capabilities` response used to
build the confirmation UI).
"""
import asyncio
import hashlib

import httpx

from . import guest_agent, pve_client
from .auth import ensure_fresh_ticket
from .restore_chunking import (
    Chunk,
    needs_guest_exec,
    scratch_dir_path,
    scratch_filename,
    scratch_path_sep,
    split_into_chunks,
)
from .restore_jobs import RestoreJob, RestoreJobManager, RestoreStatus


def _pve_error_message(exc: httpx.HTTPStatusError) -> str:
    reason = exc.response.reason_phrase
    if reason and reason.strip().lower() not in ("bad request", ""):
        return reason
    try:
        data = exc.response.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:
        pass
    return str(exc)


async def _exec(job: RestoreJob, argv: list[str]) -> tuple[int, str, str]:
    return await pve_client.run_guest_exec(job.session, job.guest_type, job.vmid, argv)


async def _create_scratch_dir(job: RestoreJob, guest_os_family: str | None, scratch_dir: str) -> None:
    if guest_os_family == "windows":
        exitcode, out, err = await _exec(job, ["cmd", "/c", "mkdir", scratch_dir])
    else:
        exitcode, out, err = await _exec(job, ["mkdir", "-p", scratch_dir])
    if exitcode != 0:
        raise RuntimeError(f"Could not create scratch directory: {err.strip() or out.strip()}")


async def _remove_scratch_dir(job: RestoreJob, guest_os_family: str | None, scratch_dir: str) -> None:
    # Best-effort cleanup - a leftover scratch dir under the guest's own
    # temp root is harmless clutter, not worth failing an otherwise-done
    # restore over, so failures here are swallowed rather than raised.
    try:
        if guest_os_family == "windows":
            await _exec(job, ["cmd", "/c", "rmdir", "/s", "/q", scratch_dir])
        else:
            await _exec(job, ["rm", "-rf", scratch_dir])
    except Exception:
        pass


async def _write_chunks_to_scratch(
    job: RestoreJob, chunks: list[Chunk], guest_os_family: str | None, scratch_dir: str
) -> list[str]:
    sep = scratch_path_sep(guest_os_family)
    paths = []
    for chunk in chunks:
        if job.cancel_requested:
            break
        chunk_path = scratch_dir + sep + scratch_filename(job.id, chunk)
        await ensure_fresh_ticket(job.session)
        await pve_client.write_guest_file(job.session, job.guest_type, job.vmid, chunk_path, chunk.content)
        paths.append(chunk_path)
    return paths


async def _concat_chunks(job: RestoreJob, chunk_paths: list[str], guest_os_family: str | None) -> None:
    """Concatenates already-written scratch chunk files into the final
    destination, in order. `job.destination` was already validated via
    pve_client.check_path_safe() by the caller before this runs - the
    only reason that check exists is to make embedding it in one of
    these shell/PowerShell-interpreted command strings safe; the chunk
    paths themselves are this module's own generated scratch names
    (never user input), so they don't need the same check."""
    if guest_os_family == "windows":
        parts = "+".join(f'"{p}"' for p in chunk_paths)
        command = f'copy /b {parts} "{job.destination}"'
        argv = ["cmd", "/c", command]
    else:
        parts = " ".join(f"'{p}'" for p in chunk_paths)
        command = f"cat {parts} > '{job.destination}'"
        argv = ["sh", "-c", command]
    exitcode, out, err = await _exec(job, argv)
    if exitcode != 0:
        raise RuntimeError(f"Could not assemble the restored file: {err.strip() or out.strip()}")


async def _restore_mtime(job: RestoreJob, guest_os_family: str | None) -> None:
    """Sets the destination's modified time to the source's original
    mtime - the only metadata file-restore/list actually exposes (no
    uid/gid/mode field on any PVE version, docs/plan.md §7.5's
    corrected scope). No-ops if the job has no mtime to apply."""
    if job.source_mtime is None:
        return
    if guest_os_family == "windows":
        script = (
            "try { (Get-Item -LiteralPath '"
            + job.destination
            + "').LastWriteTime = "
            + f"[DateTimeOffset]::FromUnixTimeSeconds({job.source_mtime}).LocalDateTime "
            + "} catch { Write-Error $_; exit 1 }"
        )
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        argv = ["touch", "-d", f"@{job.source_mtime}", job.destination]
    exitcode, out, err = await _exec(job, argv)
    if exitcode != 0:
        raise RuntimeError(f"Could not restore the original modified time: {err.strip() or out.strip()}")


def _parse_certutil_hash(out: str) -> str:
    # certutil -hashfile prints: a header line, the hash as
    # space-separated hex byte pairs on its own line, then a trailer -
    # not yet live-verified against a real guest (docs/plan.md §7.5).
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"Unexpected certutil output: {out!r}")
    return lines[1].replace(" ", "").lower()


async def _verify_checksum(job: RestoreJob, expected_sha256: str, guest_os_family: str | None) -> bool:
    if guest_os_family == "windows":
        exitcode, out, err = await _exec(job, ["certutil", "-hashfile", job.destination, "SHA256"])
        if exitcode != 0:
            raise RuntimeError(f"Could not verify the restored file: {err.strip() or out.strip()}")
        actual = _parse_certutil_hash(out)
    else:
        exitcode, out, err = await _exec(job, ["sha256sum", job.destination])
        if exitcode != 0:
            raise RuntimeError(f"Could not verify the restored file: {err.strip() or out.strip()}")
        actual = out.strip().split()[0].lower() if out.strip() else ""
    return actual == expected_sha256.lower()


async def run_restore(job: RestoreJob, jobs: RestoreJobManager) -> None:
    """Streams the source file from file-restore, then writes it - via a
    single agent/file-write call when the content fits in one chunk and
    neither metadata restore nor verify was requested, otherwise via the
    scratch-file/guest-exec/concatenate path (which also handles the
    optional metadata/verify steps). Runs as a background asyncio task
    (docs/plan.md §7.5's session-handling section: job.session is this
    job's own ticket snapshot, refreshed here independently of the
    interactive session)."""
    scratch_dir: str | None = None
    guest_os_family: str | None = None
    try:
        job.status = RestoreStatus.RUNNING
        await ensure_fresh_ticket(job.session)

        client, response = await pve_client.open_download(
            job.session, job.source_volume, job.source_filepath, tar=False
        )
        try:
            content = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        chunks = split_into_chunks(content)
        needs_exec = needs_guest_exec(chunks) or job.restore_metadata or job.verify

        if not needs_exec:
            await ensure_fresh_ticket(job.session)
            await pve_client.write_guest_file(
                job.session, job.guest_type, job.vmid, job.destination, chunks[0].content
            )
            jobs.mark_done(job.id)
            return

        # Everything past this point talks to guest-exec, so the
        # destination has to be safe to embed in a shell/PowerShell
        # command string (concatenation, LastWriteTime, certutil aren't
        # all pure-argv invocations the way file-write is) - checked once
        # up front rather than at each individual step.
        pve_client.check_path_safe(job.destination)

        await ensure_fresh_ticket(job.session)
        caps = await guest_agent.get_restore_capabilities(job.session, job.guest_type, job.vmid)
        if not caps.design_b.available:
            jobs.mark_failed(
                job.id,
                caps.design_b.reason
                or "This restore needs guest-exec (large file, restore metadata, or verify was "
                "requested), which is not available for this guest.",
            )
            return
        guest_os_family = caps.guest_os_family

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        if len(chunks) == 1:
            await ensure_fresh_ticket(job.session)
            await pve_client.write_guest_file(
                job.session, job.guest_type, job.vmid, job.destination, chunks[0].content
            )
        else:
            scratch_dir = scratch_dir_path(guest_os_family, job.id)
            await _create_scratch_dir(job, guest_os_family, scratch_dir)
            chunk_paths = await _write_chunks_to_scratch(job, chunks, guest_os_family, scratch_dir)
            if job.cancel_requested:
                jobs.mark_cancelled(job.id)
                return
            await _concat_chunks(job, chunk_paths, guest_os_family)

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        if job.restore_metadata:
            await _restore_mtime(job, guest_os_family)

        if job.verify:
            job.status = RestoreStatus.VERIFYING
            expected = hashlib.sha256(content).hexdigest()
            if not await _verify_checksum(job, expected, guest_os_family):
                jobs.mark_failed(
                    job.id,
                    "Restore completed but checksum verification failed - the file may not "
                    "match the backed-up original.",
                )
                return

        jobs.mark_done(job.id)
    except asyncio.CancelledError:
        # task.cancel() (RestoreJobManager.cancel()) interrupts whichever
        # await this was blocked on - settle the job's own status before
        # letting the cancellation propagate, or it would stay "running"
        # forever (cancel_requested alone isn't checked mid-await).
        jobs.mark_cancelled(job.id)
        raise
    except httpx.HTTPStatusError as exc:
        jobs.mark_failed(job.id, _pve_error_message(exc))
    except Exception as exc:  # last-resort guard - a job must never hang "running" forever
        jobs.mark_failed(job.id, str(exc))
    finally:
        # Cleanup regardless of how the job ended (done/failed/cancelled)
        # - a leftover scratch dir isn't specific to any one outcome.
        if scratch_dir is not None:
            await _remove_scratch_dir(job, guest_os_family, scratch_dir)
