"""Streaming multi-select archive download (issue #7) - the engine behind
`/api/download-bundle`.

The old implementation buffered every selected item whole in RAM (several
copies of it, in fact), built the archive in a `BytesIO` and compressed
it on the event loop - one big selection could OOM the process and stall
every other request, auth included. This builds the archive on a worker
thread instead and streams it out as it goes:

    PVE download (async, chunked)
        -> _AsyncSource (sync file-like; the worker thread pulls chunks
           from the event loop on demand, so backpressure is natural)
        -> tarfile reader (streaming, `r|` - no random access needed)
        -> zip / tar.gz / tar.zst writer (blocking `zipfile`/`tarfile`)
        -> _QueueSink (sync file-like; hands output chunks back to the
           event loop through a bounded `asyncio.Queue`)
        -> StreamingResponse

Memory is bounded by the queue depth plus a few chunk buffers,
independent of how large any file or the whole selection is; nothing is
staged on disk. That's possible because every item - a leaf or a
directory - is fetched with `tar=1`: a tar can be read strictly front to
back (a zip cannot - its index is at the end, so reading one needs random
access, i.e. a full copy on disk). A single file under `tar=1` arrives as
a one-member tar whose header carries its size, which the output tar
needs before the first byte of the member.

Failure semantics: once the first byte is sent the HTTP status can no
longer change, so a mid-stream failure (PVE drops, a bad archive) aborts
the response instead - the browser sees an interrupted download rather
than a silently truncated, apparently complete file. It's logged.
"""
import asyncio
import logging
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO

import zstandard

from . import pve_client
from .auth import SessionData
from .restore_bundle import BundleItem, _safe_zip_date_time

_log = logging.getLogger(__name__)

# Chunk size for reads from PVE and for the copy loop into the archive.
_CHUNK_SIZE = 256 * 1024
# Output is coalesced to about this size before it's queued - tar/zip
# headers are tiny writes, and a queue hop per header would dominate.
_SINK_BUFFER_SIZE = 256 * 1024
# Output chunks in flight between the worker thread and the response:
# at most ~_QUEUE_DEPTH * _SINK_BUFFER_SIZE of buffered output.
_QUEUE_DEPTH = 8

# Each in-progress download occupies one worker thread for its whole
# duration, so they get their own small pool instead of asyncio's default
# executor - which dir_cache/job_history's SQLite calls also share, and
# which a handful of long downloads would otherwise starve. A download
# past this many waits for a free worker (no bytes flow until it gets one).
MAX_CONCURRENT_BUNDLES = 4
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BUNDLES, thread_name_prefix="download-bundle")

# A symlinked leaf's target is spooled in memory up to this size, then on disk.
_SPOOL_MAX_SIZE = 8 * 1024 * 1024

_DONE = object()


class _Aborted(Exception):
    """The client went away; unwinds the worker thread."""


class _AsyncSource:
    """Sync file-like (just `read`/`peek`) over an async iterator of byte
    chunks. Called from the worker thread only: each refill hops to the
    event loop and blocks the *thread* (never the loop) until a chunk
    arrives, so the download only advances as fast as the archive writer
    consumes it."""

    def __init__(self, chunks: AsyncIterator[bytes], loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
        self._chunks = chunks
        self._loop = loop
        self._stop = stop
        self._buf = bytearray()
        self._eof = False

    async def _next(self) -> bytes | None:
        try:
            return await self._chunks.__anext__()
        except StopAsyncIteration:
            return None

    def _fill(self, n: int | None) -> None:
        while not self._eof and (n is None or len(self._buf) < n):
            if self._stop.is_set():
                raise _Aborted
            chunk = asyncio.run_coroutine_threadsafe(self._next(), self._loop).result()
            if chunk is None:
                self._eof = True
            else:
                self._buf += chunk

    def peek(self, n: int) -> bytes:
        self._fill(n)
        return bytes(self._buf[:n])

    def read(self, n: int | None = -1) -> bytes:
        if n is None or n < 0:
            self._fill(None)
            n = len(self._buf)
        else:
            self._fill(n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class _QueueSink:
    """Sync, non-seekable file-like handed to zipfile/tarfile as the
    output. Coalesces small writes, then hands each chunk to the event
    loop's bounded queue and blocks the worker thread until there's room
    (backpressure from a slow client)."""

    def __init__(
        self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, stop: threading.Event
    ) -> None:
        self._queue = queue
        self._loop = loop
        self._stop = stop
        self._buf = bytearray()

    def write(self, data) -> int:
        self._buf += data
        if len(self._buf) >= _SINK_BUFFER_SIZE:
            self._send()
        return len(data)

    def flush(self) -> None:
        pass

    def _send(self) -> None:
        if self._stop.is_set():
            raise _Aborted
        if self._buf:
            chunk = bytes(self._buf)
            self._buf.clear()
            asyncio.run_coroutine_threadsafe(self._queue.put(chunk), self._loop).result()

    def finish(self) -> None:
        self._send()


async def _download_chunks(
    session: SessionData, volume: str, filepath: str, tar: bool = True
) -> AsyncIterator[bytes]:
    client, response = await pve_client.open_download(session, volume, filepath, tar=tar)
    try:
        async for piece in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
            yield piece
    finally:
        await response.aclose()
        await client.aclose()


def _open_item_tar(source: _AsyncSource) -> tarfile.TarFile:
    """`tar=1` output is sometimes zstd-framed and sometimes a plain tar,
    for the same endpoint/parameter (docs/push-to-guest.md) - so detect
    by magic number. Opened in streaming mode (`r|`): strictly
    sequential, no seeking, which is all a pipe from PVE can offer."""
    if source.peek(4) == pve_client.ZSTD_MAGIC:
        return tarfile.open(fileobj=zstandard.ZstdDecompressor().stream_reader(source), mode="r|")
    return tarfile.open(fileobj=source, mode="r|")


def _safe_arcname(name: str) -> str | None:
    """Archive-relative path with no leading slash, no `.`/empty parts and
    no `..` - None if nothing usable is left or it tries to climb out."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _copy(src: BinaryIO, dst: BinaryIO, stop: threading.Event) -> None:
    while chunk := src.read(_CHUNK_SIZE):
        if stop.is_set():
            raise _Aborted
        dst.write(chunk)


class _OutputWriter:
    """One output archive (zip, tar.gz or tar.zst) being written to a
    sink, one member at a time."""

    def __init__(self, fmt: str, sink: _QueueSink, stop: threading.Event) -> None:
        self._fmt = fmt
        self._sink = sink
        self._stop = stop
        self._compressor = None
        if fmt == "zip":
            # The sink is non-seekable, so zipfile writes data descriptors
            # instead of seeking back to patch each local header.
            self._archive: zipfile.ZipFile | tarfile.TarFile = zipfile.ZipFile(
                sink, mode="w", compression=zipfile.ZIP_DEFLATED
            )
        elif fmt == "targz":
            self._archive = tarfile.open(fileobj=sink, mode="w|gz")
        else:
            # zstandard package, not stdlib compression.zstd (3.14+ only).
            self._compressor = zstandard.ZstdCompressor().stream_writer(sink, closefd=False)
            self._archive = tarfile.open(fileobj=self._compressor, mode="w|")

    def add(self, arcname: str, size: int, mtime: int, mode: int, src: BinaryIO) -> None:
        if isinstance(self._archive, zipfile.ZipFile):
            info = zipfile.ZipInfo(arcname)
            date_time = _safe_zip_date_time(mtime)
            info.date_time = date_time if date_time is not None else time.localtime(time.time())[:6]
            info.compress_type = zipfile.ZIP_DEFLATED
            info.file_size = size  # lets zipfile decide on zip64 up front
            with self._archive.open(info, "w", force_zip64=size >= zipfile.ZIP64_LIMIT) as dst:
                _copy(src, dst, self._stop)
        else:
            info = tarfile.TarInfo(name=arcname)
            info.size = size
            info.mtime = mtime
            info.mode = mode
            self._archive.addfile(info, src)

    def add_dir(self, arcname: str, mtime: int, mode: int) -> None:
        """A directory entry, so an empty folder (Windows' PerfLogs) survives."""
        if isinstance(self._archive, zipfile.ZipFile):
            info = zipfile.ZipInfo(arcname + "/")
            date_time = _safe_zip_date_time(mtime)
            info.date_time = date_time if date_time is not None else time.localtime(time.time())[:6]
            info.external_attr = (0o40000 | mode) << 16 | 0x10  # drwx bits + MS-DOS directory flag
            self._archive.writestr(info, b"")
        else:
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            info.mtime = mtime
            info.mode = mode
            self._archive.addfile(info)

    def close(self) -> None:
        self._archive.close()
        if self._compressor is not None:
            self._compressor.close()
        self._sink.finish()


def _add_item(
    writer: _OutputWriter,
    item: BundleItem,
    source: _AsyncSource,
    stop: threading.Event,
) -> None:
    """Reads one item's tar front to back and re-emits each regular file
    and directory (empty folders included) into the output archive. A directory's members keep PVE's own names -
    PVE already roots them under the directory's name, so re-prefixing
    would double it (issue #66/#26). A leaf's single member is named by
    the item itself. Returns whether anything was written - a leaf that
    isn't a regular file under `tar=1` (a symlink: /initrd.img is
    typically one) yields nothing, and the caller falls back to
    `_add_followed_leaf`."""
    wrote = False
    with _open_item_tar(source) as tar:
        for member in tar:
            if stop.is_set():
                raise _Aborted
            if member.isdir() and not item.leaf:
                dir_name = _safe_arcname(member.name)
                if dir_name is not None:
                    writer.add_dir(dir_name, int(member.mtime), member.mode & 0o7777)
                continue
            if not member.isfile():
                continue
            arcname = _safe_arcname(item.name if item.leaf else member.name)
            if arcname is None:
                _log.warning("download-bundle: skipping unsafe member name %r", member.name)
                continue
            src = tar.extractfile(member)
            writer.add(arcname, member.size, int(member.mtime), member.mode & 0o7777, src)
            wrote = True
            if item.leaf:
                break
    return wrote


def _add_followed_leaf(
    writer: _OutputWriter,
    session: SessionData,
    volume: str,
    item: BundleItem,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """A leaf whose `tar=1` form held no regular file is a symlink (the
    plain single-file download, `tar=0`, follows it and serves the
    target's bytes - which is what the user gets from the Download button
    and expects in the bundle). The target's size isn't known up front
    and a tar member needs it, so the content is spooled (memory up to
    _SPOOL_MAX_SIZE, then a temp file) - the one place a bundle touches
    disk, and only for a symlink's target."""
    arcname = _safe_arcname(item.name)
    if arcname is None:
        _log.warning("download-bundle: skipping unsafe item name %r", item.name)
        return
    chunks = _download_chunks(session, volume, item.filepath, tar=False)
    source = _AsyncSource(chunks, loop, stop)
    try:
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_SIZE) as spool:
            _copy(source, spool, stop)
            size = spool.tell()
            spool.seek(0)
            writer.add(arcname, size, int(time.time()), 0o644, spool)
    finally:
        asyncio.run_coroutine_threadsafe(chunks.aclose(), loop).result()


def _build(
    session: SessionData,
    volume: str,
    items: list[BundleItem],
    fmt: str,
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """Worker-thread body. Everything blocking (tarfile, zipfile,
    compression) happens here, never on the event loop."""
    sink = _QueueSink(queue, loop, stop)
    writer = _OutputWriter(fmt, sink, stop)
    for item in items:
        chunks = _download_chunks(session, volume, item.filepath)
        source = _AsyncSource(chunks, loop, stop)
        try:
            wrote = _add_item(writer, item, source, stop)
        finally:
            # Closes the PVE response/client even when this item's tar
            # wasn't read to the end (a leaf stops after its one member).
            asyncio.run_coroutine_threadsafe(chunks.aclose(), loop).result()
        if not wrote and item.leaf:
            _add_followed_leaf(writer, session, volume, item, loop, stop)
    writer.close()


async def stream_bundle(
    session: SessionData, volume: str, items: list[BundleItem], fmt: str
) -> AsyncIterator[bytes]:
    """Async generator of the finished archive's bytes. `fmt` is one of
    `zip`, `targz`, `tarzst` (validated by the caller). If the consumer
    stops early (client disconnect) the worker thread is unwound; if the
    build itself fails the exception propagates out of the generator,
    which aborts the response mid-stream - see the module docstring."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_DEPTH)
    stop = threading.Event()

    def build() -> None:
        try:
            _build(session, volume, items, fmt, queue, loop, stop)
        finally:
            if not stop.is_set():
                asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()

    task = asyncio.ensure_future(loop.run_in_executor(_executor, build))
    try:
        while (chunk := await queue.get()) is not _DONE:
            yield chunk
        await task
    except BaseException:
        stop.set()
        raise
    finally:
        if not task.done():
            # Unblock a worker parked on a full queue so it can see `stop`
            # and exit, instead of leaking a thread per abandoned download.
            while not task.done():
                while not queue.empty():
                    queue.get_nowait()
                await asyncio.sleep(0.05)
        if task.done() and not task.cancelled() and task.exception() is not None:
            _log.error("download-bundle failed mid-stream", exc_info=task.exception())
