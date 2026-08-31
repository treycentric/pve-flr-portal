import asyncio

import pytest

from backend.restore_jobs import RestoreJobManager, RestoreStatus


@pytest.fixture
def manager():
    m = RestoreJobManager()
    yield m
    m.clear()


def _make(manager, session_data, **overrides):
    defaults = dict(
        session=session_data,
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        task_name="Restore 2026-08-30 14:48 -> /etc",
        snapshot_time="2026-08-30T14:48:06Z",
        source_volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        source_filepath="L2V0Yy9ob3N0cw==",
        source="/etc/hosts",
        destination="/etc",
    )
    defaults.update(overrides)
    return manager.create(**defaults)


def test_create_assigns_id_and_queued_status(manager, session_data):
    job = _make(manager, session_data)
    assert job.id
    assert job.status == RestoreStatus.QUEUED
    assert job.is_active


def test_create_snapshots_the_session_independently(manager, session_data):
    # The job must not share the same SessionData object the interactive
    # session store points at - see restore_jobs.py's module docstring:
    # a job outlives its browser session and refreshes its own ticket.
    job = _make(manager, session_data)
    assert job.session is not session_data
    assert job.session.ticket == session_data.ticket  # same initial values...
    job.session.ticket = "PVE:alice@pam:JOB-REFRESHED"
    assert session_data.ticket != job.session.ticket  # ...but independent after


def test_requested_by_reflects_session_username(manager, session_data):
    job = _make(manager, session_data)
    assert job.requested_by == session_data.username


def test_list_jobs_returns_newest_first(manager, session_data):
    job1 = _make(manager, session_data, task_name="first")
    job1.started_at -= 10  # force job1 to be older
    job2 = _make(manager, session_data, task_name="second")
    jobs = manager.list_jobs()
    assert [j.id for j in jobs] == [job2.id, job1.id]


def test_get_returns_none_for_unknown_id(manager):
    assert manager.get("does-not-exist") is None


def test_mark_done_sets_status_and_stops_elapsed_clock(manager, session_data, monkeypatch):
    import backend.restore_jobs as restore_jobs_module

    job = _make(manager, session_data)
    manager.mark_done(job.id)
    assert job.status == RestoreStatus.DONE
    assert not job.is_active
    elapsed_at_finish = job.elapsed_seconds

    # Advancing the clock after finish must not move elapsed_seconds -
    # it should be pinned to finished_at, not recomputed against "now".
    monkeypatch.setattr(restore_jobs_module.time, "time", lambda: job.finished_at + 100)
    assert job.elapsed_seconds == pytest.approx(elapsed_at_finish)


def test_mark_failed_records_error(manager, session_data):
    job = _make(manager, session_data)
    manager.mark_failed(job.id, "guest agent not responding")
    assert job.status == RestoreStatus.FAILED
    assert job.error == "guest agent not responding"
    assert not job.is_active


async def test_cancel_active_job_sets_flag_and_returns_true(manager, session_data):
    job = _make(manager, session_data)

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


def test_cancel_already_finished_job_returns_false(manager, session_data):
    job = _make(manager, session_data)
    manager.mark_done(job.id)
    assert manager.cancel(job.id) is False


def test_mark_cancelled_sets_status(manager, session_data):
    job = _make(manager, session_data)
    manager.mark_cancelled(job.id)
    assert job.status == RestoreStatus.CANCELLED
    assert not job.is_active


def test_to_dict_shape_matches_ui_columns(manager, session_data):
    job = _make(manager, session_data)
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


def test_restore_metadata_and_verify_are_independent_opt_ins(manager, session_data):
    # Not a "quick vs full" choice - a job can restore metadata without
    # verifying, verify without restoring metadata, or neither.
    job = _make(manager, session_data, restore_metadata=True, verify=False)
    assert job.restore_metadata is True
    assert job.verify is False


async def test_submitted_job_actually_runs(manager, session_data):
    job = _make(manager, session_data)
    ran = asyncio.Event()

    async def do_work(j):
        ran.set()
        manager.mark_done(j.id)

    manager.submit(job, do_work)
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await asyncio.sleep(0)  # let mark_done's task finish
    assert job.status == RestoreStatus.DONE
