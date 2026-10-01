"""Issue #124: persisted restore job history + logs, with retention and
restart reconciliation.

`backend/restore_jobs.py`'s `RestoreJobManager` is deliberately
in-memory-only (same tradeoff as `auth._sessions`, per CLAUDE.md) - a
backend restart loses every job, including ones that were still
running. This module adds a durable record alongside that in-memory
one, in its own SQLite file (`job_history.sqlite`, **not** folded into
PH.6's `dir_cache.sqlite3`): different lifecycle (a job record outlives
the process; a directory listing is re-fetchable on demand) and
different sensitivity (a job's log/source/destination can carry real
filesystem paths out of a guest; a directory listing is scoped to what
the user could already browse to anyway). SQLite files are cheap - no
reason to share one for unrelated concerns.

**Two tables, not one JSON blob (2026-10-01 redesign, prompted by a
user review question).** The first cut of this module stored each
job's entire `to_detail_dict()` snapshot - including its whole log so
far - as one JSON blob, rewritten in full on every `RestoreJob.log()`
call. That gets strictly more expensive to rewrite as a job's log
grows, and forces every list-endpoint read to fetch and parse a job's
full log just to strip it back out again (the list view never shows
log lines). A normalized `jobs` row (one per job, the `to_dict()`
fields as real columns) plus an append-only `job_log_entries` child
table (one row per log line, `job_id` FK) fixes both: logging a line is
a single small `INSERT`, independent of how long the log already is,
and the list endpoint's query never touches `job_log_entries` at all.

**Write timing.** A `jobs` row is upserted at job creation (status
`queued`) and at every coarse status transition (`running`/
`verifying`/terminal) - see `RestoreJobManager.create()`/
`mark_running()`/`mark_verifying()`/`mark_done()`/`mark_failed()`/
`mark_cancelled()` in `restore_jobs.py`. A `job_log_entries` row is
appended on every `RestoreJob.log()` call, independently of those
status writes - so an interrupted job's persisted log is current up to
the last line actually logged, not just up to its last status change.
Both are safe at the volume they actually see: every loop in
`restore_runner.py` that calls `log()` already throttles itself to
roughly one line per percentage point or every few seconds (the
chunked-write heartbeat, the bundle-download progress callback) - never
one call per chunk or byte - so even a large, long-running transfer
logs on the order of dozens to ~150 times, not thousands. Deliberately
**not** written on every `progress_current` tick itself (a chunked
transfer can take hundreds of chunks, and nothing calls `log()` for
each one) - that field only matters live, while this process's own
in-memory copy is what's being read; once that copy is gone (a
restart), the job is necessarily terminal already (see
`reconcile_interrupted()` below) and a frozen last-known percentage is
fine.

**Writes are synchronous, not `asyncio.to_thread`-wrapped like PH.6's
`dir_cache`.** `dir_cache` offloads because it's a hot path - every
directory browse, for every user, calls it. A restore job's `log()`
calls and status transitions are bounded to roughly dozens over a
job's entire lifetime (see above), and each is now a small, single-row
write (not a growing blob rewrite), so a brief synchronous SQLite write
on the event loop thread is an acceptable, bounded cost here - and
avoids threading `async`/`await` through dozens of call sites across
`restore_runner.py` for no real benefit. Reads (the job list's history
merge, retention sweep, startup reconciliation) do use
`asyncio.to_thread`, since those run from `main.py`'s async routes and
startup hook where blocking the loop matters more (a slow query would
stall every other concurrent request, not just this one job's own
transition).
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

# The exact set of columns `jobs` stores - deliberately the same fields
# RestoreJob.to_dict() already produces (the frozen, computed-at-write-
# time values: progress_percent/elapsed_seconds/cancellable are
# properties on RestoreJob, not raw state - storing their computed
# values here, same as the old JSON-blob design did, avoids duplicating
# RestoreJob's own clamping/pinning logic a second time on the read
# side) plus the bookkeeping this module needs on top (started_at/
# finished_at for sorting and retention).
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
        # Off by default per connection in sqlite3 even when the schema
        # declares a foreign key - without this, ON DELETE CASCADE below
        # is silently a no-op and evict_expired() would orphan rows in
        # job_log_entries forever.
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
    """A `jobs` row, in the exact shape `RestoreJob.to_dict()` produces -
    SQLite has no real bool, so `cancellable` round-trips through 0/1
    and needs converting back."""
    d = {col: row[col] for col in _JOB_COLUMNS if col not in ("started_at", "finished_at")}
    d["cancellable"] = bool(d["cancellable"])
    return d


def persist_sync(job) -> None:
    """Upserts `job`'s current metadata (the `to_dict()` shape) into
    `jobs` - never touches `job_log_entries`, which only ever grows via
    `RestoreJob.log()`'s own call below. Called directly (not via
    `asyncio.to_thread`) - see the module docstring for why that's an
    acceptable tradeoff here."""
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
    """A single-row, append-only insert - independent of how long the
    job's log already is, unlike the old rewrite-the-whole-blob design.
    Called directly from `RestoreJob.log()` on every call; see the
    module docstring for why that's safe at the volume `log()` sees."""
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
        # Deliberately never joins job_log_entries - the list view never
        # shows log lines, so there's nothing to fetch there at all.
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
    # "_started_at" is a real column here (unlike the old JSON-blob
    # design, which had to smuggle it in separately since to_dict() has
    # no raw timestamp field of its own) - still prefixed with an
    # underscore because it's for the caller's own sort/interleave with
    # live jobs, not part of the public job shape; main.py strips it
    # back out before serving.
    return [{**_row_to_dict(row), "_started_at": row["started_at"]} for row in rows]


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
        # ON DELETE CASCADE (+ PRAGMA foreign_keys above) takes the
        # matching job_log_entries rows with it - no separate DELETE
        # needed here.
        conn.execute("DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
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
            elapsed = row["elapsed_seconds"] or 0  # last known value - display only, not re-measured
            conn.execute(
                "INSERT INTO job_log_entries (job_id, line) VALUES (?, ?)",
                (row["id"], f"+{elapsed}s Interrupted: the backend restarted while this job was in progress."),
            )
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
        conn.execute("DELETE FROM job_log_entries")
        conn.execute("DELETE FROM jobs")
        conn.commit()
