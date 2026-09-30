"""PH.6 (issue #109 re-scope, docs/plan.md §6): a lazily-populated
on-disk cache for `file-restore/list` results.

Keyed by `(username, volume, path)` - **not** just `(volume, path)` as
originally sketched in docs/plan.md §6, matching
`pve_client.list_path()`'s existing in-flight-call-coalescing threat
model: file-restore access is permission-gated per PVE ticket, so
sharing a cached listing across users without keying by username would
let a second user see data PVE never actually authorized for them
(different users can hold different ACL grants on different
storages/VMs). Caching only ever saves a user their *own* repeat calls.

Backups are immutable once taken, so a cached listing never goes stale
on its own - but the snapshot it was cached from can still disappear
out from under it (PBS retention pruning, or a manual delete), and a
user's own access to it can be revoked after the fact. Nothing about
"immutable content" implies "safe to keep forever": `evict_missing()`
is the actual invalidation path, reconciling a user's cached volumes
against the live, permission-filtered list `pve_client.list_backup_archives()`
already fetches on every page load - no separate sweep/background job
needed, and it doubles as a fix for a real gap a cache hit would
otherwise have: `get()` never re-checks permission on its own (it just
trusts the row), so without this, a user whose access to a guest was
revoked could still read out everything they cached from it before,
forever. `fetched_at` is kept in case a future purely-time-based sweep
is ever wanted too, but isn't used for that today.

Single SQLite file under PFR_DATA_DIR (issue #30), no background job,
per CLAUDE.md's "no extra services" constraint. Uses stdlib `sqlite3`
via `asyncio.to_thread` rather than adding an async-sqlite dependency -
these are tiny single-row reads/writes, not worth a new requirement.
"""
import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .config import ensure_data_dir

_DB_FILENAME = "dir_cache.sqlite3"
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """`check_same_thread=False` since `asyncio.to_thread` may run
    different calls on different worker threads - `_lock` (not asyncio's
    Lock, since this code runs in worker threads, not the event loop)
    serializes actual access to the shared connection, since sqlite3
    connections aren't safe for concurrent same-instant use across
    threads even with that flag."""
    global _conn
    if _conn is None:
        db_path: Path = ensure_data_dir() / _DB_FILENAME
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dir_cache (
              username      TEXT NOT NULL,
              volume        TEXT NOT NULL,
              path          TEXT NOT NULL,
              listing_json  TEXT NOT NULL,
              fetched_at    TEXT NOT NULL,
              PRIMARY KEY (username, volume, path)
            )
            """
        )
        _conn.commit()
    return _conn


def _get_sync(username: str, volume: str, path: str) -> list[dict] | None:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT listing_json FROM dir_cache WHERE username = ? AND volume = ? AND path = ?",
            (username, volume, path),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _set_sync(username: str, volume: str, path: str, listing: list[dict]) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO dir_cache (username, volume, path, listing_json, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, volume, path, json.dumps(listing), datetime.now(UTC).isoformat()),
        )
        conn.commit()


async def get(username: str, volume: str, path: str) -> list[dict] | None:
    return await asyncio.to_thread(_get_sync, username, volume, path)


async def set(username: str, volume: str, path: str, listing: list[dict]) -> None:
    await asyncio.to_thread(_set_sync, username, volume, path, listing)


def _evict_missing_sync(username: str, existing_volumes: frozenset[str]) -> None:
    with _lock:
        conn = _get_conn()
        if not existing_volumes:
            # NOT IN () with no values is invalid SQL - and correct
            # anyway: no visible archives at all means nothing of this
            # user's should stay cached.
            conn.execute("DELETE FROM dir_cache WHERE username = ?", (username,))
        else:
            placeholders = ",".join("?" * len(existing_volumes))
            conn.execute(
                f"DELETE FROM dir_cache WHERE username = ? AND volume NOT IN ({placeholders})",
                (username, *existing_volumes),
            )
        conn.commit()


async def evict_missing(username: str, existing_volumes: frozenset[str]) -> None:
    """Drops every cached row for `username` whose volume isn't in
    `existing_volumes` - the caller's own current, permission-filtered
    view of what actually exists (main.py's `index()`, right after its
    own `list_backup_archives()` call). Deliberately scoped to just this
    one user's rows: a different user's own cache reflects their own
    visibility and access, and must never be touched by this one."""
    await asyncio.to_thread(_evict_missing_sync, username, existing_volumes)


def clear() -> None:
    """Test-only: deletes every cached row, same leak-between-tests
    convention as this codebase's other backend modules' clear()
    helpers (e.g. auth._sessions.clear())."""
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM dir_cache")
        conn.commit()
