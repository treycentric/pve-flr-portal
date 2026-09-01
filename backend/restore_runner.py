"""PH.5: runs a submitted RestoreJob's actual work (docs/plan.md §7.5).

Three independent facts decide what a given restore actually needs,
worked out at runtime rather than guessed at submission time: whether
the content fits in a single `agent/file-write` call, whether metadata
restore was requested, and whether verify was requested. None of them
means guest-exec is needed; any one of them does. When it is, the
caller's `VM.GuestAgent.Unrestricted` grant is re-checked here (never
trusted from the earlier `/api/restore-capabilities` response used to
build the confirmation UI).

Whenever more than one chunk would otherwise be needed, `_try_design_c()`
(docs/plan.md §7.6, issue #22) is tried first as a faster alternative to
the scratch-write+concat path below it - silently, and only when an
admin has actually configured a data NIC that matches the guest's own
subnet; every other guest keeps going through the unchanged Design B
path exactly as before.
"""
import asyncio
import hashlib
import ntpath
import posixpath

import httpx

from . import guest_agent, pve_client, restore_download, restore_network_pull
from .auth import ensure_fresh_ticket
from .config import settings
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
        job.progress_current += 1
    return paths


async def _ensure_destination_dir(job: RestoreJob, guest_os_family: str | None) -> None:
    """Creates the destination's parent directory if it doesn't already
    exist. Confirmed live as a real gap: restoring into a directory that
    didn't exist yet made the concatenation step (_concat_chunks) report
    success - `copy /b`'s exit code isn't reliable enough to catch every
    failure mode - while never actually creating the file, which then
    failed confusingly two steps later in metadata restore ("Cannot find
    path ... because it does not exist"), docs/plan.md §7.5. Tolerant of
    the directory already existing (Windows `mkdir` errors on that;
    `mkdir -p` doesn't)."""
    if guest_os_family == "windows":
        parent = ntpath.dirname(job.destination.rstrip("\\")) or job.destination
        exitcode, out, err = await _exec(job, ["cmd", "/c", f'if not exist "{parent}" mkdir "{parent}"'])
    else:
        parent = posixpath.dirname(job.destination.rstrip("/")) or "/"
        exitcode, out, err = await _exec(job, ["mkdir", "-p", parent])
    if exitcode != 0:
        raise RuntimeError(f"Could not create the destination directory: {err.strip() or out.strip()}")


async def _verify_destination_exists(job: RestoreJob, guest_os_family: str | None) -> None:
    """A direct existence check right after concatenation, because its
    exit code alone isn't trustworthy enough (see _ensure_destination_dir)
    - catches a silent failure here, with a clear message, instead of
    letting it surface confusingly in a later step."""
    if guest_os_family == "windows":
        exitcode, out, _err = await _exec(job, ["cmd", "/c", f'if exist "{job.destination}" (echo FOUND)'])
        exists = "FOUND" in out
    else:
        exitcode, _out, _err = await _exec(job, ["test", "-f", job.destination])
        exists = exitcode == 0
    if not exists:
        raise RuntimeError(
            "Concatenation reported success but the destination file wasn't actually created - "
            "check that the destination directory exists and is writable."
        )


async def _try_design_c(job: RestoreJob, guest_os_family: str | None) -> bool:
    """Design C (docs/plan.md §7.6, issue #22): the network-pull path -
    the guest fetches its own file over its own NIC instead of this app
    chunking it over the slow QMP/virtio-serial channel. Attempted only
    as an alternative to Design B's scratch-write+concat (the caller
    only calls this when more than one chunk would otherwise be needed),
    and only ever silently: returns False - meaning "not eligible, fall
    back to Design B" - the moment any prerequisite isn't met (no data
    NICs configured at all, the common case; the guest's reported
    subnet doesn't match a configured one; no usable fetch tool found;
    the detected tool can't build a command for this URL). Once truly
    eligible (a NIC AND a tool were both found and a command was built),
    an actual failure during the fetch itself raises rather than falling
    back - having confidently offered Design C and then had it fail
    partway is a real problem worth surfacing clearly, not masking by
    silently retrying via a completely different mechanism.

    **Why the download URL is plain HTTP, never HTTPS:** every guest
    fetch tool this app might use would otherwise have to be individually
    taught to trust this app's own (self-signed by default, docs/plan.md
    §7.3) certificate, and `bash`'s `/dev/tcp` fallback cannot speak TLS
    at all regardless. The token (restore_download.py - single-use,
    short TTL) is the real access control on this one route; the NIC
    segmentation design (§7.6) firewalls it to begin with. Skipping TLS
    here is a deliberate, narrow tradeoff, not an oversight - the rest of
    this app (UI, PVE API calls) stays HTTPS-only as always.

    **Not yet wired: `cscript` staging.** Detected as a candidate by
    `detect_fetch_tool()`, but building its command needs a scratch file
    written first (`build_fetch_command()`'s docstring) - that staging,
    and its cleanup, isn't threaded through here yet, so a guest whose
    *only* usable tool is `cscript` currently falls back to Design B
    rather than actually using it. Follow-up, not a silent bug: logged
    clearly below so it's visible in a real job's log if it happens.
    """
    data_nics = restore_network_pull.parse_data_nics(settings.restore_data_nics_json)
    if not data_nics:
        return False  # Design C unconfigured - the common case, cheapest check first

    guest_ips = await guest_agent.get_guest_ip_addresses(job.session, job.guest_type, job.vmid)
    nic = restore_network_pull.select_data_nic(guest_ips, data_nics)
    if nic is None:
        job.log("Design C: no configured data NIC matches this guest's reported subnet - using Design B.")
        return False

    tool = await restore_network_pull.detect_fetch_tool(lambda argv: _exec(job, argv), guest_os_family)
    if tool is None:
        job.log("Design C: no usable fetch tool found in the guest - using Design B.")
        return False
    if tool == "cscript":
        job.log("Design C: only cscript was detected, and its script-staging isn't wired up yet - using Design B.")
        return False

    port = settings.restore_data_nic_port or settings.port
    token = restore_download.mint_token(job.id)
    url = f"http://{nic.local_ip}:{port}/api/restore-downloads/{token}"

    try:
        plan = restore_network_pull.build_fetch_command(tool, url, job.destination, guest_os_family)
    except ValueError as exc:
        job.log(f"Design C: {tool} can't be used for this download ({exc}) - using Design B.")
        return False

    job.log(f"Design C: fetching via {tool} over {nic.local_ip} (matches the guest's own subnet).")
    exitcode, out, err = await _exec(job, plan.exec_argv)
    if exitcode != 0:
        raise RuntimeError(f"Design C fetch failed via {tool}: {err.strip() or out.strip()}")
    await _verify_destination_exists(job, guest_os_family)
    job.log("Design C: fetch complete.")
    return True


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
        job.log(f"Starting restore of {job.source!r} -> {job.destination!r}.")
        await ensure_fresh_ticket(job.session)

        client, response = await pve_client.open_download(
            job.session, job.source_volume, job.source_filepath, tar=False
        )
        try:
            content = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        job.log(f"Downloaded {len(content)} byte(s) from the backup.")

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        chunks = split_into_chunks(content)
        needs_exec = needs_guest_exec(chunks) or job.restore_metadata or job.verify
        if len(chunks) > 1:
            job.log(f"Content needs {len(chunks)} chunks (over the single-call size limit).")

        # Coarse step-count total, known up front so the UI can show a
        # percentage from the start rather than only once work begins -
        # see RestoreJob.progress_total's docstring for what a "unit" is.
        job.progress_total = (
            (len(chunks) if len(chunks) > 1 else 1)
            + (1 if len(chunks) > 1 else 0)  # concatenation step
            + (1 if job.restore_metadata else 0)
            + (1 if job.verify else 0)
        )

        if not needs_exec:
            job.log("Fits in one call and no metadata/verify requested - writing directly, no guest-exec.")
            await ensure_fresh_ticket(job.session)
            await pve_client.write_guest_file(
                job.session, job.guest_type, job.vmid, job.destination, chunks[0].content
            )
            job.progress_current = 1
            jobs.mark_done(job.id)
            return

        # Everything past this point talks to guest-exec, so the
        # destination has to be safe to embed in a shell/PowerShell
        # command string (concatenation, LastWriteTime, certutil aren't
        # all pure-argv invocations the way file-write is) - checked once
        # up front rather than at each individual step.
        pve_client.check_path_safe(job.destination)

        job.log("Checking VM.GuestAgent.Unrestricted availability (needed for guest-exec).")
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
        job.log(f"guest-exec available (guest OS family: {guest_os_family or 'unknown'}).")

        await _ensure_destination_dir(job, guest_os_family)
        job.log("Confirmed the destination directory exists.")

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        if len(chunks) == 1:
            await ensure_fresh_ticket(job.session)
            await pve_client.write_guest_file(
                job.session, job.guest_type, job.vmid, job.destination, chunks[0].content
            )
            job.progress_current += 1
            job.log("Wrote the file directly (single chunk, exec still needed for a later step).")
        elif await _try_design_c(job, guest_os_family):
            # Design C (docs/plan.md §7.6, issue #22): a faster
            # alternative to the scratch-write+concat path below, only
            # ever taken when an admin has actually configured a data
            # NIC that matches this guest's subnet - see _try_design_c's
            # docstring for the full eligibility chain. Counts as the
            # same number of progress units the Design B path below
            # would have used (chunks + concat), so the percentage math
            # stays consistent with progress_total either way.
            job.progress_current += len(chunks) + 1
        else:
            scratch_dir = scratch_dir_path(guest_os_family, job.id)
            job.log(f"Creating scratch directory {scratch_dir!r} in the guest.")
            await _create_scratch_dir(job, guest_os_family, scratch_dir)
            chunk_paths = await _write_chunks_to_scratch(job, chunks, guest_os_family, scratch_dir)
            if job.cancel_requested:
                jobs.mark_cancelled(job.id)
                return
            job.log(f"Wrote all {len(chunk_paths)} chunk(s) to scratch; concatenating into the destination.")
            await _concat_chunks(job, chunk_paths, guest_os_family)
            await _verify_destination_exists(job, guest_os_family)
            job.progress_current += 1
            job.log("Concatenation complete.")

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        if job.restore_metadata:
            if job.source_mtime is None:
                job.log("Restore metadata was requested, but the source had no mtime to apply - skipped.")
            else:
                job.log("Restoring the original modified time.")
                await _restore_mtime(job, guest_os_family)
                job.log("Modified time restored.")
            job.progress_current += 1

        if job.verify:
            job.status = RestoreStatus.VERIFYING
            job.log("Verifying checksum against the source.")
            expected = hashlib.sha256(content).hexdigest()
            verified = await _verify_checksum(job, expected, guest_os_family)
            job.progress_current += 1
            if not verified:
                jobs.mark_failed(
                    job.id,
                    "Restore completed but checksum verification failed - the file may not "
                    "match the backed-up original.",
                )
                return
            job.log("Checksum verified - matches the source.")

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
