"""Multi-file/directory restore-to-guest (docs/plan.md §7.7, issue #24)
- not yet wired into a live restore; this module holds the pure,
fully-testable-without-a-live-guest pieces the design depends on. See
docs/plan.md §7.7 for the full design and open questions.

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
"""
import io
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

import zstandard

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
