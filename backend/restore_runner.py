"""PH.5: runs a submitted RestoreJob's actual work (docs/plan.md §7.5).
Content-only path only so far - a single `agent/file-write` call, no
`guest-exec` anywhere. The multi-chunk (scratch files + concat) and
metadata/verify paths land in a later change; a file that doesn't fit
in one chunk fails the job with a clear message rather than silently
falling back to something unbuilt.
"""
import asyncio

import httpx

from . import pve_client
from .auth import ensure_fresh_ticket
from .restore_chunking import needs_guest_exec, split_into_chunks
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


async def run_content_only_restore(job: RestoreJob, jobs: RestoreJobManager) -> None:
    """Streams the source file from file-restore, and - only if it fits
    in a single agent/file-write call - writes it straight to the
    destination. Runs as a background asyncio task (docs/plan.md §7.5's
    session-handling section: job.session is this job's own ticket
    snapshot, refreshed here independently of the interactive session)."""
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
        if needs_guest_exec(chunks):
            jobs.mark_failed(
                job.id,
                "File is too large for a quick restore (this build only writes "
                "content that fits in one chunk - larger/multi-chunk restore "
                "isn't implemented yet).",
            )
            return

        if job.cancel_requested:
            jobs.mark_cancelled(job.id)
            return

        await ensure_fresh_ticket(job.session)
        await pve_client.write_guest_file(
            job.session, job.guest_type, job.vmid, job.destination, chunks[0].content
        )
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
