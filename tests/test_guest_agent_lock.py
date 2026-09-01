import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from backend import guest_agent_lock
from backend.guest_agent_lock import call_with_retries, guest_agent_command


async def test_same_vmid_serializes_overlapping_commands():
    order = []

    async def worker(name, delay):
        async with guest_agent_command("133"):
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    # "first" holds the lock for longer than "second" waits before
    # starting - if they weren't serialized, "second-start" would land
    # between "first-start" and "first-end".
    await asyncio.gather(worker("first", 0.05), worker("second", 0))
    assert order == ["first-start", "first-end", "second-start", "second-end"]


async def test_different_vmids_do_not_block_each_other():
    order = []

    async def worker(vmid, name, delay):
        async with guest_agent_command(vmid):
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    await asyncio.gather(worker("133", "a", 0.05), worker("999", "b", 0))
    # "b" (a different vmid) finishes without waiting on "a"'s lock.
    assert order[0] == "a-start"
    assert order[1] == "b-start"
    assert order.index("b-end") < order.index("a-end")


async def test_lock_is_released_after_an_exception():
    class Boom(Exception):
        pass

    with_lock_raised = False
    try:
        async with guest_agent_command("133"):
            raise Boom
    except Boom:
        with_lock_raised = True
    assert with_lock_raised

    # Must still be acquirable afterwards - not left held by the failed attempt.
    acquired = False
    async with guest_agent_command("133"):
        acquired = True
    assert acquired


def test_clear_drops_tracked_locks():
    guest_agent_lock._lock_for("133")
    assert "133" in guest_agent_lock._locks
    guest_agent_lock.clear()
    assert guest_agent_lock._locks == {}


def _fake_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "http://x"))


# --- call_with_retries ----------------------------------------------------


async def test_call_with_retries_returns_on_first_success():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "ok"

    assert await call_with_retries(factory) == "ok"
    assert calls == 1


async def test_call_with_retries_retries_on_http_status_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", _fake_sleep)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        if calls < 3:
            _fake_response(500).raise_for_status()
        return "ok"

    assert await call_with_retries(factory, attempts=3) == "ok"
    assert calls == 3


async def test_call_with_retries_reraises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", _fake_sleep)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        _fake_response(500).raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        await call_with_retries(factory, attempts=3)
    assert calls == 3


async def test_call_with_retries_does_not_retry_on_timeout():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("timed out")

    with pytest.raises(httpx.TimeoutException):
        await call_with_retries(factory, attempts=3)
    assert calls == 1  # no retry - ambiguous whether the in-guest command completed


async def _fake_sleep(*_args, **_kwargs):
    return None


# --- pacing (guest_agent_min_command_gap_seconds) --------------------------


async def test_pacing_delays_the_next_command_on_the_same_vmid(monkeypatch):
    monkeypatch.setattr(guest_agent_lock, "settings", SimpleNamespace(guest_agent_min_command_gap_seconds=0.05))
    async with guest_agent_command("133"):
        pass
    start = time.monotonic()
    async with guest_agent_command("133"):
        pass
    assert time.monotonic() - start >= 0.04  # allow a little scheduling slack


async def test_pacing_disabled_by_default(monkeypatch):
    monkeypatch.setattr(guest_agent_lock, "settings", SimpleNamespace(guest_agent_min_command_gap_seconds=0.0))
    start = time.monotonic()
    async with guest_agent_command("133"):
        pass
    async with guest_agent_command("133"):
        pass
    assert time.monotonic() - start < 0.04  # no artificial delay when the gap is 0


async def test_pacing_does_not_apply_across_different_vmids(monkeypatch):
    monkeypatch.setattr(guest_agent_lock, "settings", SimpleNamespace(guest_agent_min_command_gap_seconds=0.5))
    async with guest_agent_command("133"):
        pass
    start = time.monotonic()
    async with guest_agent_command("999"):  # a different guest - its own pacing clock
        pass
    assert time.monotonic() - start < 0.1
