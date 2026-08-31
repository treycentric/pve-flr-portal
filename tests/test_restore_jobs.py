import asyncio

import pytest

from backend.restore_jobs import RestoreJobManager, RestoreStatus


@pytest.fixture
def manager():
    m = RestoreJobManager()
    yield m
    m.clear()


def _make(manager, **overrides):
    defaults = dict(
        requested_by="alice@pam",
        guest_type="qemu",
        vmid="133",
        guest_label="web (133)",
        task_name="Restore 2026-08-30 14:48 -> /etc",
        snapshot_time="2026-08-30T14:48:06Z",
        source="/etc/hosts",
        destination="/etc",
        strategy="quick",
    )
    defaults.update(overrides)
    return manager.create(**defaults)


def test_create_assigns_id_and_queued_status(manager):
    job = _make(manager)
    assert job.id
    assert job.status == RestoreStatus.QUEUED
    assert job.is_active


def test_list_jobs_returns_newest_first(manager):
    job1 = _make(manager, task_name="first")
    job1.started_at -= 10  # force job1 to be older
    job2 = _make(manager, task_name="second")
    jobs = manager.list_jobs()
    assert [j.id for j in jobs] == [job2.id, job1.id]


def test_get_returns_none_for_unknown_id(manager):
    assert manager.get("does-not-exist") is None


def test_mark_done_sets_status_and_stops_elapsed_clock(manager, monkeypatch):
    import backend.restore_jobs as restore_jobs_module

    job = _make(manager)
    manager.mark_done(job.id)
    assert job.status == RestoreStatus.DONE
    assert not job.is_active
    elapsed_at_finish = job.elapsed_seconds

    # Advancing the clock after finish must not move elapsed_seconds -
    # it should be pinned to finished_at, not recomputed against "now".
    monkeypatch.setattr(restore_jobs_module.time, "time", lambda: job.finished_at + 100)
    assert job.elapsed_seconds == pytest.approx(elapsed_at_finish)


def test_mark_failed_records_error(manager):
    job = _make(manager)
    manager.mark_failed(job.id, "guest agent not responding")
    assert job.status == RestoreStatus.FAILED
    assert job.error == "guest agent not responding"
    assert not job.is_active


async def test_cancel_active_job_sets_flag_and_returns_true(manager):
    job = _make(manager)

    async def never_finishes(j):
        await asyncio.sleep(3600)

    manager.submit(job, never_finishes)
    assert manager.cancel(job.id) is True
    assert job.cancel_requested is True
    # submit()'s task was cancelled by cancel() above; let it settle so no
    # "Task exception was never retrieved" warning leaks into other tests.
    task = manager._tasks[job.id]
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancel_nonexistent_job_returns_false(manager):
    assert manager.cancel("nope") is False


def test_cancel_already_finished_job_returns_false(manager):
    job = _make(manager)
    manager.mark_done(job.id)
    assert manager.cancel(job.id) is False


def test_mark_cancelled_sets_status(manager):
    job = _make(manager)
    manager.mark_cancelled(job.id)
    assert job.status == RestoreStatus.CANCELLED
    assert not job.is_active


def test_to_dict_shape_matches_ui_columns(manager):
    job = _make(manager)
    d = job.to_dict()
    assert set(d) == {
        "id",
        "device",
        "task_name",
        "restore_version",
        "source",
        "destination",
        "status",
        "elapsed_seconds",
        "error",
        "cancellable",
    }
    assert d["device"] == "web (133)"
    assert d["cancellable"] is True


async def test_submitted_job_actually_runs(manager):
    job = _make(manager)
    ran = asyncio.Event()

    async def do_work(j):
        ran.set()
        manager.mark_done(j.id)

    manager.submit(job, do_work)
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await asyncio.sleep(0)  # let mark_done's task finish
    assert job.status == RestoreStatus.DONE
