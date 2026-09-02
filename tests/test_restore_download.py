import time

import pytest

from backend import restore_download


@pytest.fixture(autouse=True)
def _clear_tokens():
    restore_download.clear()
    yield
    restore_download.clear()


def test_mint_then_consume_returns_the_job_id():
    token = restore_download.mint_token("job-1", ttl_seconds=60)
    assert restore_download.consume_token(token) == "job-1"


def test_consume_is_single_use():
    token = restore_download.mint_token("job-1", ttl_seconds=60)
    assert restore_download.consume_token(token) == "job-1"
    assert restore_download.consume_token(token) is None


def test_unknown_token_returns_none():
    assert restore_download.consume_token("not-a-real-token") is None


def test_expired_token_returns_none_and_is_consumed():
    token = restore_download.mint_token("job-1", ttl_seconds=-1)  # already expired
    assert restore_download.consume_token(token) is None
    # Still single-use even though it was expired - no second bite at it.
    assert restore_download.consume_token(token) is None


def test_tokens_are_unique_and_unguessable_length():
    tokens = {restore_download.mint_token("job-1", ttl_seconds=60) for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 32 for t in tokens)


def test_local_path_defaults_to_none_for_the_ordinary_single_file_case():
    token = restore_download.mint_token("job-1", ttl_seconds=60)
    info = restore_download.consume_token_full(token)
    assert info.job_id == "job-1"
    assert info.local_path is None


def test_local_path_is_carried_through_for_a_bundle_restore():
    # 2026-09-02, docs/plan.md §7.7: a bundle's Direct Network Transfer
    # mints a token carrying the already-built local bundle path, so the
    # download endpoint can serve it directly instead of re-proxying
    # from PVE (which can't hand back a synthesized bundle as one item).
    token = restore_download.mint_token("job-1", ttl_seconds=60, local_path="/tmp/bundle.tar.gz")
    info = restore_download.consume_token_full(token)
    assert info.job_id == "job-1"
    assert info.local_path == "/tmp/bundle.tar.gz"


def test_consume_token_still_returns_just_the_job_id():
    # The original, narrower shape - still used by callers that only
    # need the job_id, and by every test above this one.
    token = restore_download.mint_token("job-1", ttl_seconds=60, local_path="/tmp/bundle.tar.gz")
    assert restore_download.consume_token(token) == "job-1"


def test_consume_token_full_is_also_single_use():
    token = restore_download.mint_token("job-1", ttl_seconds=60)
    assert restore_download.consume_token_full(token) is not None
    assert restore_download.consume_token_full(token) is None


def test_default_ttl_comes_from_settings(monkeypatch):
    # Settings is a frozen dataclass - swap the module's whole reference
    # rather than mutating a field on it.
    from dataclasses import replace

    monkeypatch.setattr(
        restore_download, "settings", replace(restore_download.settings, restore_download_token_ttl_seconds=0.01)
    )
    token = restore_download.mint_token("job-1")
    time.sleep(0.02)
    assert restore_download.consume_token(token) is None
