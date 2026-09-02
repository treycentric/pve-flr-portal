"""Design C (docs/plan.md §7.6, issue #22): the single-use, short-TTL
download token a Design C bootstrap script fetches its file against.

The guest must never see the operator's PVE ticket - that's a live
credential, and the whole point of running restores through this app in
the first place is that guests never hold one. Instead the backend mints
a random token scoped to exactly one restore job, with a short lifetime
(`RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS`), and the download endpoint
(backend.main) consumes it on first use - a second attempt with the same
token, or one presented after its TTL expires, gets nothing. Never a
standing, reusable, or broadly-scoped credential.

Same in-memory, single-process tradeoff already accepted for
auth._sessions and restore_jobs.manager (CLAUDE.md - no extra services):
a backend restart invalidates every outstanding token, same as it drops
every session and every job.
"""
import secrets
import time
from dataclasses import dataclass

from .config import settings


@dataclass
class DownloadToken:
    job_id: str
    expires_at: float
    local_path: str | None = None
    """Set only for a bundle restore (docs/plan.md §7.7, issue #24):
    when present, the download endpoint serves this already-built local
    bundle file directly instead of re-proxying from PVE via
    job_id/source_volume/source_filepath - a bundle isn't something
    PVE's file-restore API can hand back as one item, since this app
    synthesizes it locally from multiple downloaded items."""


_tokens: dict[str, DownloadToken] = {}


def mint_token(job_id: str, ttl_seconds: float | None = None, local_path: str | None = None) -> str:
    """Creates a new single-use token for this job, returning the token
    string. `ttl_seconds` defaults to the configured
    RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS - overridable per call mainly so
    tests don't need to fight a real clock. `local_path` - see
    DownloadToken's docstring - defaults to None (the ordinary
    single-file case: proxy from PVE)."""
    ttl = settings.restore_download_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    token = secrets.token_urlsafe(32)
    _tokens[token] = DownloadToken(job_id=job_id, expires_at=time.time() + ttl, local_path=local_path)
    return token


def consume_token_full(token: str) -> DownloadToken | None:
    """Single-use: looks the token up and immediately removes it,
    regardless of outcome, so a second call with the same token - replay,
    a retried request, whatever - always gets None. Returns the full
    DownloadToken (job_id and, for a bundle restore, local_path) on a
    valid, unexpired token; None otherwise. Safe without extra locking:
    this app runs the event loop single-threaded, and there's no `await`
    between the dict lookup and the pop, so nothing can interleave."""
    entry = _tokens.pop(token, None)
    if entry is None:
        return None
    if time.time() > entry.expires_at:
        return None
    return entry


def consume_token(token: str) -> str | None:
    """Same as consume_token_full(), but returns just the job_id - the
    original, still-used-elsewhere shape for callers that only need
    that (e.g. every existing test predates local_path)."""
    entry = consume_token_full(token)
    return entry.job_id if entry is not None else None


def clear() -> None:
    """Test/dev helper - mirrors auth._sessions.clear() and
    restore_jobs.RestoreJobManager.clear()'s role in tests."""
    _tokens.clear()
