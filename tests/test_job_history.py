from backend import job_history
from backend.restore_jobs import RestoreJobManager, RestoreStatus


def _make(session_data, **overrides):
    manager = RestoreJobManager()
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


async def test_create_persists_a_queued_row(session_data):
    job = _make(session_data)
    detail = await job_history.get(job.id)
    assert detail is not None
    assert detail["status"] == "queued"
    assert detail["id"] == job.id


async def test_get_returns_none_for_unknown_id():
    assert await job_history.get("does-not-exist") is None


async def test_persisted_detail_includes_log(session_data):
    job = _make(session_data)
    job.log("did a thing")
    job_history.persist_sync(job)
    detail = await job_history.get(job.id)
    assert any("did a thing" in line for line in detail["log"])


async def test_log_entries_accumulate_rather_than_overwrite(session_data):
    """Each log() call appends a row; all prior lines stay, in order."""
    job = _make(session_data)
    job.log("first")
    job.log("second")
    job.log("third")
    detail = await job_history.get(job.id)
    assert [line.split(" ", 1)[1] for line in detail["log"]] == ["first", "second", "third"]


async def test_evict_expired_does_not_orphan_log_entries(session_data):
    """ON DELETE CASCADE must actually take effect, not just be declared."""
    import time

    job = _make(session_data)
    job.log("some log line")
    job.status = RestoreStatus.DONE
    job.finished_at = time.time() - (8 * 86400)
    job_history.persist_sync(job)

    await job_history.evict_expired(7)

    with job_history._lock:
        conn = job_history._get_conn()
        orphaned = conn.execute(
            "SELECT COUNT(*) FROM job_log_entries WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
    assert orphaned == 0


async def test_persist_updates_in_place_not_a_new_row(session_data):
    job = _make(session_data)
    job_history.persist_sync(job)
    job.status = RestoreStatus.DONE
    job_history.persist_sync(job)
    persisted = await job_history.list_recent(7, exclude_ids=frozenset())
    matches = [p for p in persisted if p["id"] == job.id]
    assert len(matches) == 1
    assert matches[0]["status"] == "done"


async def test_list_recent_excludes_given_ids(session_data):
    job = _make(session_data)
    persisted = await job_history.list_recent(7, exclude_ids=frozenset({job.id}))
    assert all(p["id"] != job.id for p in persisted)


async def test_list_recent_includes_started_at_for_sorting(session_data):
    job = _make(session_data)
    persisted = await job_history.list_recent(7, exclude_ids=frozenset())
    entry = next(p for p in persisted if p["id"] == job.id)
    assert entry["_started_at"] == job.started_at


async def test_list_recent_excludes_rows_past_retention_window(session_data, monkeypatch):
    import time

    job = _make(session_data)
    job.status = RestoreStatus.DONE
    job.finished_at = time.time() - (8 * 86400)  # 8 days ago
    job_history.persist_sync(job)
    persisted = await job_history.list_recent(7, exclude_ids=frozenset())
    assert all(p["id"] != job.id for p in persisted)


async def test_list_recent_keeps_active_rows_regardless_of_age(session_data):
    """A still-active row (finished_at is None) is never excluded by the
    retention cutoff - reconcile_interrupted() is what closes these out,
    not the retention sweep."""
    job = _make(session_data)  # queued, finished_at is None
    persisted = await job_history.list_recent(0, exclude_ids=frozenset())
    assert any(p["id"] == job.id for p in persisted)


async def test_evict_expired_drops_old_terminal_rows(session_data):
    import time

    job = _make(session_data)
    job.status = RestoreStatus.DONE
    job.finished_at = time.time() - (8 * 86400)
    job_history.persist_sync(job)
    await job_history.evict_expired(7)
    assert await job_history.get(job.id) is None


async def test_evict_expired_keeps_recent_terminal_rows(session_data):
    job = _make(session_data)
    job.status = RestoreStatus.DONE
    job_history.persist_sync(job)
    await job_history.evict_expired(7)
    assert await job_history.get(job.id) is not None


async def test_evict_expired_never_drops_active_rows(session_data):
    job = _make(session_data)  # queued, finished_at None
    await job_history.evict_expired(0)
    assert await job_history.get(job.id) is not None


async def test_reconcile_interrupted_closes_out_active_rows(session_data):
    job = _make(session_data)  # queued
    reconciled = await job_history.reconcile_interrupted()
    assert job.id in reconciled
    detail = await job_history.get(job.id)
    assert detail["status"] == "interrupted"
    assert detail["cancellable"] is False
    assert detail["progress_percent"] is None
    assert any("Interrupted" in line for line in detail["log"])


async def test_reconcile_interrupted_preserves_log_lines_after_last_transition(session_data):
    """A crash mid-run must not lose log lines written since the last
    status transition."""
    job = _make(session_data)
    job.status = RestoreStatus.RUNNING
    job_history.persist_sync(job)
    job.log("mid-run progress line")  # RestoreJob.log() persists this itself
    reconciled = await job_history.reconcile_interrupted()
    assert job.id in reconciled
    detail = await job_history.get(job.id)
    assert any("mid-run progress line" in line for line in detail["log"])


async def test_reconcile_interrupted_leaves_terminal_rows_alone(session_data):
    job = _make(session_data)
    job.status = RestoreStatus.DONE
    job_history.persist_sync(job)
    reconciled = await job_history.reconcile_interrupted()
    assert job.id not in reconciled
    detail = await job_history.get(job.id)
    assert detail["status"] == "done"


async def test_clear_empties_history(session_data):
    job = _make(session_data)
    job_history.clear()
    assert await job_history.get(job.id) is None
