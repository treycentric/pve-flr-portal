"""Multi-file/directory restore-to-guest (docs/plan.md §7.7, issue #24,
shipped v1.1.0). See docs/plan.md §7.7 for the full design and the
live-testing log.

- **`BundleItem`** - one selected file or directory, the same shape as
  `main.py`'s existing `download_bundle()` `item` query param (JSON
  `{filepath, name, leaf}`), reused here for one consistent multi-select
  convention across download and restore rather than inventing a new
  one.
- **`ManifestBuilder`** - accumulates one SHA256 per bundle entry as
  it's streamed through while the bundle is built (never buffering a
  whole entry just to hash it after the fact - the same streaming-hash
  discipline `restore_runner.py`'s memory fix, §7.6, established for
  single-file restore, extended here to many entries), and renders in
  the exact format Linux's own `sha256sum -c` already understands
  natively - so guest-side verification is one command, not an
  app-side comparison.
- **`select_bundle_format()`/probing** - "living off the land" (assuming
  a guest's `tar` can decompress `.zst`) is exactly the kind of
  assumption this project has been burned by before (`certutil`'s
  output shape, `copy /b`'s exit code, `cmd /c`'s quoting, `wmic`'s
  slowness) - so this probes by actually attempting to decompress a
  tiny known-good test blob rather than trusting a `--version`/`--help`
  string, and falls back to a server-side-rebuilt, universally
  extractable format (`.zip`/`.tar.gz`) when the guest can't.
- **`build_extract_command()`/`build_verify_command()`** - the actual
  guest-exec commands per bundle format, mirroring
  `restore_network_pull.build_fetch_command()`'s role for Design C.
- **`build_bundle()`** - the actual builder: downloads each selected
  item to its own local temp file (streamed, never a whole item in
  memory) and adds it to the output bundle one item at a time, deleting
  each item's temp file immediately after it's added - not downloading
  every item up front the way an earlier version of this did. Confirmed
  live 2026-09-01 that downloading everything before building anything
  could exhaust a real LXC container's disk on a multi-item selection
  (`OSError: [Errno 28] No space left on device`); this way, at most
  one source item's temp file exists on disk at once, alongside the
  growing output bundle, not every selected item's worth simultaneously.
  Stages through local temp files at all rather than a true zero-buffer
  pipe deliberately (2026-09-01 design review) - simpler and more
  robust than a hand-rolled sync/async bridge, at the cost of some real
  disk usage; the zero-buffer alternative is tracked separately as
  issue #25. `tarfile`/`zipfile` are blocking APIs, so the actual
  reads/writes run in a background thread (`asyncio.to_thread`) across
  several separate thread hops, since an async download has to happen
  between each item's add. Every entry - a whole leaf item, or one
  member inside a fetched directory's zip - streams through in
  fixed-size pieces during both passes, never buffered whole, matching
  the discipline §7.6's memory fix established.
"""
import asyncio
import hashlib
import io
import tarfile
import tempfile
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import zstandard

from . import pve_client
from .auth import SessionData
from .restore_chunking import DEFAULT_CHUNK_SIZE_BYTES

ExecFn = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
WriteFn = Callable[[str, str], Awaitable[None]]  # (guest path, wire-ready content) -> None

MANIFEST_NAME = ".pve-flr-manifest.sha256"


@dataclass(frozen=True)
class BundleItem:
    """One selected file or directory - same shape as main.py's
    download_bundle() `item` query param. `leaf=False` means a
    directory - fetched from PVE with `tar=1`, same as download_bundle()
    already does, and (confirmed by that existing code path) already
    covers the *full recursive tree* under it, not just one level."""

    filepath: str  # opaque file-restore filepath token (docs/plan.md §3)
    name: str  # display/archive-relative name
    leaf: bool = True


class ManifestBuilder:
    """Accumulates (relative_path, sha256_hex) entries while a bundle is
    being built. Renders as `sha256sum -c`-compatible text - two spaces
    between hash and path is `sha256sum`'s own format, not a stylistic
    choice, so it stays parseable by the real tool guest-side."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, str]] = []

    def add(self, relative_path: str, digest_hex: str) -> None:
        self._entries.append((relative_path, digest_hex))

    def __len__(self) -> int:
        return len(self._entries)

    def render(self) -> str:
        return "".join(f"{digest}  {path}\n" for path, digest in self._entries)


def build_zst_probe_blob() -> bytes:
    """A tiny, deterministic .tar.zst blob (one file, content 'ok') used
    to test whether a guest's `tar` can actually decompress zstd -
    rather than trusting a `--version`/`--help` string, which may not
    truthfully reflect the capability either way (some `tar` builds
    support zstd transparently via an external `zstd`/`unzstd` found on
    PATH without ever mentioning it; others print an unrelated banner).
    Built fresh each call (cheap - a couple hundred bytes) rather than
    hardcoded, so it stays obviously correct as this module changes."""
    payload = b"ok"
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="probe")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return zstandard.ZstdCompressor().compress(tar_buf.getvalue())


async def probe_tar_zst_support(write_fn: WriteFn, exec_fn: ExecFn, scratch_path: str) -> bool:
    """Writes the probe blob to the guest and attempts to actually
    extract it to stdout, checking both the exit code and the extracted
    content - the only reliable way to know (see build_zst_probe_blob()'s
    docstring). `write_fn`/`exec_fn` are injected the same way
    restore_network_pull.detect_fetch_tool() takes its exec_fn, so this
    is testable with fakes."""
    from .restore_chunking import bytes_to_wire_str

    blob = build_zst_probe_blob()
    try:
        await write_fn(scratch_path, bytes_to_wire_str(blob))
        exitcode, out, _err = await exec_fn(["tar", "-xO", "-f", scratch_path, "probe"])
    except Exception:
        return False  # any failure here just means "assume not capable", never fatal
    return exitcode == 0 and out.strip() == "ok"


class BundleFormat(StrEnum):
    TAR_ZST = "tarzst"
    ZIP = "zip"
    TAR_GZ = "targz"


def select_bundle_format(guest_os_family: str | None, zst_capable: bool) -> BundleFormat:
    """Native `.tar.zst` when the guest can actually decompress it (no
    server-side rebuild needed - most efficient path, straight from PVE
    to the guest). Otherwise falls back to a format the guest can
    extract without any special support: `.zip` on Windows
    (`Expand-Archive`, built into every supported PowerShell version,
    no dependency on the guest's own `tar` at all) or `.tar.gz`
    elsewhere (gzip support in `tar` predates zstd by decades - as close
    to universal as this project is willing to assume without a live
    guest test, matching the fetch-tool fallback chain's own caution)."""
    if zst_capable:
        return BundleFormat.TAR_ZST
    return BundleFormat.ZIP if guest_os_family == "windows" else BundleFormat.TAR_GZ


def build_extract_command(fmt: BundleFormat, bundle_path: str, dest_dir: str, guest_os_family: str | None) -> list[str]:
    """The guest-exec command that extracts an already-written bundle
    into `dest_dir`. `bundle_path`/`dest_dir` are embedded directly in a
    shell/PowerShell-interpreted command string for the ZIP case - same
    trust assumption restore_runner.py's own concat/mtime/verify
    commands already make: both must already have passed
    pve_client.check_path_safe() before this runs."""
    if fmt in (BundleFormat.TAR_ZST, BundleFormat.TAR_GZ):
        return ["tar", "-xf", bundle_path, "-C", dest_dir]
    if fmt == BundleFormat.ZIP:
        script = f"Expand-Archive -LiteralPath '{bundle_path}' -DestinationPath '{dest_dir}' -Force"
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    raise ValueError(f"Unknown bundle format: {fmt!r}")


def build_verify_command(manifest_path: str, dest_dir: str, guest_os_family: str | None) -> list[str]:
    """The guest-exec command that verifies every extracted file against
    the embedded manifest, entirely inside the guest - one call, no
    app-side hash comparison (docs/plan.md §7.7). Same embedding trust
    assumption as build_extract_command()."""
    if guest_os_family == "windows":
        script = (
            "$ok = $true; "
            f"Get-Content -LiteralPath '{manifest_path}' | ForEach-Object {{ "
            "$parts = $_ -split '  ', 2; "
            "if ($parts.Length -eq 2) { "
            f"$path = Join-Path '{dest_dir}' $parts[1]; "
            "$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash; "
            "if ($actual -ne $parts[0].ToUpper()) { $ok = $false; Write-Output ('FAIL ' + $parts[1]) } "
            "} }; "
            "if ($ok) { Write-Output 'ALL-OK' } else { exit 1 }"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return ["sh", "-c", f"cd '{dest_dir}' && sha256sum -c '{manifest_path}'"]


class _HashingReader:
    """Wraps a file-like object, updating a hasher as bytes are read
    through it - lets tarfile's/zipfile's own internal chunked reads
    double as the manifest hash computation, without a second pass over
    the same content."""

    def __init__(self, inner, hasher) -> None:
        self._inner = inner
        self._hasher = hasher

    def read(self, n: int = -1) -> bytes:
        chunk = self._inner.read(n)
        self._hasher.update(chunk)
        return chunk


ItemProgressFn = Callable[[BundleItem, int, int | None], None]  # (item, bytes so far, total bytes or None)


# (uid, gid, mode, mtime) - see pve_client.fetch_source_metadata. Only
# ever populated for a leaf item; a directory item's metadata is
# per-member, read later while its contents are added to the bundle
# (_add_directory_entries_to_tar/_zip), not one value for the whole item.
ItemMetadata = tuple[int, int, int, int] | None


async def _stream_download_to_file(
    session: SessionData,
    volume: str,
    filepath: str,
    tar: bool,
    dest: Path,
    item: BundleItem,
    on_progress: ItemProgressFn | None,
) -> None:
    """The actual streamed download, one DEFAULT_CHUNK_SIZE_BYTES piece
    at a time - never the whole item in memory (the same discipline
    restore_runner.py's memory fix, §7.6, established for a single
    file, extended here to each item in a multi-select bundle). Shared
    by both branches of _download_item_to_temp_file below, which only
    differ in `tar` and what (if anything) they fetch afterward."""
    client, response = await pve_client.open_download(session, volume, filepath, tar=tar)
    try:
        content_length_header = response.headers.get("content-length")
        total = int(content_length_header) if content_length_header is not None else None
        downloaded = 0
        with dest.open("wb") as f:
            async for piece in response.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE_BYTES):
                f.write(piece)
                downloaded += len(piece)
                if on_progress is not None:
                    on_progress(item, downloaded, total)
    finally:
        await response.aclose()
        await client.aclose()


async def _download_item_to_temp_file(
    session: SessionData,
    volume: str,
    item: BundleItem,
    tmp_dir: Path,
    guest_os_family: str | None,
    on_progress: ItemProgressFn | None = None,
) -> tuple[Path, ItemMetadata]:
    """Streams one selected item to its own local temp file - see
    _stream_download_to_file for the streaming discipline itself.

    A **leaf** item downloads its raw content unchanged (`tar=0`), then
    makes a second, small `fetch_source_metadata` call for its real
    mtime/uid/gid/mode (issue #26 - `file-restore/list`'s JSON never
    has uid/gid/mode, docs/plan.md §7.5/§7.7) - returned alongside the
    path.

    A **directory** item's local temp file format now depends on the
    guest: Windows keeps PVE's default zip (`tar=0`) - zip's own
    per-member `date_time` already gives a real mtime with no format
    switch needed, and Windows never needs uid/gid/mode (NTFS has no
    such concept - issue #20's confirmed-infeasible-via-any-Proxmox-API
    ACL finding applies here too). Every other guest now uses `tar=1`
    instead of the previous always-zip default - confirmed live
    2026-09-28 to carry real per-member uid/gid/mode *and* mtime
    (docs/plan.md §7.7), needed uniformly regardless of whether
    ownership restore was requested, since mtime restoration is
    unconditional. Metadata for a directory item is per-member, read
    later while its contents are added to the bundle - `None` here.

    `on_progress`, when given, is called after every chunk with (item,
    bytes downloaded so far, total bytes if PVE sent a Content-Length
    header else None) - unthrottled, so the caller decides how often to
    actually act on it (confirmed live 2026-09-02: this whole download
    step had no progress signal at all, a large single-directory
    selection looked indistinguishable from a hang for several
    minutes)."""
    dest = tmp_dir / f"item-{uuid.uuid4().hex}"
    if item.leaf:
        await _stream_download_to_file(session, volume, item.filepath, False, dest, item, on_progress)
        metadata = await pve_client.fetch_source_metadata(session, volume, item.filepath)
        return dest, metadata
    use_tar = guest_os_family != "windows"
    await _stream_download_to_file(session, volume, item.filepath, use_tar, dest, item, on_progress)
    return dest, None


def _safe_zip_date_time(mtime: int) -> tuple[int, int, int, int, int, int] | None:
    """Zip's date_time field can't represent anything before 1980 (no
    timezone either, 2-second granularity) - returns None for an mtime
    that predates that, so the caller falls back to the pre-existing
    "now" behavior instead of a hard ValueError from zipfile itself."""
    dt = time.localtime(mtime)
    if dt.tm_year < 1980:
        return None
    return dt[:6]


def _add_leaf_to_tar(
    tf: tarfile.TarFile,
    arcname: str,
    local_path: Path,
    manifest: ManifestBuilder,
    metadata: ItemMetadata,
    apply_ownership: bool,
) -> None:
    """Issue #26: `metadata` (from `pve_client.fetch_source_metadata`)
    carries the item's real mtime, applied unconditionally when known -
    and real uid/gid/mode, applied only when `apply_ownership` is set
    (the "Restore original owner/permissions" checkbox - Linux/BSD
    only, same as the single-file case, #20). `metadata` being None
    (the fetch itself failed) or `apply_ownership` being False just
    leaves tarfile's own defaults (mtime 0, uid/gid 0, mode 0o644) -
    same behavior as before this existed."""
    hasher = hashlib.sha256()
    info = tarfile.TarInfo(name=arcname)
    info.size = local_path.stat().st_size
    if metadata is not None:
        uid, gid, mode, mtime = metadata
        info.mtime = mtime
        if apply_ownership:
            info.uid = uid
            info.gid = gid
            info.mode = mode
    with local_path.open("rb") as f:
        tf.addfile(info, _HashingReader(f, hasher))
    manifest.add(arcname, hasher.hexdigest())


def _add_leaf_to_zip(
    zf: zipfile.ZipFile, arcname: str, local_path: Path, manifest: ManifestBuilder, metadata: ItemMetadata
) -> None:
    """Issue #26: `metadata`'s mtime, when known, becomes the zip
    entry's real `date_time` - previously always "now" (zipfile's own
    default when given a bare name instead of a ZipInfo). Uid/gid/mode
    are moot here: zip output only ever happens for a Windows guest
    (select_bundle_format), which has no Unix ownership concept at
    all - see _add_leaf_to_tar for the Linux/BSD ownership case."""
    hasher = hashlib.sha256()
    info: zipfile.ZipInfo | str = arcname
    if metadata is not None:
        date_time = _safe_zip_date_time(metadata[3])
        if date_time is not None:
            info = zipfile.ZipInfo(arcname, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
    with local_path.open("rb") as src, zf.open(info, "w") as dst:
        while piece := src.read(DEFAULT_CHUNK_SIZE_BYTES):
            hasher.update(piece)
            dst.write(piece)
    manifest.add(arcname, hasher.hexdigest())


@contextmanager
def _open_local_tar(path: Path):
    """Issue #26: a non-Windows directory item's local temp file is
    PVE's own `tar=1` encoding (_download_item_to_temp_file) - possibly
    zstd-framed, possibly a genuine plain tar (confirmed live
    inconsistent for the same endpoint/parameter, issue #20/#26,
    docs/plan.md §7.5/§7.7), so detect via the magic number rather than
    assume either. Unlike `pve_client.fetch_source_metadata`'s bounded-
    prefix read (only needs the first header), every member has to be
    read here, so this decompresses the whole file via a streaming
    reader rather than a bounded chunk."""
    raw = path.open("rb")
    try:
        magic = raw.read(4)
        raw.seek(0)
        if magic == pve_client.ZSTD_MAGIC:
            tf = tarfile.open(fileobj=zstandard.ZstdDecompressor().stream_reader(raw), mode="r|")
        else:
            tf = tarfile.open(fileobj=raw, mode="r")
        try:
            yield tf
        finally:
            tf.close()
    finally:
        raw.close()


def _add_directory_entries_to_tar(
    tf: tarfile.TarFile, local_tar_path: Path, manifest: ManifestBuilder, apply_ownership: bool
) -> None:
    """A directory item's local temp file is now PVE's own `tar=1`
    encoding (issue #26 - previously always a zip, re-expanded here;
    switched so real per-member uid/gid/mode/mtime - confirmed live
    2026-09-28 to survive PVE's file-restore path, docs/plan.md §7.7 -
    can be copied onto the outgoing entry instead of tarfile's bare
    defaults). Re-expands each member into the outer tar - one member
    at a time, streamed through, never a whole member's content in
    memory. mtime is applied unconditionally when known; uid/gid/mode
    only when `apply_ownership` is set (the "Restore original owner/
    permissions" checkbox, Linux/BSD only - this function is never
    reached for a Windows guest at all, since Windows directories still
    download as zip - see _add_directory_entries_to_zip).

    Each member's own `.name` is used as the arcname as-is, not
    prefixed with the item's own name - PVE's own archive for a
    directory already roots every entry under the directory's own name
    (e.g. `Downloads/file.txt` for a `Downloads` selection), confirmed
    live 2026-09-02 by a real restore landing files under a doubled
    `Downloads/Downloads/` because this used to re-prefix on top of
    that (issue #66/#26)."""
    with _open_local_tar(local_tar_path) as sub:
        for member in sub:
            if not member.isfile():
                continue
            arcname = member.name
            hasher = hashlib.sha256()
            tinfo = tarfile.TarInfo(name=arcname)
            tinfo.size = member.size
            tinfo.mtime = member.mtime
            if apply_ownership:
                tinfo.uid = member.uid
                tinfo.gid = member.gid
                tinfo.mode = member.mode & 0o7777
            src = sub.extractfile(member)
            tf.addfile(tinfo, _HashingReader(src, hasher))
            manifest.add(arcname, hasher.hexdigest())


def _add_directory_entries_to_zip(zf: zipfile.ZipFile, local_zip_path: Path, manifest: ManifestBuilder) -> None:
    """Windows-only path (non-Windows directories now source from a tar
    instead - see _add_directory_entries_to_tar): copies each member's
    real `date_time` from PVE's own zip, previously discarded (issue
    #26's folded-in mtime fix - same no-re-prefixing rationale as the
    tar case above). Ownership/mode is moot here: NTFS has no such
    concept, and this path is never reached for a Linux/BSD guest
    (select_bundle_format only ever picks ZIP for Windows)."""
    with zipfile.ZipFile(local_zip_path) as sub:
        for info in sub.infolist():
            if info.is_dir():
                continue
            arcname = info.filename
            hasher = hashlib.sha256()
            out_info = zipfile.ZipInfo(arcname, date_time=info.date_time)
            out_info.compress_type = zipfile.ZIP_DEFLATED
            with sub.open(info.filename) as src, zf.open(out_info, "w") as dst:
                while piece := src.read(DEFAULT_CHUNK_SIZE_BYTES):
                    hasher.update(piece)
                    dst.write(piece)
            manifest.add(arcname, hasher.hexdigest())


def _add_manifest_to_tar(tf: tarfile.TarFile, manifest: ManifestBuilder) -> None:
    data = manifest.render().encode("utf-8")
    info = tarfile.TarInfo(name=MANIFEST_NAME)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


@dataclass
class _BundleWriter:
    """Holds whatever open, still-being-written-to handles a given
    format needs across multiple separate `asyncio.to_thread()` calls -
    `open`/`add`/`finish` each run as their own thread hop (see
    `build_bundle()`), so nothing here can rely on a `with` block
    spanning the whole build the way `zipfile`/`tarfile` are normally
    used."""

    fmt: BundleFormat
    zf: zipfile.ZipFile | None = None
    tf: tarfile.TarFile | None = None
    compressor: object | None = None  # zstandard stream_writer - only for TAR_ZST
    raw: object | None = None  # the underlying file handle - only for the tar formats


def _open_bundle_writer(output_path: Path, fmt: BundleFormat) -> _BundleWriter:
    if fmt == BundleFormat.ZIP:
        return _BundleWriter(fmt=fmt, zf=zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED))
    # .tar.gz uses tarfile's built-in gzip support; .tar.zst uses the
    # zstandard package (not stdlib compression.zstd, which is 3.14+
    # only and won't exist on a normal deployment's Python) - same
    # reasoning as main.py's download_bundle().
    tar_mode = "w:gz" if fmt == BundleFormat.TAR_GZ else "w|"
    raw = output_path.open("wb")
    if fmt == BundleFormat.TAR_ZST:
        compressor = zstandard.ZstdCompressor().stream_writer(raw, closefd=False)
        tf = tarfile.open(fileobj=compressor, mode=tar_mode)
        return _BundleWriter(fmt=fmt, tf=tf, compressor=compressor, raw=raw)
    return _BundleWriter(fmt=fmt, tf=tarfile.open(fileobj=raw, mode=tar_mode), raw=raw)


def _add_item_to_bundle_writer(
    writer: _BundleWriter,
    item: BundleItem,
    local_path: Path,
    manifest: ManifestBuilder,
    metadata: ItemMetadata,
    apply_ownership: bool,
) -> None:
    """Adds one already-downloaded item's local file to the still-open
    bundle - the caller deletes `local_path` immediately after this
    returns (`build_bundle()`), so at most one source item's temp file
    exists on local disk at a time, alongside the growing output bundle
    - not every selected item's worth at once. Confirmed live
    2026-09-01: the previous download-everything-then-build approach
    ran a real LXC container's rootfs out of space
    (`OSError: [Errno 28] No space left on device`) on a multi-item
    selection - see docs/plan.md §7.7's finding for the fuller
    zero-buffer alternative (issue #25) this stops short of.

    `metadata`/`apply_ownership` (issue #26) only apply to a leaf item
    and the tar-output directory case - see _add_leaf_to_tar/_zip and
    _add_directory_entries_to_tar for what each actually does with
    them."""
    if writer.fmt == BundleFormat.ZIP:
        if item.leaf:
            _add_leaf_to_zip(writer.zf, item.name, local_path, manifest, metadata)
        else:
            _add_directory_entries_to_zip(writer.zf, local_path, manifest)
    else:
        if item.leaf:
            _add_leaf_to_tar(writer.tf, item.name, local_path, manifest, metadata, apply_ownership)
        else:
            _add_directory_entries_to_tar(writer.tf, local_path, manifest, apply_ownership)


def _finish_bundle_writer(writer: _BundleWriter, manifest: ManifestBuilder) -> None:
    """Embeds the manifest (has to be last - every other entry's hash
    has to be known first) and closes every handle `_open_bundle_writer()`
    opened, in order."""
    if writer.fmt == BundleFormat.ZIP:
        writer.zf.writestr(MANIFEST_NAME, manifest.render())
        writer.zf.close()
        return
    _add_manifest_to_tar(writer.tf, manifest)
    writer.tf.close()
    if writer.compressor is not None:
        writer.compressor.close()
    writer.raw.close()


def _abort_bundle_writer(writer: _BundleWriter) -> None:
    # Best-effort cleanup on the way out after a real error - swallows
    # its own failures rather than masking whatever actually went wrong.
    for handle in (writer.zf, writer.tf, writer.compressor, writer.raw):
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


async def build_bundle(
    session: SessionData,
    volume: str,
    items: list[BundleItem],
    guest_os_family: str | None,
    zst_capable: bool,
    on_item_progress: ItemProgressFn | None = None,
    restore_ownership: bool = False,
) -> tuple[Path, BundleFormat, ManifestBuilder, tempfile.TemporaryDirectory]:
    """Downloads each selected item (streamed, one piece at a time) to
    its own local temp file and adds it to the output bundle - format
    chosen by `select_bundle_format()` - one item at a time, deleting
    each item's temp file immediately after it's added rather than
    downloading everything up front: at most one source item's temp
    file exists on disk at once, alongside the growing output bundle,
    not every selected item's worth simultaneously (confirmed live
    2026-09-01 that downloading-everything-first could exhaust a real
    LXC container's disk on a multi-item selection). `tarfile`/`zipfile`
    are blocking APIs, so the actual reads/writes run in a background
    thread (`asyncio.to_thread`) - across several separate thread hops,
    since a download has to happen (async) between each item's add.
    Caller owns the returned `TemporaryDirectory`: keep it alive until
    the bundle's been fully sent on to the guest, then let it clean
    itself up (or use it as a context manager).

    `on_item_progress` - see `_download_item_to_temp_file()`'s
    docstring - is forwarded to each item's download, unthrottled;
    added because this whole build phase (download + add-to-bundle) had
    no progress signal at all, confirmed live 2026-09-02 to look
    indistinguishable from a hang on a large single-directory
    selection.

    `restore_ownership` (issue #26) - re-derived into `apply_ownership`
    here rather than trusted as-is, the same defense-in-depth already
    applied at /api/restore's job-creation time (#20): NTFS has no
    uid/gid/mode concept, so this is forced off for a Windows guest
    regardless of what's passed in. Original mtime is restored
    unconditionally whenever it's known, independent of this flag -
    that part was always claimed by the restore modal's own copy, just
    never actually implemented (issue #26's folded-in bug fix)."""
    apply_ownership = restore_ownership and guest_os_family != "windows"
    fmt = select_bundle_format(guest_os_family, zst_capable)
    tmp_dir_ctx = tempfile.TemporaryDirectory(prefix="pve-flr-portal-bundle-")
    tmp_dir = Path(tmp_dir_ctx.name)
    extension = {BundleFormat.TAR_ZST: "tar.zst", BundleFormat.TAR_GZ: "tar.gz", BundleFormat.ZIP: "zip"}[fmt]
    output_path = tmp_dir / f"bundle.{extension}"

    manifest = ManifestBuilder()
    writer = await asyncio.to_thread(_open_bundle_writer, output_path, fmt)
    try:
        for item in items:
            local_path, metadata = await _download_item_to_temp_file(
                session, volume, item, tmp_dir, guest_os_family, on_item_progress
            )
            try:
                await asyncio.to_thread(
                    _add_item_to_bundle_writer, writer, item, local_path, manifest, metadata, apply_ownership
                )
            finally:
                local_path.unlink(missing_ok=True)
        await asyncio.to_thread(_finish_bundle_writer, writer, manifest)
    except BaseException:
        await asyncio.to_thread(_abort_bundle_writer, writer)
        raise
    return output_path, fmt, manifest, tmp_dir_ctx
