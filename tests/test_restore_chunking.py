import base64

import pytest

from backend.restore_chunking import needs_guest_exec, scratch_filename, split_into_chunks


def test_empty_content_yields_single_empty_chunk():
    chunks = split_into_chunks(b"")
    assert len(chunks) == 1
    assert chunks[0].content_b64 == ""
    assert chunks[0].byte_count == 0
    assert not needs_guest_exec(chunks)


def test_content_smaller_than_chunk_size_is_one_chunk():
    content = b"hello world"
    chunks = split_into_chunks(content, chunk_size_bytes=1024)
    assert len(chunks) == 1
    assert base64.b64decode(chunks[0].content_b64) == content
    assert chunks[0].byte_count == len(content)
    assert not needs_guest_exec(chunks)


def test_content_exactly_divisible_splits_cleanly():
    content = b"a" * 100
    chunks = split_into_chunks(content, chunk_size_bytes=25)
    assert len(chunks) == 4
    assert all(c.byte_count == 25 for c in chunks)
    reassembled = b"".join(base64.b64decode(c.content_b64) for c in chunks)
    assert reassembled == content


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
