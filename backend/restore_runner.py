"""PH.5: runs a submitted RestoreJob's actual work (docs/plan.md §7.5).

Three independent facts decide what a given restore actually needs,
worked out at runtime rather than guessed at submission time: whether
the content fits in a single `agent/file-write` call, whether metadata
restore was requested, and whether verify was requested. None of them
means guest-exec is needed; any one of them does. When it is, the
caller's `VM.GuestAgent.Unrestricted` grant is re-checked here (never
trusted from the earlier `/api/restore-capabilities` response used to
build the confirmation UI).

Whenever more than one chunk would otherwise be needed, `_try_direct_network_transfer()`
(docs/plan.md §7.6, issue #22) is tried first as a faster alternative to
the scratch-write+concat path below it - silently, and only when an
admin has actually configured a data NIC that matches the guest's own
subnet; every other guest keeps going through the unchanged chunked
guest-agent-write path exactly as before.

Streams the download rather than buffering the whole file into a
separate wire-ready copy - see restore_chunking.py's module docstring
for the real memory blow-up this replaced (confirmed live 2026-09-01:
a large restore OOM-killed a memory-constrained deployment before ever
reaching the Direct Network Transfer eligibility check).
"""
import asyncio
import hashlib
import ntpath
import posixpath
import time
from pathlib import Path

import httpx

from . import guest_agent, pve_client, restore_bundle, restore_download, restore_network_pull
from .auth import ensure_fresh_ticket
from .config import settings
from .restore_chunking import (
    DEFAULT_CHUNK_SIZE_BYTES,
    bytes_to_wire_str,
    chunk_count,
    scratch_dir_path,
    scratch_filename,
    scratch_path_sep,
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


async def _exec(job: RestoreJob, argv: list[str], timeout_seconds: float | None = None) -> tuple[int, str, str]:
    """`timeout_seconds` defaults to `run_guest_exec()`'s own ~15s
    default when omitted - pass `settings.restore_long_running_exec_timeout_seconds`
    explicitly for a call whose duration scales with file size (see
    that setting's docstring)."""
    kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    return await pve_client.run_guest_exec(job.session, job.guest_type, job.vmid, argv, **kwargs)


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


async def _iter_download_pieces(first_piece: bytes, second_piece: bytes | None, byte_iter) -> None:
    """Re-chains the two pieces `run_restore()` already had to read (to
    find out whether there even *is* a second piece, i.e. whether
    single-call or multi-chunk applies) back in front of whatever's left
    in `byte_iter`, so downstream code can just iterate one clean
    sequence instead of special-casing "the first two are already in
    hand"."""
    yield first_piece
    if second_piece is not None:
        yield second_piece
        async for piece in byte_iter:
            yield piece


async def _drain_and_hash(pieces, hasher) -> int:
    """Consumes the rest of a download without writing it anywhere -
    used when Direct Network Transfer is handling the actual guest-side
    fetch itself, so this process only needs the total byte count (for
    logging) and, if verify was requested, a running checksum - never
    the file's bytes themselves. Returns total bytes seen."""
    total = 0
    async for piece in pieces:
        total += len(piece)
        if hasher is not None:
            hasher.update(piece)
    return total


async def _write_chunks_to_scratch(
    job: RestoreJob,
    guest_os_family: str | None,
    scratch_dir: str,
    pieces,
    hasher,
    total_bytes_hint: int | None = None,
) -> tuple[list[str], int]:
    """Writes each already-downloaded-but-not-yet-buffered piece from
    `pieces` (an async iterable, `_iter_download_pieces()` below) to the
    guest as a numbered scratch file, one piece at a time - never more
    than one piece's worth of raw bytes or wire-ready string alive at
    once, and never the whole file. Updates `hasher` (a hashlib object,
    or None if verify wasn't requested) incrementally so the full
    checksum is available without ever buffering the whole file for that
    either. Returns (scratch paths written, total bytes seen) - the real
    chunk count isn't known until the source is exhausted, unlike the
    pre-2026-09-01 design which downloaded everything up front specifically
    to know this before writing a single byte (confirmed live: that
    up-front-buffering approach OOM-killed a memory-constrained
    deployment on a large file, twice - once for the redundant
    wire-string copy, fixed first, and again for the raw-bytes buffer
    itself, fixed here).

    `total_bytes_hint`: pass the source's real size when it's already
    known up front (e.g. a bundle already fully materialized on local
    disk before this call) so `job.progress_percent` reports real
    progress against an accurate chunk count from the start, instead of
    the "+1 ahead of current" placeholder scheme below (kept as the
    fallback for a genuinely-unknown-length streaming download, where
    the real count truly isn't known until exhausted). Confirmed live
    2026-09-02: without this, a several-thousand-chunk bundle write
    (61440-byte chunks against a 1.5GB+ bundle) rounded up to a
    displayed 100% after only ~200 chunks - a few percent of the real
    work - misreading as stuck rather than still genuinely writing."""
    if total_bytes_hint is not None:
        job.progress_total = max(job.progress_total, chunk_count(total_bytes_hint, DEFAULT_CHUNK_SIZE_BYTES) + 1)
    sep = scratch_path_sep(guest_os_family)
    paths: list[str] = []
    total = 0
    index = 0
    async for piece in pieces:
        if job.cancel_requested:
            break
        if hasher is not None:
            hasher.update(piece)
        total += len(piece)
        chunk_path = scratch_dir + sep + scratch_filename(job.id, index)
        await ensure_fresh_ticket(job.session)
        await pve_client.write_guest_file(
            job.session, job.guest_type, job.vmid, chunk_path, bytes_to_wire_str(piece)
        )
        paths.append(chunk_path)
        job.progress_current += 1
        if total_bytes_hint is None:
            # Keep the total at least one ahead of current (room for the
            # trailing concat unit) while the real count is still unknown -
            # RestoreJob.progress_percent clamps to 100 regardless, but this
            # avoids it reading a premature 100% mid-write.
            job.progress_total = max(job.progress_total, job.progress_current + 1)
        index += 1
    return paths, total


async def _ensure_destination_dir(job: RestoreJob, guest_os_family: str | None) -> None:
    """Creates the destination's parent directory if it doesn't already
    exist. Confirmed live as a real gap: restoring into a directory that
    didn't exist yet made the concatenation step (_concat_chunks) report
    success - `copy /b`'s exit code isn't reliable enough to catch every
    failure mode - while never actually creating the file, which then
    failed confusingly two steps later in metadata restore ("Cannot find
    path ... because it does not exist"), docs/plan.md §7.5. Tolerant of
    the directory already existing.

    Windows uses PowerShell's `New-Item -Force` (idempotent - no
    separate exists-check needed) rather than `cmd /c if not exist "X"
    mkdir "X"`, which this function originally used: confirmed live
    2026-09-01 that cmd.exe's handling of multiple embedded double-quoted
    segments on one `/c` line is unreliable ("The filename, directory
    name, or volume label syntax is incorrect" against a perfectly valid
    path). Single-quoted PowerShell literal instead, matching
    `_restore_mtime()`'s already-live-verified pattern - safe for the
    same reason: `pve_client.check_path_safe()` (called once, up front,
    before any exec step runs) already rejects `'` along with every
    other shell-metacharacter."""
    if guest_os_family == "windows":
        parent = ntpath.dirname(job.destination.rstrip("\\")) or job.destination
        script = f"New-Item -ItemType Directory -Force -Path '{parent}' | Out-Null"
        exitcode, out, err = await _exec(job, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    else:
        parent = posixpath.dirname(job.destination.rstrip("/")) or "/"
        exitcode, out, err = await _exec(job, ["mkdir", "-p", parent])
    if exitcode != 0:
        raise RuntimeError(f"Could not create the destination directory: {err.strip() or out.strip()}")


async def _ensure_directory_exists(job: RestoreJob, dir_path: str, guest_os_family: str | None) -> None:
    """Like `_ensure_destination_dir()` but for a path that's already a
    directory itself, not a file whose *parent* needs creating - a
    bundle restore's `job.destination` IS the extraction target
    directory (docs/plan.md §7.7), unlike a single-file restore's,
    which is a file path within one. Kept as a separate function rather
    than adding a flag to `_ensure_destination_dir()` to avoid touching
    that already-live-tested function's behavior for the single-file
    path it exists for."""
    if guest_os_family == "windows":
        script = f"New-Item -ItemType Directory -Force -Path '{dir_path}' | Out-Null"
        exitcode, out, err = await _exec(job, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    else:
        exitcode, out, err = await _exec(job, ["mkdir", "-p", dir_path])
    if exitcode != 0:
        raise RuntimeError(f"Could not create the destination directory: {err.strip() or out.strip()}")


async def _verify_destination_exists(job: RestoreJob, guest_os_family: str | None, path: str | None = None) -> None:
    """A direct existence check right after concatenation, because its
    exit code alone isn't trustworthy enough (see _ensure_destination_dir)
    - catches a silent failure here, with a clear message, instead of
    letting it surface confusingly in a later step. `path` defaults to
    `job.destination`; a bundle restore's Direct Network Transfer passes
    the scratch bundle-file path instead (docs/plan.md §7.7).

    Windows uses PowerShell's `Test-Path -LiteralPath`, not `cmd /c if
    exist "X" (...)` - see `_ensure_destination_dir()`'s docstring for
    why `cmd /c` with embedded quotes is being moved away from here."""
    target = job.destination if path is None else path
    if guest_os_family == "windows":
        script = f"if (Test-Path -LiteralPath '{target}') {{ Write-Output 'FOUND' }}"
        exitcode, out, _err = await _exec(job, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        exists = "FOUND" in out
    else:
        exitcode, _out, _err = await _exec(job, ["test", "-f", target])
        exists = exitcode == 0
    if not exists:
        raise RuntimeError(
            "Concatenation reported success but the destination file wasn't actually created - "
            "check that the destination directory exists and is writable."
        )


async def _try_direct_network_transfer(
    job: RestoreJob,
    guest_os_family: str | None,
    dest_path: str | None = None,
    local_path: Path | None = None,
) -> bool:
    """Direct Network Transfer (internally "Design C", docs/plan.md
    §7.6, issue #22 - see that section for why the user-facing name
    differs from the dev-doc name): the guest fetches its own file over
    its own NIC instead of this app chunking it over the slow
    QMP/virtio-serial channel. Attempted only as an alternative to the
    scratch-write+concat path (the caller only calls this when more than
    one chunk would otherwise be needed), and only ever silently:
    returns False - meaning "not eligible, fall back to the chunked
    guest-agent write path" - the moment any prerequisite isn't met (no
    data NICs configured at all, the common case; the guest's reported
    subnet doesn't match a configured one; no usable fetch tool found;
    the detected tool can't build a command for this URL). Once truly
    eligible (a NIC AND a tool were both found and a command was built),
    an actual failure during the fetch itself raises rather than falling
    back - having confidently offered Direct Network Transfer and then
    had it fail partway is a real problem worth surfacing clearly, not
    masking by silently retrying via a completely different mechanism.

    `dest_path`/`local_path` (docs/plan.md §7.7, issue #24): a bundle
    restore passes both - `dest_path` is the scratch bundle-file path in
    the guest to fetch *into* (not `job.destination`, which is the
    extraction target directory, not a file), and `local_path` is the
    already-built local bundle file to serve, minted into the download
    token so the endpoint streams it straight off local disk instead of
    re-proxying from PVE (which can't hand back a synthesized bundle as
    one item). Single-file restore leaves both unset - the original
    behavior, fetching straight to `job.destination` via a token that
    re-proxies `job.source_volume`/`job.source_filepath` from PVE.

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
    *only* usable tool is `cscript` currently falls back to the chunked
    write path rather than actually using it. Follow-up, not a silent
    bug: logged clearly below so it's visible in a real job's log if it
    happens.
    """
    data_nics = restore_network_pull.parse_data_nics(settings.restore_data_nics_json)
    if not data_nics:
        return False  # Design C unconfigured - the common case, cheapest check first

    guest_ips = await guest_agent.get_guest_ip_addresses(job.session, job.guest_type, job.vmid)
    nic = restore_network_pull.select_data_nic(guest_ips, data_nics)
    if nic is None:
        job.log("Direct Network Transfer not available: no configured data NIC matches this guest's subnet.")
        return False

    tool = await restore_network_pull.detect_fetch_tool(lambda argv: _exec(job, argv), guest_os_family)
    if tool is None:
        job.log("Direct Network Transfer not available: no usable fetch tool found in the guest.")
        return False
    if tool == "cscript":
        job.log("Direct Network Transfer not available: only cscript was detected, not yet supported.")
        return False

    fetch_dest = job.destination if dest_path is None else dest_path
    port = settings.restore_data_nic_port or settings.port
    token = restore_download.mint_token(job.id, local_path=None if local_path is None else str(local_path))
    url = f"http://{nic.local_ip}:{port}/api/restore-downloads/{token}"

    try:
        plan = restore_network_pull.build_fetch_command(tool, url, fetch_dest, guest_os_family)
    except ValueError as exc:
        job.log(f"Direct Network Transfer not available: {tool} can't be used for this download ({exc}).")
        return False

    job.log(f"Direct Network Transfer: fetching via {tool} over {nic.local_ip} (matches the guest's own subnet).")
    # The fetch itself scales with file size (and network throughput),
    # not the ~15s "fast command" default that every other guest-exec
    # call in this module uses - confirmed live 2026-09-01: the default
    # timed out mid-fetch on a real file. See
    # settings.restore_long_running_exec_timeout_seconds's docstring.
    exitcode, out, err = await _exec(
        job, plan.exec_argv, timeout_seconds=settings.restore_long_running_exec_timeout_seconds
    )
    if exitcode != 0:
        raise RuntimeError(f"Direct Network Transfer failed via {tool}: {err.strip() or out.strip()}")
    await _verify_destination_exists(job, guest_os_family, path=fetch_dest)
    job.log("Direct Network Transfer: fetch complete.")
    return True


async def _concat_chunks(job: RestoreJob, chunk_paths: list[str], dest_path: str, guest_os_family: str | None) -> None:
    """Concatenates already-written scratch chunk files into `dest_path`,
    in order - the single-file restore path's own final destination
    (`job.destination`), or a bundle restore's scratch bundle-archive
    file (docs/plan.md §7.7). `dest_path` must already have passed
    pve_client.check_path_safe() by the caller before this runs - the
    only reason that check exists is to make embedding it in one of
    these shell/PowerShell-interpreted command strings safe; the chunk
    paths themselves are this module's own generated scratch names
    (never user input), so they don't need the same check."""
    if guest_os_family == "windows":
        parts = "+".join(f'"{p}"' for p in chunk_paths)
        command = f'copy /b {parts} "{dest_path}"'
        argv = ["cmd", "/c", command]
    else:
        parts = " ".join(f"'{p}'" for p in chunk_paths)
        command = f"cat {parts} > '{dest_path}'"
        argv = ["sh", "-c", command]
    # Scales with total file size, not the ~15s "fast command" default -
    # see settings.restore_long_running_exec_timeout_seconds's docstring.
    exitcode, out, err = await _exec(job, argv, timeout_seconds=settings.restore_long_running_exec_timeout_seconds)
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
    # Hashing time scales with file size, not the ~15s "fast command"
    # default - see settings.restore_long_running_exec_timeout_seconds's
    # docstring.
    timeout = settings.restore_long_running_exec_timeout_seconds
    if guest_os_family == "windows":
        exitcode, out, err = await _exec(job, ["certutil", "-hashfile", job.destination, "SHA256"], timeout)
        if exitcode != 0:
            raise RuntimeError(f"Could not verify the restored file: {err.strip() or out.strip()}")
        actual = _parse_certutil_hash(out)
    else:
        exitcode, out, err = await _exec(job, ["sha256sum", job.destination], timeout)
        if exitcode != 0:
            raise RuntimeError(f"Could not verify the restored file: {err.strip() or out.strip()}")
        actual = out.strip().split()[0].lower() if out.strip() else ""
    return actual == expected_sha256.lower()


async def run_restore(job: RestoreJob, jobs: RestoreJobManager) -> None:
    """Dispatches to the single-file restore path (unchanged since
    §7.5/§7.6) or the multi-file/directory bundle restore path (§7.7,
    issue #24), based on whether `job.items` is set. `job.items` is
    `None` for every job created before that field existed and for
    every ordinary single-file restore submitted today, so this is a
    zero-behavior-change dispatch for the existing path."""
    if job.items:
        await _run_bundle_restore(job, jobs)
    else:
        await _run_single_file_restore(job, jobs)


async def _run_bundle_restore(job: RestoreJob, jobs: RestoreJobManager) -> None:
    """Multi-file/directory restore (docs/plan.md §7.7, issue #24):
    builds one bundle from every selected item (`restore_bundle.
    build_bundle()`), writes it to the guest via the same chunked
    scratch-write+concat mechanism the single-file path already uses
    (just targeting a scratch bundle file instead of the final
    destination), extracts it, then verifies every extracted file
    against the manifest embedded in the bundle - entirely inside the
    guest, one command, no app-side per-file hash comparison. Always
    needs guest-exec (there's no Design-A-equivalent single-call fast
    path for a bundle). Tries Direct Network Transfer first when the
    bundle would otherwise need more than one chunk (2026-09-02,
    docs/plan.md §7.7) - the download-token endpoint serves the already-
    built local bundle file directly rather than re-proxying from PVE -
    falling back to the chunked scratch-write+concat path silently
    whenever it's not eligible (no configured data NIC, no usable fetch
    tool, etc.), same contract the single-file path already uses."""
    scratch_dir: str | None = None
    guest_os_family: str | None = None
    tmp_dir_ctx = None
    try:
        job.status = RestoreStatus.RUNNING
        job.log(f"Starting restore of {len(job.items)} item(s) -> {job.destination!r}.")
        await ensure_fresh_ticket(job.session)

        # Every guest-exec command below embeds job.destination in a
        # shell/PowerShell-interpreted string (mkdir, extract, verify) -
        # checked once up front, same discipline as the single-file path.
        pve_client.check_path_safe(job.destination)

        job.log("Checking VM.GuestAgent.Unrestricted availability (needed for guest-exec).")
        caps = await guest_agent.get_restore_capabilities(job.session, job.guest_type, job.vmid)
        if not caps.design_b.available:
            jobs.mark_failed(
                job.id,
                caps.design_b.reason
                or "Multi-file/directory restore needs guest-exec (VM.GuestAgent.Unrestricted), "
                "which is not available for this guest.",
            )
            return
        guest_os_family = caps.guest_os_family
        job.log(f"guest-exec available (guest OS family: {guest_os_family or 'unknown'}).")

        await _ensure_directory_exists(job, job.destination, guest_os_family)
        job.log("Confirmed the destination directory exists.")
        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        scratch_dir = scratch_dir_path(guest_os_family, job.id)
        job.log(f"Creating scratch directory {scratch_dir!r} in the guest.")
        await _create_scratch_dir(job, guest_os_family, scratch_dir)
        sep = scratch_path_sep(guest_os_family)

        job.log("Checking whether the guest can decompress .tar.zst directly.")
        probe_path = scratch_dir + sep + f"{job.id}.probe.tar.zst"

        async def _write_fn(path: str, content: str) -> None:
            await ensure_fresh_ticket(job.session)
            await pve_client.write_guest_file(job.session, job.guest_type, job.vmid, path, content)

        zst_capable = await restore_bundle.probe_tar_zst_support(_write_fn, lambda argv: _exec(job, argv), probe_path)
        job.log(f"Guest can decompress .tar.zst directly: {zst_capable}.")
        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        job.log(f"Building the restore bundle from {len(job.items)} item(s).")

        # Confirmed live 2026-09-02: this whole build phase (download +
        # add-to-bundle) had no progress signal at all - a large
        # single-directory selection looked indistinguishable from a
        # hang for several minutes. Logs when each item starts, and
        # periodically (throttled to every ~5s, not every 61440-byte
        # chunk) while it's downloading; when PVE sends a Content-Length
        # for the item, job.progress_current/total track real bytes too
        # (overwritten below once the real total - chunk count or DNT -
        # is known, same "reassign progress_total per phase" pattern
        # already used through the rest of this function).
        progress_state: dict[str, object] = {"item": None, "logged_at": 0.0}

        def _on_item_progress(item: restore_bundle.BundleItem, downloaded: int, total: int | None) -> None:
            now = time.monotonic()
            is_new_item = progress_state["item"] is not item
            if is_new_item:
                progress_state["item"] = item
                progress_state["logged_at"] = now
                size_note = f" ({total} bytes)" if total is not None else ""
                job.log(f"Downloading {item.name!r}{size_note}...")
            if total is not None:
                job.progress_current = downloaded
                job.progress_total = max(total, 1)
            if is_new_item or (now - progress_state["logged_at"]) >= 5.0:
                progress_state["logged_at"] = now
                if total is not None and not is_new_item:
                    pct = round(100 * downloaded / total) if total > 0 else 100
                    job.log(f"Downloading {item.name!r}: {downloaded} / {total} bytes ({pct}%).")
                elif total is None and not is_new_item:
                    job.log(f"Downloading {item.name!r}: {downloaded} bytes so far...")

        output_path, fmt, manifest, tmp_dir_ctx = await restore_bundle.build_bundle(
            job.session, job.source_volume, job.items, guest_os_family, zst_capable, _on_item_progress
        )
        bundle_size_bytes = output_path.stat().st_size
        expected_chunks = chunk_count(bundle_size_bytes, DEFAULT_CHUNK_SIZE_BYTES)
        # Doesn't claim *how* it'll reach the guest yet - Direct Network
        # Transfer is tried next and, when eligible, skips the chunked
        # write entirely; the log line below that actually says "N
        # chunk(s)" only fires on the path that's really taking them.
        job.log(f"Bundle built ({fmt.value}, {len(manifest)} file(s), {bundle_size_bytes} bytes).")

        # Already known exactly - the bundle is fully materialized on
        # local disk at this point, unlike the single-file path's
        # streaming download whose total length isn't known up front.
        job.progress_total = expected_chunks + 1 + 1 + 1  # writes + concat + extract + verify

        bundle_ext = {"tarzst": "tar.zst", "targz": "tar.gz", "zip": "zip"}[fmt.value]
        bundle_guest_path = scratch_dir + sep + f"{job.id}.bundle.{bundle_ext}"

        # Try Direct Network Transfer first when it'd otherwise take more
        # than one chunk (2026-09-02, docs/plan.md §7.7) - the guest
        # fetches the already-built bundle straight from this app's
        # download-token endpoint over its own NIC, instead of tens of
        # thousands of individual agent/file-write round trips at
        # DEFAULT_CHUNK_SIZE_BYTES each. Confirmed live the same day this
        # was added: a ~1.5GB bundle's chunked write was projected at
        # tens of thousands of chunks - Design B alone doesn't scale to
        # bundle-sized payloads the way it does to modest single files.
        # Silently unavailable (no configured data NIC, no usable fetch
        # tool, etc.) falls straight through to the chunked path below,
        # same contract as the single-file path's own use of this.
        if expected_chunks > 1 and await _try_direct_network_transfer(
            job, guest_os_family, dest_path=bundle_guest_path, local_path=output_path
        ):
            # Rescaled to just (transfer, extract, verify) - not
            # expected_chunks-based like the chunked path below, and no
            # separate concat unit (DNT fetches the whole bundle in one
            # shot, nothing to concatenate). Confirmed live 2026-09-02:
            # leaving progress_total at expected_chunks + 3 here meant
            # progress_current (set to expected_chunks, standing in for
            # "the transfer is done") divided by that total rounded to a
            # displayed 100% the instant the fetch finished - before
            # extraction or verification had even started, the same
            # "looks stuck/done early" symptom the chunk-count fix above
            # was written to prevent, just relocated to this phase.
            job.progress_current = 1
            job.progress_total = 3  # transfer + extract + verify
            job.log("Bundle uploaded to the guest via Direct Network Transfer.")
        else:
            async def _bundle_pieces():
                with output_path.open("rb") as f:
                    while piece := f.read(DEFAULT_CHUNK_SIZE_BYTES):
                        yield piece

            chunk_paths, total_bytes = await _write_chunks_to_scratch(
                job, guest_os_family, scratch_dir, _bundle_pieces(), None, total_bytes_hint=bundle_size_bytes
            )
            job.log(f"Wrote the {total_bytes} byte bundle in {len(chunk_paths)} chunk(s); concatenating.")
            if job.cancel_requested:
                jobs.mark_cancelled(job.id)
                return

            job.progress_total = len(chunk_paths) + 1 + 1 + 1  # writes + concat + extract + verify
            await _concat_chunks(job, chunk_paths, bundle_guest_path, guest_os_family)
            job.progress_current += 1
            job.log("Bundle uploaded to the guest.")
        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        job.log("Extracting the bundle in the guest.")
        extract_argv = restore_bundle.build_extract_command(fmt, bundle_guest_path, job.destination, guest_os_family)
        exitcode, out, err = await _exec(
            job, extract_argv, timeout_seconds=settings.restore_long_running_exec_timeout_seconds
        )
        if exitcode != 0:
            raise RuntimeError(f"Could not extract the restore bundle: {err.strip() or out.strip()}")
        job.progress_current += 1
        job.log("Extraction complete.")
        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        job.status = RestoreStatus.VERIFYING
        job.log("Verifying every restored file against the embedded manifest.")
        if guest_os_family == "windows":
            manifest_guest_path = ntpath.join(job.destination, restore_bundle.MANIFEST_NAME)
        else:
            manifest_guest_path = posixpath.join(job.destination, restore_bundle.MANIFEST_NAME)
        verify_argv = restore_bundle.build_verify_command(manifest_guest_path, job.destination, guest_os_family)
        exitcode, out, err = await _exec(
            job, verify_argv, timeout_seconds=settings.restore_long_running_exec_timeout_seconds
        )
        job.progress_current += 1
        if exitcode != 0:
            jobs.mark_failed(
                job.id,
                "Restore completed but per-file checksum verification failed - one or more files may "
                "not match the backed-up originals.",
            )
            return
        job.log(f"Checksum verified for all {len(manifest)} restored file(s) - matches the source.")

        # The manifest is a restore-mechanism artifact, not part of the
        # original backup - confirmed live 2026-09-02 that it was
        # otherwise left behind in the destination directory permanently.
        # Best-effort: verification already succeeded, so a cleanup
        # failure here shouldn't fail the whole restore, just gets a
        # log line instead.
        if guest_os_family == "windows":
            cleanup_script = f"Remove-Item -LiteralPath '{manifest_guest_path}' -Force -ErrorAction SilentlyContinue"
            cleanup_argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cleanup_script]
        else:
            cleanup_argv = ["rm", "-f", manifest_guest_path]
        cleanup_exitcode, _out, cleanup_err = await _exec(job, cleanup_argv)
        if cleanup_exitcode != 0:
            job.log(
                f"Note: could not remove the restore manifest file - harmless, left behind ({cleanup_err.strip()})."
            )

        jobs.mark_done(job.id)
    except asyncio.CancelledError:
        jobs.mark_cancelled(job.id)
        raise
    except httpx.HTTPStatusError as exc:
        jobs.mark_failed(job.id, _pve_error_message(exc))
    except Exception as exc:  # last-resort guard - a job must never hang "running" forever
        jobs.mark_failed(job.id, str(exc))
    finally:
        if scratch_dir is not None:
            await _remove_scratch_dir(job, guest_os_family, scratch_dir)
        if tmp_dir_ctx is not None:
            tmp_dir_ctx.cleanup()


async def _run_single_file_restore(job: RestoreJob, jobs: RestoreJobManager) -> None:
    """Downloads the source file from file-restore, then writes it - via
    a single agent/file-write call when the content fits in one chunk
    and neither metadata restore nor verify was requested, otherwise via
    Direct Network Transfer (when eligible) or the scratch-file/
    guest-exec/concatenate path (which also handles the optional
    metadata/verify steps). The chunked write path converts one slice of
    the already-downloaded bytes to its wire string at a time rather
    than pre-building the whole file's worth up front - see
    restore_chunking.py's module docstring for why. Runs as a background
    asyncio task (docs/plan.md §7.5's session-handling section:
    job.session is this job's own ticket snapshot, refreshed here
    independently of the interactive session)."""
    scratch_dir: str | None = None
    guest_os_family: str | None = None
    try:
        job.status = RestoreStatus.RUNNING
        job.log(f"Starting restore of {job.source!r} -> {job.destination!r}.")
        await ensure_fresh_ticket(job.session)

        hasher = hashlib.sha256() if job.verify else None

        client, response = await pve_client.open_download(
            job.session, job.source_volume, job.source_filepath, tar=False
        )
        try:
            # Read just enough (at most two pieces) to know whether this
            # is the small, single-call case, without buffering the rest
            # of a possibly-large file just to find out. See
            # restore_chunking.py's module docstring for why this
            # matters: buffering the whole file up front - even just
            # once, let alone the old double-buffering - OOM-killed a
            # memory-constrained deployment on a real large file.
            byte_iter = response.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE_BYTES)
            first_piece = None
            async for piece in byte_iter:
                first_piece = piece
                break
            if first_piece is None:
                first_piece = b""
            second_piece = None
            async for piece in byte_iter:
                second_piece = piece
                break
            has_more = second_piece is not None

            if job.cancel_requested:
                jobs.mark_cancelled(job.id)
                return

            if not has_more:
                job.log(f"Downloaded {len(first_piece)} byte(s) from the backup.")
                needs_exec = job.restore_metadata or job.verify
                job.progress_total = 1 + (1 if job.restore_metadata else 0) + (1 if job.verify else 0)

                if not needs_exec:
                    job.log("Fits in one call and no metadata/verify requested - writing directly, no guest-exec.")
                    await ensure_fresh_ticket(job.session)
                    await pve_client.write_guest_file(
                        job.session, job.guest_type, job.vmid, job.destination, bytes_to_wire_str(first_piece)
                    )
                    job.progress_current = 1
                    jobs.mark_done(job.id)
                    return

                # Everything past this point talks to guest-exec, so the
                # destination has to be safe to embed in a shell/
                # PowerShell command string (concatenation, LastWriteTime,
                # certutil aren't all pure-argv invocations the way
                # file-write is) - checked once up front rather than at
                # each individual step.
                pve_client.check_path_safe(job.destination)

                job.log("Checking VM.GuestAgent.Unrestricted availability (needed for guest-exec).")
                await ensure_fresh_ticket(job.session)
                caps = await guest_agent.get_restore_capabilities(job.session, job.guest_type, job.vmid)
                if not caps.design_b.available:
                    jobs.mark_failed(
                        job.id,
                        caps.design_b.reason
                        or "This restore needs guest-exec (restore metadata or verify was "
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

                if hasher is not None:
                    hasher.update(first_piece)
                await ensure_fresh_ticket(job.session)
                await pve_client.write_guest_file(
                    job.session, job.guest_type, job.vmid, job.destination, bytes_to_wire_str(first_piece)
                )
                job.progress_current += 1
                job.log("Wrote the file directly (single chunk, exec still needed for a later step).")
            else:
                job.log("Content needs more than one chunk (over the single-call size limit).")
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

                # A growing/placeholder total - the real chunk count
                # isn't known until the source is exhausted (streamed,
                # not pre-downloaded - see above). Refined as chunks are
                # actually written, finalized once the count is known.
                job.progress_total = 2 + (1 if job.restore_metadata else 0) + (1 if job.verify else 0)

                pieces = _iter_download_pieces(first_piece, second_piece, byte_iter)
                if await _try_direct_network_transfer(job, guest_os_family):
                    # Direct Network Transfer (docs/plan.md §7.6, issue
                    # #22): the guest fetches the file itself, straight
                    # from PVE - so the rest of this already-open stream
                    # (only read this far to determine chunking) is
                    # drained and discarded, never written anywhere by
                    # this process. Counts as one progress unit.
                    total_bytes = await _drain_and_hash(pieces, hasher)
                    job.log(f"Downloaded {total_bytes} byte(s) from the backup.")
                    job.progress_total = 1 + (1 if job.restore_metadata else 0) + (1 if job.verify else 0)
                    job.progress_current = 1
                else:
                    scratch_dir = scratch_dir_path(guest_os_family, job.id)
                    job.log(f"Creating scratch directory {scratch_dir!r} in the guest.")
                    await _create_scratch_dir(job, guest_os_family, scratch_dir)
                    chunk_paths, total_bytes = await _write_chunks_to_scratch(
                        job, guest_os_family, scratch_dir, pieces, hasher
                    )
                    job.log(f"Downloaded {total_bytes} byte(s) from the backup.")
                    if job.cancel_requested:
                        jobs.mark_cancelled(job.id)
                        return
                    job.progress_total = (
                        len(chunk_paths) + 1 + (1 if job.restore_metadata else 0) + (1 if job.verify else 0)
                    )
                    job.log(f"Wrote all {len(chunk_paths)} chunk(s) to scratch; concatenating into the destination.")
                    await _concat_chunks(job, chunk_paths, job.destination, guest_os_family)
                    await _verify_destination_exists(job, guest_os_family)
                    job.progress_current += 1
                    job.log("Concatenation complete.")
        finally:
            await response.aclose()
            await client.aclose()

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
            expected = hasher.hexdigest()
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
