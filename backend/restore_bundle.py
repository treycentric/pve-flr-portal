"""Multi-file/directory restore-to-guest (docs/plan.md §7.7, issue #24)
- not yet wired into a live restore. See docs/plan.md §7.7 for the full
design and open questions.

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
- **`build_bundle()`** - the actual builder: downloads every selected
  item to its own local temp file (streamed, never a whole item in
  memory), then combines them into one output bundle from those local
  files, in a background thread since `tarfile`/`zipfile` are
  synchronous APIs. Stages through local temp files rather than a true
  zero-buffer pipe deliberately (2026-09-01 design review) - simpler
  and more robust than a hand-rolled sync/async bridge, at the cost of
  real disk usage proportional to bundle size; the zero-buffer
  alternative is tracked separately as issue #25, worth building only
  if staging through disk turns out to actually be a problem once this
  ships. Every entry - a whole leaf item, or one member inside a
  fetched directory's zip - streams through in fixed-size pieces during
  both the download and the archive-building passes, never buffered
  whole, matching the discipline §7.6's memory fix established.
"""
import asyncio
import hashlib
import io
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
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


async def _download_item_to_temp_file(session: SessionData, volume: str, item: BundleItem, tmp_dir: Path) -> Path:
    """Streams one selected item to its own local temp file, one
    DEFAULT_CHUNK_SIZE_BYTES piece at a time - never the whole item in
    memory (the same discipline restore_runner.py's memory fix, §7.6,
    established for a single file, extended here to each item in a
    multi-select bundle). A directory item lands as PVE's own default
    zip encoding of everything under it - no `tar=1` needed; see this
    module's docstring correction in docs/plan.md §7.7."""
    dest = tmp_dir / f"item-{uuid.uuid4().hex}"
    client, response = await pve_client.open_download(session, volume, item.filepath, tar=False)
    try:
        with dest.open("wb") as f:
            async for piece in response.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE_BYTES):
                f.write(piece)
    finally:
        await response.aclose()
        await client.aclose()
    return dest


def _add_leaf_to_tar(tf: tarfile.TarFile, arcname: str, local_path: Path, manifest: ManifestBuilder) -> None:
    hasher = hashlib.sha256()
    info = tarfile.TarInfo(name=arcname)
    info.size = local_path.stat().st_size
    with local_path.open("rb") as f:
        tf.addfile(info, _HashingReader(f, hasher))
    manifest.add(arcname, hasher.hexdigest())


def _add_leaf_to_zip(zf: zipfile.ZipFile, arcname: str, local_path: Path, manifest: ManifestBuilder) -> None:
    hasher = hashlib.sha256()
    with local_path.open("rb") as src, zf.open(arcname, "w") as dst:
        while piece := src.read(DEFAULT_CHUNK_SIZE_BYTES):
            hasher.update(piece)
            dst.write(piece)
    manifest.add(arcname, hasher.hexdigest())


def _add_directory_entries_to_tar(
    tf: tarfile.TarFile, item_name: str, local_zip_path: Path, manifest: ManifestBuilder
) -> None:
    """A directory item's local temp file is PVE's own zip encoding of
    everything under it - this re-expands each member into the outer
    tar under `item_name/`, matching main.py's download_bundle()'s
    existing re-expansion logic (just targeting a different output
    format and also building the manifest). One member at a time,
    streamed through - never a whole member's content in memory."""
    with zipfile.ZipFile(local_zip_path) as sub:
        for info in sub.infolist():
            if info.is_dir():
                continue
            arcname = f"{item_name}/{info.filename}"
            hasher = hashlib.sha256()
            tinfo = tarfile.TarInfo(name=arcname)
            tinfo.size = info.file_size
            with sub.open(info.filename) as member:
                tf.addfile(tinfo, _HashingReader(member, hasher))
            manifest.add(arcname, hasher.hexdigest())


def _add_directory_entries_to_zip(
    zf: zipfile.ZipFile, item_name: str, local_zip_path: Path, manifest: ManifestBuilder
) -> None:
    with zipfile.ZipFile(local_zip_path) as sub:
        for info in sub.infolist():
            if info.is_dir():
                continue
            arcname = f"{item_name}/{info.filename}"
            hasher = hashlib.sha256()
            with sub.open(info.filename) as src, zf.open(arcname, "w") as dst:
                while piece := src.read(DEFAULT_CHUNK_SIZE_BYTES):
                    hasher.update(piece)
                    dst.write(piece)
            manifest.add(arcname, hasher.hexdigest())


def _add_manifest_to_tar(tf: tarfile.TarFile, manifest: ManifestBuilder) -> None:
    data = manifest.render().encode("utf-8")
    info = tarfile.TarInfo(name=MANIFEST_NAME)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _build_bundle_sync(
    items_with_paths: list[tuple[BundleItem, Path]], output_path: Path, fmt: BundleFormat
) -> ManifestBuilder:
    """Synchronous - combines every already-downloaded local item into
    one output bundle at `output_path`, with a manifest of every
    entry's SHA256 built as each is added, the manifest itself embedded
    last (has to be, since every other entry's hash has to be known
    first). Runs inside a background thread (`asyncio.to_thread`, see
    `build_bundle()`) since `tarfile`/`zipfile` are blocking APIs."""
    manifest = ManifestBuilder()
    if fmt == BundleFormat.ZIP:
        with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item, local_path in items_with_paths:
                if item.leaf:
                    _add_leaf_to_zip(zf, item.name, local_path, manifest)
                else:
                    _add_directory_entries_to_zip(zf, item.name, local_path, manifest)
            zf.writestr(MANIFEST_NAME, manifest.render())
        return manifest

    tar_mode = "w:gz" if fmt == BundleFormat.TAR_GZ else "w|"
    with output_path.open("wb") as raw:
        # .tar.gz uses tarfile's built-in gzip support; .tar.zst uses
        # the zstandard package (not stdlib compression.zstd, which is
        # 3.14+ only and won't exist on a normal deployment's Python) -
        # same reasoning as main.py's download_bundle().
        outer = zstandard.ZstdCompressor().stream_writer(raw, closefd=False) if fmt == BundleFormat.TAR_ZST else raw
        try:
            with tarfile.open(fileobj=outer, mode=tar_mode) as tf:
                for item, local_path in items_with_paths:
                    if item.leaf:
                        _add_leaf_to_tar(tf, item.name, local_path, manifest)
                    else:
                        _add_directory_entries_to_tar(tf, item.name, local_path, manifest)
                _add_manifest_to_tar(tf, manifest)
        finally:
            if outer is not raw:
                outer.close()
    return manifest


async def build_bundle(
    session: SessionData,
    volume: str,
    items: list[BundleItem],
    guest_os_family: str | None,
    zst_capable: bool,
) -> tuple[Path, BundleFormat, ManifestBuilder, tempfile.TemporaryDirectory]:
    """Downloads every selected item (streamed, one piece at a time) to
    its own local temp file, then builds one output bundle - format
    chosen by `select_bundle_format()` - from those local files in a
    background thread. Caller owns the returned `TemporaryDirectory`:
    keep it alive until the bundle's been fully sent on to the guest,
    then let it clean itself up (or use it as a context manager)."""
    fmt = select_bundle_format(guest_os_family, zst_capable)
    tmp_dir_ctx = tempfile.TemporaryDirectory(prefix="pve-flr-portal-bundle-")
    tmp_dir = Path(tmp_dir_ctx.name)

    items_with_paths = []
    for item in items:
        local_path = await _download_item_to_temp_file(session, volume, item, tmp_dir)
        items_with_paths.append((item, local_path))

    extension = {BundleFormat.TAR_ZST: "tar.zst", BundleFormat.TAR_GZ: "tar.gz", BundleFormat.ZIP: "zip"}[fmt]
    output_path = tmp_dir / f"bundle.{extension}"
    manifest = await asyncio.to_thread(_build_bundle_sync, items_with_paths, output_path, fmt)
    return output_path, fmt, manifest, tmp_dir_ctx
