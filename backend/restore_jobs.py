"""PH.5: in-memory registry of background restore jobs (docs/plan.md
§7.5). A restore (stream source -> N chunk writes -> optional concat ->
optional metadata -> optional verify) can run well past a reasonable
HTTP request lifetime, so `POST /api/restore` submits a job and returns
immediately; the actual work runs as a tracked asyncio task here.

Same tradeoff already accepted for auth._sessions (CLAUDE.md - no extra
services): single-process, in-memory, lost on a backend restart. Jobs
are visible to any logged-in user rather than scoped per-requester -
this is a single-admin homelab tool with one shared task list, the same
way Synology ABB's own restore-task list works.

**Session handling.** A job holds its own SessionData *snapshot*
(`dataclasses.replace(session)` at submission time), never the same
object the interactive `auth._sessions` entry points at. Two reasons:
a restore can outlive the browser session that started it (the whole
point of a fire-and-forget background job), and a PVE ticket isn't
revoked by our app's own logout/idle-eviction - it's a PVE-issued
credential valid on its own ~2h clock regardless of what our session
store does with it. The job's run loop calls
`auth.ensure_fresh_ticket()` on its own copy before each guest-agent
call, using the same staleness policy `get_session()` applies to
interactive requests, so a long-running job keeps its ticket current
without depending on the user making another browser request.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .auth import SessionData


class RestoreStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = (RestoreStatus.QUEUED, RestoreStatus.RUNNING, RestoreStatus.VERIFYING)


@dataclass
class RestoreJob:
    id: str
    session: SessionData  # job's own snapshot - see module docstring
    requested_by: str  # display-only; session.username is the credential
    guest_type: str
    vmid: str
    guest_label: str
    task_name: str
    snapshot_time: str  # "restore ver." - the backup snapshot this restores from
    source_volume: str  # backup volid, for re-fetching the content to restore
    source_filepath: str  # opaque file-restore filepath token (docs/plan.md §3)
    source: str  # display path within the backup
    destination: str  # dest_dir (+ filename for a single file)
    # Independent opt-ins, not a "quick vs full" choice - the content
    # write path (single call vs chunk+concat) is decided automatically
    # from size; whether guest-exec ends up needed follows from that PLUS
    # either of these being requested, whenever the guest/privileges
    # support it (docs/plan.md §7.5).
    restore_metadata: bool = False
    verify: bool = False
    # The source file's original mtime (from file-restore/list, which is
    # the only piece of metadata that API actually exposes - no uid/gid/
    # mode field exists on any PVE version, docs/plan.md §7.5). Only
    # meaningful when restore_metadata is set; None if the frontend
    # didn't have an mtime to send (e.g. a directory entry).
    source_mtime: int | None = None
    # Coarse step-count progress, not byte-level - one unit per chunk
    # written, plus one each for concatenation/metadata restore/verify
    # when those run (restore_runner.py sets progress_total once the
    # actual step count is known; the single-call fast path is just
    # total=1). Good enough to show "running (42%)" without the added
    # complexity of tracking partial-chunk upload progress.
    progress_current: int = 0
    progress_total: int = 1
    status: RestoreStatus = RestoreStatus.QUEUED
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def progress_percent(self) -> int | None:
        # Only meaningful while actually in progress - a finished job
        # (done/failed/cancelled) shows its terminal state, not a
        # possibly-partial percentage frozen at whatever point it stopped.
        if not self.is_active or self.progress_total <= 0:
            return None
        return max(0, min(100, round(100 * self.progress_current / self.progress_total)))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device": self.guest_label,
            "task_name": self.task_name,
            "restore_version": self.snapshot_time,
            "source": self.source,
            "destination": self.destination,
            "status": self.status.value,
            "progress_percent": self.progress_percent,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
            "cancellable": self.is_active,
        }


class RestoreJobManager:
    """Not a singleton by design - tests construct their own instance;
    backend.main holds the process-wide one."""

    def __init__(self) -> None:
        self._jobs: dict[str, RestoreJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def create(
        self,
        *,
        session: SessionData,
        guest_type: str,
        vmid: str,
        guest_label: str,
        task_name: str,
        snapshot_time: str,
        source_volume: str,
        source_filepath: str,
        source: str,
        destination: str,
        restore_metadata: bool = False,
        verify: bool = False,
        source_mtime: int | None = None,
    ) -> RestoreJob:
        # A distinct copy, not the same object the interactive session
        # store points at - see module docstring. Done here, not left to
        # the caller, so this decoupling can't be forgotten at a call site.
        job_session = replace(session)
        job = RestoreJob(
            id=str(uuid.uuid4()),
            session=job_session,
            requested_by=session.username,
            guest_type=guest_type,
            vmid=vmid,
            guest_label=guest_label,
            task_name=task_name,
            snapshot_time=snapshot_time,
            source_volume=source_volume,
            source_filepath=source_filepath,
            source=source,
            destination=destination,
            restore_metadata=restore_metadata,
            verify=verify,
            source_mtime=source_mtime,
        )
        self._jobs[job.id] = job
        return job

    def submit(self, job: RestoreJob, coro_factory) -> None:
        """Launches the job's background coroutine. Split from create() so
        tests can exercise job bookkeeping without ever running real async
        work; coro_factory takes the job and returns an awaitable."""
        self._tasks[job.id] = asyncio.create_task(coro_factory(job))

    def get(self, job_id: str) -> RestoreJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[RestoreJob]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.is_active:
            return False
        job.cancel_requested = True
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    def mark_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = RestoreStatus.CANCELLED
            job.finished_at = time.time()

    def mark_done(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = RestoreStatus.DONE
            job.finished_at = time.time()

    def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = RestoreStatus.FAILED
            job.error = error
            job.finished_at = time.time()

    def clear(self) -> None:
        """Test/dev helper - mirrors auth._sessions.clear()'s role in tests."""
        self._jobs.clear()
        self._tasks.clear()


# Process-wide instance backend.main uses. Tests construct their own
# RestoreJobManager() instances instead of touching this one directly,
# same convention as auth._sessions (a fixture clears it between tests
# that do use it via the FastAPI app, see conftest.py).
manager = RestoreJobManager()
