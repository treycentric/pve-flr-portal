import pytest

from backend.restore_chunking import (
    needs_guest_exec,
    scratch_dir_path,
    scratch_filename,
    scratch_path_sep,
    split_into_chunks,
)


def _reassembled_bytes(chunks):
    return b"".join(c.content.encode("latin-1") for c in chunks)


def test_empty_content_yields_single_empty_chunk():
    chunks = split_into_chunks(b"")
    assert len(chunks) == 1
    assert chunks[0].content == ""
    assert chunks[0].byte_count == 0
    assert not needs_guest_exec(chunks)


def test_content_smaller_than_chunk_size_is_one_chunk():
    content = b"hello world"
    chunks = split_into_chunks(content, chunk_size_bytes=1024)
    assert len(chunks) == 1
    assert chunks[0].content.encode("latin-1") == content
    assert chunks[0].byte_count == len(content)
    assert not needs_guest_exec(chunks)


def test_content_exactly_divisible_splits_cleanly():
    content = b"a" * 100
    chunks = split_into_chunks(content, chunk_size_bytes=25)
    assert len(chunks) == 4
    assert all(c.byte_count == 25 for c in chunks)
    assert _reassembled_bytes(chunks) == content


def test_content_with_remainder_gets_a_short_final_chunk():
    content = b"a" * 105
    chunks = split_into_chunks(content, chunk_size_bytes=25)
    assert len(chunks) == 5
    assert [c.byte_count for c in chunks] == [25, 25, 25, 25, 5]
    assert needs_guest_exec(chunks)


def test_chunk_indices_are_sequential_from_zero():
    chunks = split_into_chunks(b"a" * 50, chunk_size_bytes=20)
    assert [c.index for c in chunks] == [0, 1, 2]


def test_invalid_chunk_size_rejected():
    with pytest.raises(ValueError):
        split_into_chunks(b"data", chunk_size_bytes=0)
    with pytest.raises(ValueError):
        split_into_chunks(b"data", chunk_size_bytes=-1)


def test_scratch_filename_is_unique_per_job_and_chunk():
    chunks = split_into_chunks(b"a" * 50, chunk_size_bytes=20)
    names = {scratch_filename("job-1", c) for c in chunks}
    assert len(names) == len(chunks)
    assert scratch_filename("job-1", chunks[0]) != scratch_filename("job-2", chunks[0])
    assert scratch_filename("job-1", chunks[0]).startswith("job-1.part")


def test_full_binary_byte_range_round_trips_losslessly():
    # Live-verified 2026-09-01 against a real guest: every byte value
    # (including NUL and 0xFF) survives the latin-1 mapping unchanged -
    # this is the pure-logic half of that same guarantee.
    content = bytes(range(256))
    chunks = split_into_chunks(content, chunk_size_bytes=1024)
    assert len(chunks) == 1
    assert _reassembled_bytes(chunks) == content


def test_default_chunk_size_matches_confirmed_server_ceiling():
    from backend.restore_chunking import DEFAULT_CHUNK_SIZE_BYTES

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
