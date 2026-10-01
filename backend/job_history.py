"""Issue #124: persisted restore job history + logs, with retention and
restart reconciliation.

`backend/restore_jobs.py`'s `RestoreJobManager` is deliberately
in-memory-only (same tradeoff as `auth._sessions`, per CLAUDE.md) - a
backend restart loses every job, including ones that were still
running. This module adds a durable record alongside that in-memory
one, in its own SQLite file (`job_history.sqlite`, **not** folded into
PH.6's `dir_cache.sqlite3`): different lifecycle (a job record outlives
the process; a directory listing is re-fetchable on demand) and
different sensitivity (a job's `log`/`source`/`destination` can carry
real filesystem paths out of a guest; a directory listing is scoped to
what the user could already browse to anyway). SQLite files are cheap -
no reason to share one for unrelated concerns.

**Write timing.** A row is upserted at job creation (status `queued`),
at every coarse status transition (`running`/`verifying`/terminal) -
see `RestoreJobManager.create()`/`mark_running()`/`mark_verifying()`/
`mark_done()`/`mark_failed()`/`mark_cancelled()` in `restore_jobs.py` -
**and on every `RestoreJob.log()` call**, so an interrupted job's
persisted log is current up to the last line actually logged, not just
up to its last status change. This is safe at the call volume `log()`
actually sees: every loop in `restore_runner.py` that calls it already
throttles itself to roughly one line per percentage point or every few
seconds (the chunked-write heartbeat, the bundle-download progress
callback) - never one call per chunk or byte - so even a large,
long-running transfer logs on the order of dozens to ~150 times, not
thousands. Deliberately **not** on every `progress_current` tick
itself (a chunked transfer can take hundreds of chunks, and nothing
calls `log()` for each one) - that field only matters live, while this
process's own in-memory copy is what's being read; once that copy is
gone (a restart), the job is necessarily terminal already (see
`reconcile_interrupted()` below) and a frozen last-known percentage is
fine. Each write stores a full `RestoreJob.to_detail_dict()` snapshot
(including `log_lines`) as one JSON blob, keyed by job id - simpler
than normalizing every field into its own column, and means reads need
no join or separate "fetch the log" query.

**Writes are synchronous, not `asyncio.to_thread`-wrapped like PH.6's
`dir_cache`.** `dir_cache` offloads because it's a hot path - every
directory browse, for every user, calls it. A restore job's `log()`
calls and status transitions are bounded to roughly dozens over a
job's entire lifetime (see above), so a brief synchronous SQLite write
on the event loop thread is an acceptable, bounded cost here, and
avoids threading `async`/`await` through dozens of call sites across
`restore_runner.py` for no real benefit. Reads (the job list's history
merge, retention sweep, startup reconciliation) do use
`asyncio.to_thread`, since those run from `main.py`'s async routes and
startup hook where blocking the loop
matters more (a slow query would stall every other concurrent request,
not just this one job's own transition).
"""
import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import ensure_data_dir

_DB_FILENAME = "job_history.sqlite"
_ACTIVE_STATUSES_RAW = ("queued", "running", "verifying")
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path: Path = ensure_data_dir() / _DB_FILENAME
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_history (
              id            TEXT PRIMARY KEY,
              requested_by  TEXT NOT NULL,
              status        TEXT NOT NULL,
              started_at    REAL NOT NULL,
              finished_at   REAL,
              detail_json   TEXT NOT NULL,
              updated_at    REAL NOT NULL
            )
            """
        )
        _conn.commit()
    return _conn


def persist_sync(job) -> None:
    """Upserts `job`'s current state. Called directly (not via
    `asyncio.to_thread`) from `RestoreJobManager`'s sync methods - see
    the module docstring for why that's an acceptable tradeoff here."""
    detail = job.to_detail_dict()
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO job_history (id, requested_by, status, started_at, finished_at, detail_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              finished_at = excluded.finished_at,
              detail_json = excluded.detail_json,
              updated_at = excluded.updated_at
            """,
            (
                job.id,
                job.requested_by,
                job.status.value,
                job.started_at,
                job.finished_at,
                json.dumps(detail),
                time.time(),
            ),
        )
        conn.commit()


def _get_sync(job_id: str) -> dict | None:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT detail_json FROM job_history WHERE id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row is not None else None


async def get(job_id: str) -> dict | None:
    """A single persisted job's full detail (including `log`) - the
    detail endpoint's fallback once a job id is no longer in the
    current process's in-memory `RestoreJobManager`."""
    return await asyncio.to_thread(_get_sync, job_id)


def _list_recent_sync(retention_days: int, exclude_ids: frozenset[str]) -> list[dict]:
    cutoff = time.time() - retention_days * 86400
    with _lock:
        conn = _get_conn()
        # A still-active row is never excluded by the cutoff (finished_at
        # IS NULL) - in practice this never matters by the time anything
        # calls list_recent(), since reconcile_interrupted() closes out
        # every non-terminal row at the start of this same process, but
        # the query stays correct even if that invariant were ever
        # violated rather than silently hiding a stuck-looking row.
        if not exclude_ids:
            rows = conn.execute(
                "SELECT detail_json, started_at FROM job_history WHERE finished_at IS NULL OR finished_at >= ?",
                (cutoff,),
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(exclude_ids))
            rows = conn.execute(
                f"SELECT detail_json, started_at FROM job_history "
                f"WHERE (finished_at IS NULL OR finished_at >= ?) AND id NOT IN ({placeholders})",
                (cutoff, *exclude_ids),
            ).fetchall()
    # `detail_json` (RestoreJob.to_detail_dict()) carries no raw
    # timestamp field of its own (elapsed_seconds is derived, not a
    # clock time) - "_started_at" is injected from the table's own
    # column purely so the caller can interleave these with live,
    # in-memory jobs in true start order. It's not part of the public
    # job shape; main.py strips it back out before serving.
    return [{**json.loads(r[0]), "_started_at": r[1]} for r in rows]


async def list_recent(retention_days: int, exclude_ids: frozenset[str]) -> list[dict]:
    """Persisted jobs within the retention window, in no particular
    order (each carries `_started_at` so the caller can sort/interleave
    them with live, in-memory jobs itself - main.py does), minus any id
    in `exclude_ids` - the caller's own current in-memory job ids, so a
    job this process is still tracking isn't double-counted against its
    own (older, pre-transition) persisted snapshot."""
    return await asyncio.to_thread(_list_recent_sync, retention_days, exclude_ids)


def _evict_expired_sync(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM job_history WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
        conn.commit()


async def evict_expired(retention_days: int) -> None:
    """Opportunistic sweep, not a scheduled background job - same
    pattern as `dir_cache.evict_missing()` (CLAUDE.md's "no background
    job" constraint). Called from the job list endpoint on every load;
    cheap even when there's nothing to delete."""
    await asyncio.to_thread(_evict_expired_sync, retention_days)


def _reconcile_interrupted_sync() -> list[str]:
    now = time.time()
    with _lock:
        conn = _get_conn()
        placeholders = ",".join("?" * len(_ACTIVE_STATUSES_RAW))
        rows = conn.execute(
            f"SELECT id, detail_json FROM job_history WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES_RAW,
        ).fetchall()
        reconciled = []
        for job_id, detail_json in rows:
            detail = json.loads(detail_json)
            elapsed = detail.get("elapsed_seconds", 0)  # last known value - display only, not re-measured
            detail["status"] = "interrupted"
            detail["progress_percent"] = None
            detail["cancellable"] = False
            detail.setdefault("log", []).append(
                f"+{elapsed}s Interrupted: the backend restarted while this job was in progress."
            )
            conn.execute(
                "UPDATE job_history SET status = ?, finished_at = ?, detail_json = ?, updated_at = ? WHERE id = ?",
                ("interrupted", now, json.dumps(detail), now, job_id),
            )
            reconciled.append(job_id)
        conn.commit()
    return reconciled


async def reconcile_interrupted() -> list[str]:
    """Called once at startup, before serving any request (`main.py`'s
    `lifespan`). Any row still `queued`/`running`/`verifying` at this
    point is necessarily left over from a previous process - nothing in
    the current process could have created an active job yet - so each
    one is closed out as `interrupted` rather than left looking
    perpetually in-progress forever. Returns the ids reconciled, for a
    one-line startup log message."""
    return await asyncio.to_thread(_reconcile_interrupted_sync)


def clear() -> None:
    """Test-only, same convention as dir_cache.clear()/auth._sessions.clear()."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM job_history")
        conn.commit()
