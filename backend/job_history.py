"""Persisted restore job history and logs, with retention and restart
reconciliation. Separate SQLite file (`job_history.sqlite`, under
PFR_DATA_DIR) from PH.6's dir_cache.

Two tables: `jobs` (one row per job, RestoreJob.to_dict()'s fields as
columns) and an append-only `job_log_entries` child table (`job_id` FK,
`ON DELETE CASCADE`). Logging a line is a single small INSERT
regardless of how long the log already is; the list endpoint never
joins job_log_entries since it never shows log lines.

Writes are synchronous (not asyncio.to_thread-wrapped like dir_cache) -
job status transitions and log lines are infrequent enough per job that
a brief blocking write is an acceptable tradeoff. Reads use
asyncio.to_thread since they run from async routes/the startup hook.
"""
import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from .config import ensure_data_dir

_DB_FILENAME = "job_history.sqlite"
_ACTIVE_STATUSES_RAW = ("queued", "running", "verifying")
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

# Mirrors RestoreJob.to_dict()'s fields - progress_percent/elapsed_seconds/
# cancellable are stored as the already-computed values, not re-derived
# from raw state.
_JOB_COLUMNS = (
    "id",
    "requested_by",
    "device",
    "task_name",
    "restore_version",
    "source",
    "destination",
    "status",
    "progress_percent",
    "elapsed_seconds",
    "error",
    "cancellable",
    "started_at",
    "finished_at",
)


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path: Path = ensure_data_dir() / _DB_FILENAME
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # Off by default per connection even when the schema declares a
        # foreign key - required for ON DELETE CASCADE below to work.
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id                TEXT PRIMARY KEY,
              requested_by      TEXT NOT NULL,
              device            TEXT NOT NULL,
              task_name         TEXT NOT NULL,
              restore_version   TEXT NOT NULL,
              source            TEXT NOT NULL,
              destination       TEXT NOT NULL,
              status            TEXT NOT NULL,
              progress_percent  INTEGER,
              elapsed_seconds   REAL,
              error             TEXT,
              cancellable       INTEGER NOT NULL,
              started_at        REAL NOT NULL,
              finished_at       REAL,
              updated_at        REAL NOT NULL
            )
            """
        )
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_log_entries (
              id      INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
              line    TEXT NOT NULL
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_job_log_entries_job_id ON job_log_entries (job_id)")
        _conn.commit()
    return _conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """A `jobs` row in RestoreJob.to_dict()'s shape - SQLite has no real
    bool, so `cancellable` round-trips through 0/1."""
    d = {col: row[col] for col in _JOB_COLUMNS if col not in ("started_at", "finished_at")}
    d["cancellable"] = bool(d["cancellable"])
    return d


def persist_sync(job) -> None:
    """Upserts `job`'s current metadata into `jobs`. Never touches
    job_log_entries - that only grows via append_log_entry()."""
    d = job.to_dict()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO jobs (id, requested_by, device, task_name, restore_version, source, destination,
                               status, progress_percent, elapsed_seconds, error, cancellable,
                               started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              progress_percent = excluded.progress_percent,
              elapsed_seconds = excluded.elapsed_seconds,
              error = excluded.error,
              cancellable = excluded.cancellable,
              finished_at = excluded.finished_at,
              updated_at = excluded.updated_at
            """,
            (
                d["id"],
                d["requested_by"],
                d["device"],
                d["task_name"],
                d["restore_version"],
                d["source"],
                d["destination"],
                d["status"],
                d["progress_percent"],
                d["elapsed_seconds"],
                d["error"],
                int(d["cancellable"]),
                job.started_at,
                job.finished_at,
                time.time(),
            ),
        )
        conn.commit()


def append_log_entry(job_id: str, line: str) -> None:
    """Appends one log line. Called from RestoreJob.log() on every call."""
    with _lock:
        conn = _get_conn()
        conn.execute("INSERT INTO job_log_entries (job_id, line) VALUES (?, ?)", (job_id, line))
        conn.commit()


def _get_sync(job_id: str) -> dict | None:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        log_rows = conn.execute(
            "SELECT line FROM job_log_entries WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    detail = _row_to_dict(row)
    detail["log"] = [r["line"] for r in log_rows]
    return detail


async def get(job_id: str) -> dict | None:
    """A single persisted job's full detail, including its log - the
    detail endpoint's fallback once a job id leaves the in-memory
    RestoreJobManager."""
    return await asyncio.to_thread(_get_sync, job_id)


def _list_recent_sync(retention_days: int, exclude_ids: frozenset[str]) -> list[dict]:
    cutoff = time.time() - retention_days * 86400
    with _lock:
        conn = _get_conn()
        if not exclude_ids:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE finished_at IS NULL OR finished_at >= ?", (cutoff,)
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(exclude_ids))
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE (finished_at IS NULL OR finished_at >= ?) AND id NOT IN ({placeholders})",
                (cutoff, *exclude_ids),
            ).fetchall()
    # "_started_at" is for the caller's own sort/interleave with live
    # jobs - not part of the public job shape.
    return [{**_row_to_dict(row), "_started_at": row["started_at"]} for row in rows]


async def list_recent(retention_days: int, exclude_ids: frozenset[str]) -> list[dict]:
    """Persisted jobs within the retention window, minus any id in
    `exclude_ids` (the caller's own current in-memory job ids)."""
    return await asyncio.to_thread(_list_recent_sync, retention_days, exclude_ids)


def _evict_expired_sync(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
        conn.commit()


async def evict_expired(retention_days: int) -> None:
    """Opportunistic sweep, not a scheduled background job. Called from
    the job list endpoint on every load."""
    await asyncio.to_thread(_evict_expired_sync, retention_days)


def _reconcile_interrupted_sync() -> list[str]:
    now = time.time()
    with _lock:
        conn = _get_conn()
        placeholders = ",".join("?" * len(_ACTIVE_STATUSES_RAW))
        rows = conn.execute(
            f"SELECT id, elapsed_seconds FROM jobs WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES_RAW,
        ).fetchall()
        reconciled = [row["id"] for row in rows]
        for row in rows:
            conn.execute(
                """
                UPDATE jobs SET status = ?, progress_percent = NULL, cancellable = 0,
                                finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                ("interrupted", now, now, row["id"]),
            )
            elapsed = row["elapsed_seconds"] or 0
            conn.execute(
                "INSERT INTO job_log_entries (job_id, line) VALUES (?, ?)",
                (row["id"], f"+{elapsed}s Interrupted: the backend restarted while this job was in progress."),
            )
        conn.commit()
    return reconciled


async def reconcile_interrupted() -> list[str]:
    """Closes out any row still queued/running/verifying at startup as
    interrupted, before serving any request. Returns the ids reconciled."""
    return await asyncio.to_thread(_reconcile_interrupted_sync)


def clear() -> None:
    """Test-only."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM job_log_entries")
        conn.execute("DELETE FROM jobs")
        conn.commit()
