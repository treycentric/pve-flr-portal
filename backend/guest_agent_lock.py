"""PH.5: serializes every guest-agent command per vmid, and retries the
ones worth retrying (docs/plan.md §7.5). `qemu-guest-agent` accepts only
one command at a time over its virtio-serial channel - two overlapping
commands (a retry racing the original, two browser tabs, a capability
check landing mid-browse) can desync that channel until the guest's
agent is restarted, which is what a naive retry-after-timeout did in
practice. Wrapping every actual agent/* HTTP call (not the plain PVE API
calls like /config or /access/permissions, which aren't guest-agent
commands and don't share the channel) in this per-vmid lock closes that
whole class of problem structurally, rather than special-casing each
caller.

The lock alone only protects against overlap from *this application's*
own requests, though - it can't stop something else entirely (a
scheduled PBS backup's fs-freeze, another admin's `qm agent` call, the
Proxmox web UI) from being mid-command on the same channel at the same
moment. PVE/QEMU surfaces that kind of contention as a definite HTTP
error response, not silent corruption or an ambiguous timeout, so
`call_with_retries()` retries specifically on `httpx.HTTPStatusError`
(never on a timeout/connection error - see its docstring) to ride out
that legitimate, externally-caused busy window.

Different vmids are independent channels and never block each other.

Preventing overlap is a correctness concern; not monopolizing the
channel once it's free is a separate, courtesy concern -
`guest_agent_command()` also enforces `settings.
guest_agent_min_command_gap_seconds` (default 0, i.e. off) as a minimum
gap between the end of this app's previous command on a guest and the
start of its next one, so a scheduled PBS backup's fs-freeze or another
admin's `qm agent` call still gets a turn even while this app is busy -
most relevant once a multi-chunk restore is sending many sequential
commands back to back, not just today's single-write case.
"""
import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

import httpx

from .config import settings

_locks: dict[str, asyncio.Lock] = {}
_last_released_at: dict[str, float] = {}

_T = TypeVar("_T")

DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0


def _lock_for(vmid: str) -> asyncio.Lock:
    lock = _locks.get(vmid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[vmid] = lock
    return lock


@contextlib.asynccontextmanager
async def guest_agent_command(vmid: str) -> AsyncIterator[None]:
    """Hold this for the full request/response cycle of one guest-agent
    command (a single agent/info call; the whole exec-then-poll-
    exec-status sequence for one guest-exec invocation) - never partially,
    and never held across multiple independent commands, so the channel
    is always left in a clean state at each release point."""
    async with _lock_for(vmid):
        gap = settings.guest_agent_min_command_gap_seconds
        if gap > 0:
            last = _last_released_at.get(vmid)
            if last is not None:
                remaining = gap - (time.monotonic() - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        try:
            yield
        finally:
            if gap > 0:
                _last_released_at[vmid] = time.monotonic()


async def call_with_retries(
    factory: Callable[[], Awaitable[_T]],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
) -> _T:
    """Runs `factory()` - a zero-arg callable returning a *fresh* awaitable
    on each call, since a single coroutine object can't be awaited twice -
    retrying only on `httpx.HTTPStatusError` (see module docstring for
    why: a completed error response is a legitimate, safe-to-retry signal
    that something else was using the channel, unlike a timeout, which
    doesn't prove the in-guest command was actually abandoned and is
    never retried here). Re-raises the last error once attempts are
    exhausted; any other exception (including a timeout) propagates
    immediately on its first occurrence, no retry."""
    for attempt in range(attempts):
        try:
            return await factory()
        except httpx.HTTPStatusError:
            if attempt + 1 < attempts:
                await asyncio.sleep(delay_seconds)
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def clear() -> None:
    """Test-only: drops all tracked locks and pacing state between tests."""
    _locks.clear()
    _last_released_at.clear()
