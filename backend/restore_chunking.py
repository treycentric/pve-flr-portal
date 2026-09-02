"""PH.5: chunk-size math and scratch-path helpers for restore_runner.py's
streaming multi-chunk write path (docs/plan.md §7.5). `agent/file-write`
is a genuine one-shot call - no handle/offset parameter exists, so a
file bigger than one call's payload ceiling can't be assembled by
repeated calls to the same guest path. A single chunk means the
content-only write path (no guest-exec) is usable; more than one means
the per-chunk-scratch-file + guest-exec concatenation path is required
instead.

**Live-verified 2026-09-01** against a real guest (PVE 9.2.11, QGA
110.0.2, Windows 10, see docs/plan.md §7.5):
- The `content` field is a **raw literal string, not base64** - neither
  direction is decoded/encoded by the server regardless of the
  `encode` param (tried 0 and 1, no observable effect on this
  version). This contradicts the archived §7.4 doc's "content is
  base64" assumption, which most likely described `pvesh`'s own
  convenience encoding for shell safety, not the raw HTTP API.
- The exact per-call ceiling is **61440 characters** - confirmed via
  the server's own validation error ("value may only be 61440
  characters long") at the boundary (60 KiB succeeds, 70 KiB fails).
- Arbitrary binary content (the full 0-255 byte range) round-trips
  losslessly by mapping bytes to a Latin-1-decoded `str` before
  sending - each byte maps 1:1 to a single Unicode codepoint with no
  loss, and the server preserves it exactly. No base64 needed, so the
  full 61440-character ceiling is 61440 *bytes* per chunk, not reduced
  by base64's ~33% encoding overhead as originally assumed.

**Streaming rewrite (2026-09-01):** `restore_runner.py` used to
pre-build a full list of every chunk's wire-ready string (`Chunk`
objects from `split_into_chunks()`) *in addition to* the raw downloaded
bytes it already held - roughly doubling peak memory, for content that
Direct Network Transfer (docs/plan.md §7.6, issue #22) doesn't even use
in wire-string form at all. Confirmed live: a large restore on a
memory-constrained deployment (512 MB LXC default) got OOM-killed by
the kernel before the restore ever got far enough to check Direct
Network Transfer's eligibility. `Chunk`/`split_into_chunks()`/
`needs_guest_exec()` are gone - `restore_runner.py` now works from the
raw downloaded bytes directly, converting one `DEFAULT_CHUNK_SIZE_BYTES`
slice to its wire string immediately before writing it and discarding
it right after, so at most one chunk's worth of wire-ready string is
ever alive at once instead of the whole file's worth.
"""

# Confirmed exact per-call ceiling (see module docstring) - not an
# estimate. Re-verify if a deployment ever targets a PVE/QGA version
# meaningfully older than the one this was checked against.
DEFAULT_CHUNK_SIZE_BYTES = 61440


def bytes_to_wire_str(data: bytes) -> str:
    """Lossless byte<->codepoint mapping for the `content` form field -
    NOT base64 (see module docstring: agent/file-write's content isn't
    decoded by the server, so base64 would be written to the guest file
    literally rather than the bytes it represents). Public (not
    module-private) since restore_runner.py now calls this once per
    streamed chunk rather than once per pre-built Chunk object."""
    return data.decode("latin-1")


def chunk_count(total_bytes: int, chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES) -> int:
    """How many `agent/file-write` calls a source of this size needs -
    cheap arithmetic, no need to actually materialize any chunk content
    just to answer "does this need guest-exec". Empty content still
    needs one (empty) write, same as the old split_into_chunks()."""
    if chunk_size_bytes <= 0:
        raise ValueError("chunk_size_bytes must be positive")
    if total_bytes <= 0:
        return 1
    return -(-total_bytes // chunk_size_bytes)  # ceiling division


def scratch_filename(job_id: str, index: int) -> str:
    """Deterministic, collision-safe per-chunk scratch filename within a
    job's own scratch directory (docs/plan.md §7.5)."""
    return f"{job_id}.part{index:05d}"


def scratch_dir_path(guest_os_family: str | None, job_id: str) -> str:
    """A per-job scratch directory under the guest's own temp root -
    `job_id` is a uuid (restore_jobs.RestoreJob.id), so collision with
    anything else is not a practical concern. Windows uses
    C:\\Windows\\Temp rather than %TEMP% - the latter varies by user
    profile, and QGA/guest-exec runs as SYSTEM, whose %TEMP% already
    resolves to C:\\Windows\\Temp anyway, so this just states it
    directly rather than depending on an extra env-var lookup."""
    if guest_os_family == "windows":
        return f"C:\\Windows\\Temp\\pve-flr-portal-{job_id}"
    return f"/tmp/pve-flr-portal-{job_id}"


def scratch_path_sep(guest_os_family: str | None) -> str:
    return "\\" if guest_os_family == "windows" else "/"
