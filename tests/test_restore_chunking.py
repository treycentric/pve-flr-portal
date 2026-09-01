import pytest

from backend.restore_chunking import (
    DEFAULT_CHUNK_SIZE_BYTES,
    bytes_to_wire_str,
    chunk_count,
    scratch_dir_path,
    scratch_filename,
    scratch_path_sep,
)


def test_empty_content_needs_one_chunk():
    assert chunk_count(0) == 1


def test_content_smaller_than_chunk_size_is_one_chunk():
    assert chunk_count(len(b"hello world"), chunk_size_bytes=1024) == 1


def test_content_exactly_divisible_splits_cleanly():
    assert chunk_count(100, chunk_size_bytes=25) == 4


def test_content_with_remainder_gets_a_short_final_chunk():
    assert chunk_count(105, chunk_size_bytes=25) == 5


def test_invalid_chunk_size_rejected():
    with pytest.raises(ValueError):
        chunk_count(4, chunk_size_bytes=0)
    with pytest.raises(ValueError):
        chunk_count(4, chunk_size_bytes=-1)


def test_scratch_filename_is_unique_per_job_and_index():
    names = {scratch_filename("job-1", i) for i in range(3)}
    assert len(names) == 3
    assert scratch_filename("job-1", 0) != scratch_filename("job-2", 0)
    assert scratch_filename("job-1", 0).startswith("job-1.part")


def test_bytes_to_wire_str_full_binary_range_round_trips_losslessly():
    # Live-verified 2026-09-01 against a real guest: every byte value
    # (including NUL and 0xFF) survives the latin-1 mapping unchanged -
    # this is the pure-logic half of that same guarantee.
    content = bytes(range(256))
    assert bytes_to_wire_str(content).encode("latin-1") == content


def test_default_chunk_size_matches_confirmed_server_ceiling():
    assert DEFAULT_CHUNK_SIZE_BYTES == 61440


def test_scratch_dir_path_windows():
    path = scratch_dir_path("windows", "job-123")
    assert path == "C:\\Windows\\Temp\\pve-flr-portal-job-123"


def test_scratch_dir_path_posix_default():
    assert scratch_dir_path("linux", "job-123") == "/tmp/pve-flr-portal-job-123"
    assert scratch_dir_path(None, "job-123") == "/tmp/pve-flr-portal-job-123"
    assert scratch_dir_path("bsd", "job-123") == "/tmp/pve-flr-portal-job-123"


def test_scratch_path_sep():
    assert scratch_path_sep("windows") == "\\"
    assert scratch_path_sep("linux") == "/"
    assert scratch_path_sep(None) == "/"
