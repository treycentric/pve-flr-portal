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
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


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
    requested_by: str
    guest_type: str
    vmid: str
    guest_label: str
    task_name: str
    snapshot_time: str  # "restore ver." - the backup snapshot this restores from
    source: str  # display path within the backup
    destination: str  # dest_dir (+ filename for a single file)
    strategy: str  # "quick" | "full"
    restore_metadata: bool = False
    verify: bool = False
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device": self.guest_label,
            "task_name": self.task_name,
            "restore_version": self.snapshot_time,
            "source": self.source,
            "destination": self.destination,
            "status": self.status.value,
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
        requested_by: str,
        guest_type: str,
        vmid: str,
        guest_label: str,
        task_name: str,
        snapshot_time: str,
        source: str,
        destination: str,
        strategy: str,
        restore_metadata: bool = False,
        verify: bool = False,
    ) -> RestoreJob:
        job = RestoreJob(
            id=str(uuid.uuid4()),
            requested_by=requested_by,
            guest_type=guest_type,
            vmid=vmid,
            guest_label=guest_label,
            task_name=task_name,
            snapshot_time=snapshot_time,
            source=source,
            destination=destination,
            strategy=strategy,
            restore_metadata=restore_metadata,
            verify=verify,
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
