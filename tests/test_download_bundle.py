"""Streaming /api/download-bundle - see backend/download_bundle.py."""

import asyncio
import io
import os
import tarfile
import threading
import time
import zipfile

import pytest
import zstandard

from backend import download_bundle, pve_client
from backend.restore_bundle import BundleItem


def _tar(members: dict[str, bytes], mtime: int = 1_700_000_000) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = mtime
            info.mode = 0o640
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _symlink_tar(name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tf.addfile(info)
    return buf.getvalue()


def _zst(data: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(data)


class _FakeResponse:
    def __init__(self, payload: bytes, piece: int = 1000, on_close=None) -> None:
        self._payload = payload
        self._piece = piece
        self._on_close = on_close
        self.headers = {}

    async def aiter_bytes(self, chunk_size=None):
        for i in range(0, len(self._payload), self._piece):
            yield self._payload[i : i + self._piece]
            await asyncio.sleep(0)

    async def aclose(self):
        if self._on_close:
            self._on_close()


class _FakeClient:
    async def aclose(self):
        pass


def patch_downloads(
    monkeypatch, by_filepath: dict[str, bytes], piece: int = 1000, on_close=None, plain: dict[str, bytes] | None = None
):
    """`plain` maps filepath -> bytes served for a `tar=0` request."""
    seen_tar_flags = []

    async def fake_open(session, volume, filepath, tar=False):
        seen_tar_flags.append(tar)
        payload = by_filepath[filepath] if tar else (plain or {})[filepath]
        return _FakeClient(), _FakeResponse(payload, piece, on_close)

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    return seen_tar_flags


async def _collect(session, items, fmt) -> bytes:
    out = bytearray()
    async for chunk in download_bundle.stream_bundle(session, "v", items, fmt):
        out += chunk
    return bytes(out)


@pytest.mark.parametrize("framing", ["plain", "zstd"])
async def test_zip_from_leaf_and_directory_items(monkeypatch, session_data, framing):
    wrap = (lambda b: b) if framing == "plain" else _zst
    dir_members = {"Downloads/": b"", "Downloads/photo.jpg": b"family photo bytes", "Downloads/a/b.txt": b"b"}
    flags = patch_downloads(
        monkeypatch,
        {"leaf": wrap(_tar({"hosts": b"file-content"})), "dir": wrap(_tar(dir_members))},
    )
    items = [BundleItem("leaf", "renamed.txt", True), BundleItem("dir", "Downloads", False)]
    data = await _collect(session_data, items, "zip")
    assert all(flags)  # every item is fetched as tar=1, never buffered as a zip
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        # leaf is named by the item; directory members keep PVE's names un-reprefixed
        assert sorted(zf.namelist()) == ["Downloads/", "Downloads/a/b.txt", "Downloads/photo.jpg", "renamed.txt"]
        assert zf.read("renamed.txt") == b"file-content"
        assert zf.read("Downloads/photo.jpg") == b"family photo bytes"


async def test_targz_and_tarzst_outputs(monkeypatch, session_data):
    patch_downloads(monkeypatch, {"dir": _tar({"D/x": b"x" * 5000, "D/y": b"y"})})
    items = [BundleItem("dir", "D", False)]

    gz = await _collect(session_data, items, "targz")
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["D/x", "D/y"]
        assert tf.extractfile("D/x").read() == b"x" * 5000
        assert tf.getmember("D/x").mtime == 1_700_000_000
        assert tf.getmember("D/x").mode == 0o640

    zst = await _collect(session_data, items, "tarzst")
    raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(zst)).read()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        assert sorted(tf.getnames()) == ["D/x", "D/y"]


async def test_large_file_streams_in_many_bounded_chunks(monkeypatch, session_data):
    big = os.urandom(12 * 1024 * 1024)  # incompressible; delivered in 64 KiB pieces
    patch_downloads(monkeypatch, {"f": _tar({"big.bin": big})}, piece=64 * 1024)
    chunks = []
    async for chunk in download_bundle.stream_bundle(session_data, "v", [BundleItem("f", "big.bin", True)], "zip"):
        chunks.append(chunk)
    assert len(chunks) > 3
    assert max(map(len, chunks)) <= 2 * download_bundle._SINK_BUFFER_SIZE
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as zf:
        assert zf.read("big.bin") == big


async def test_event_loop_stays_responsive_during_build(monkeypatch, session_data):
    big = b"z" * (8 * 1024 * 1024)
    patch_downloads(monkeypatch, {"f": _tar({"big": big})}, piece=64 * 1024)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    t = asyncio.create_task(ticker())
    await _collect(session_data, [BundleItem("f", "big", True)], "targz")
    t.cancel()
    assert ticks > 0


async def test_unsafe_member_names_are_skipped(monkeypatch, session_data):
    patch_downloads(monkeypatch, {"d": _tar({"D/ok": b"1", "../evil": b"2", "/abs/path": b"3"})})
    data = await _collect(session_data, [BundleItem("d", "D", False)], "zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["D/ok", "abs/path"]


async def test_failure_mid_stream_aborts_instead_of_truncating_silently(monkeypatch, session_data):
    good = _tar({"a": b"a" * 3000})

    async def fake_open(session, volume, filepath, tar=False):
        payload = good if filepath == "ok" else good[:700]  # cut off mid-member
        return _FakeClient(), _FakeResponse(payload)

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    items = [BundleItem("ok", "a", True), BundleItem("bad", "b", True)]
    with pytest.raises(Exception):
        await _collect(session_data, items, "zip")


async def test_consumer_abandoning_the_stream_unwinds_the_worker(monkeypatch, session_data):
    closed = threading.Event()
    big = bytes(range(256)) * 20000
    patch_downloads(monkeypatch, {"f": _tar({"big": big})}, piece=4096, on_close=closed.set)

    agen = download_bundle.stream_bundle(session_data, "v", [BundleItem("f", "big", True)], "zip")
    await agen.__anext__()
    await agen.aclose()
    deadline = time.time() + 5
    while not closed.is_set() and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert closed.is_set()  # the PVE response was closed and the worker thread exited


@pytest.mark.parametrize("fmt", ["zip", "targz", "tarzst"])
async def test_symlinked_leaf_is_bundled_as_its_target_content(monkeypatch, session_data, fmt):
    # /initrd.img is a symlink: tar=1 yields a symlink member (no bytes), the
    # plain download follows it. Used to produce an empty archive.
    content = b"initrd-bytes" * 5000
    patch_downloads(
        monkeypatch,
        {"a": _symlink_tar("initrd.img", "boot/initrd.img-6.1"), "b": _symlink_tar("initrd.img.old", "boot/old")},
        plain={"a": content, "b": b"old"},
    )
    items = [BundleItem("a", "initrd.img", True), BundleItem("b", "initrd.img.old", True)]
    data = await _collect(session_data, items, fmt)
    if fmt == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.testzip() is None
            assert {n: zf.read(n) for n in zf.namelist()} == {"initrd.img": content, "initrd.img.old": b"old"}
    else:
        raw = data if fmt == "targz" else zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
            assert {m.name: tf.extractfile(m).read() for m in tf} == {"initrd.img": content, "initrd.img.old": b"old"}


@pytest.mark.parametrize("fmt", ["zip", "targz", "tarzst"])
async def test_empty_directory_is_kept(monkeypatch, session_data, fmt):
    # Windows' C:\PerfLogs is an empty folder: its tar is one directory entry.
    patch_downloads(monkeypatch, {"d": _tar({"PerfLogs/": b""})})
    data = await _collect(session_data, [BundleItem("d", "PerfLogs", False)], fmt)
    if fmt == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ["PerfLogs/"]
            assert zf.getinfo("PerfLogs/").is_dir()
    else:
        raw = data if fmt == "targz" else zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
            assert [(m.name, m.isdir()) for m in tf] == [("PerfLogs", True)]
