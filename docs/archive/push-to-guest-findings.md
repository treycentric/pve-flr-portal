# Archived: push-to-guest restore (PH.5) — real-world findings

> **This is a frozen historical record**, not living documentation. It
> collects the dated bug-investigation write-ups from building and
> live-testing push-to-guest restore (PH.5, shipped v1.1.0) — what
> broke, why, and how it was found and fixed — kept because the lessons
> (especially "verify a guest-tool's real behavior, never trust a
> `--version` string or an exit code alone") are worth knowing before
> touching this code again. For current mechanism/data model, see
> [`../push-to-guest.md`](../push-to-guest.md). For the API facts these
> findings were often chasing down, see
> [`../api-reference.md`](../api-reference.md). Not living — don't
> edit it; new lessons belong in `push-to-guest.md` or a fresh entry
> here, matching the pattern below.

## Single-file restore: write mechanics and guest reliability

**QGA facts confirmed from official sources, then live-verified
(2026-08-31 / 2026-09-01).** Early drafts of this design assumed
`agent/file-write`'s `content` was base64-encoded and that the per-call
ceiling was somewhere in a wide "~40–60 KiB vs. 48 MB" range — both
turned out wrong once checked against a real guest (PVE 9.2.11, QGA
110.0.2, Windows 10): the ceiling is exactly 61440 characters (the
server's own validation error at the boundary proved it), and `content`
is a raw literal string regardless of the `encode` param, confirmed by
round-tripping an 11-character non-base64-shaped string unchanged and
separately the full 0–255 byte range byte-for-byte identical. The
archived base64 assumption most likely described `pvesh`'s own
convenience encoding in its CLI examples, not the raw HTTP API.
`qemu-guest-agent` was also found not running on the test guest despite
`agent: 1` in its config — confirming the capability-detection design's
degrade-to-unavailable-on-any-failure behavior was the right call, not
just defensive nicety.

**`guest_agent_lock.py` exists because of a real channel-desync
incident (2026-09-01).** `qemu-guest-agent` accepts only one command at
a time over its virtio-serial channel. An early version of the
capability-check code retried `agent/info` after *any* failure,
including a client-side timeout — but a timeout doesn't prove the
in-guest command was actually abandoned, so the retry could send a
second command while the first was still in flight. This is the most
likely explanation for an observed regression: capability checks
reading "still enabled" moments earlier, then "not enabled or not
responding" on every check after, with restarting the in-guest service
not clearing it (consistent with a channel-level desync, not the
service actually stopping). Fixed with a per-vmid `asyncio.Lock`
(`guest_agent_command()`) and a retry policy that only retries on a
*definite* `httpx.HTTPStatusError`, never a timeout/connection error —
PVE/QEMU surfaces genuine external contention (another admin's `qm
agent` call, a scheduled backup's fs-freeze) as a completed error
response, not a timeout, so that specific signal is safe to retry on.

**Windows `cmd /c` with multiple embedded quoted segments is
unreliable — found three separate times before the lesson generalized
(2026-09-01, 2026-09-02 ×2).** `_ensure_destination_dir()` and
`_verify_destination_exists()`'s original `cmd /c if not exist "X"
mkdir "X"` form failed its very first live test against a real Windows
guest with "The filename, directory name, or volume label syntax is
incorrect" against a completely valid path. Fixed by switching to
PowerShell (`New-Item -Force`, `Test-Path -LiteralPath`, single-quoted
literals) — the same pattern `_restore_mtime()` had already used
successfully. `_concat_chunks()`'s Windows path used the superficially
similar `copy /b "a"+"b" "dest"` and was left alone at the time since
it hadn't been shown to fail — until a later live test (a 4-item
bundle, single-chunk) hit `Could not assemble the restored file: The
filename, directory name, or volume label syntax is incorrect` from
that exact function. Fixed the same way (a small PowerShell
`[System.IO.File]::Open`/`CopyTo` script), and a proactive sweep of the
rest of the codebase for the same shape found one more unconverted
case, `restore_network_pull.py`'s `bitsadmin` fetch-tool command —
fixed before it could fail live too, by invoking `bitsadmin.exe` as
plain argv instead of through `cmd /c`. Lesson: "already fixed this bug
class elsewhere" doesn't mean every call site got the memo — a targeted
grep for the pattern after the first fix would have caught the other
two before they shipped.

**A Windows shell command's exit code alone is not trustworthy evidence
it did what it claims (2026-09-03).** Restoring into a destination
directory that didn't yet exist failed two steps *after* the actual
problem: `copy /b`'s exit code reported success even though the
destination was never created (its non-existent parent silently
swallowed the write), so the job sailed through concatenation and only
failed confusingly during metadata restore. Fixed with
`_ensure_destination_dir()` (create the parent up front) and
`_verify_destination_exists()` (an explicit existence check immediately
after concatenation, raising a clear error right there instead of
letting the failure surface downstream). Confirms the same lesson the
browse feature's `dir`/`wmic` findings (below) already established
independently.

**`desktop.ini` and any ReadOnly-attributed destination fail with
"Access is denied," even for SYSTEM (2026-09-25, issue #72).** Windows'
`CreateFile` refuses write access to a ReadOnly file regardless of
caller privilege. Explorer creates `desktop.ini` with ReadOnly (often
Hidden+System too) by default in nearly every folder, making it the
single most common file to hit this. Fixed with
`_clear_readonly_if_present()`, called *proactively* before every
write (not just reactively after a failure) whenever `Unrestricted` is
available — the restore modal's own overwrite-confirmation checkbox
already states the intent this fulfills, and the failure mode
otherwise is a hard stop with no useful partial state. Covers Linux
too (owner-write bit, immutable flag), though no live Linux case had
actually been seen yet at the time. Making this proactive meant even a
single-chunk, no-metadata restore now pays one extra capability-check
round trip — a deliberate, small latency tradeoff in exchange for
avoiding the failure outright.

**`tar=1`'s output format is not reliably one thing, and this hid from
local testing for a genuinely subtle reason (2026-09-28, issue #20).**
A live single-file ownership-restore test against a real Turnkey Linux
guest failed — diagnostic logging showed the `tar=1` response starting
with the zstd magic number, fed straight into `tarfile.open()`
unmodified ("truncated header"). The exact same endpoint/parameter had
returned a genuine plain, uncompressed tar for a *different* file
earlier in the same investigation — PVE's `tar=1` isn't reliably
`.tar.zst` despite the name, and consuming code must detect via the
magic number and handle either. Fixed with `_decompress_prefix`, using
a *streaming* zstd reader rather than a single-shot `decompress()`
(which needs a known size or complete frame, neither guaranteed by a
deliberately-truncated 16KB prefix). **The regression test needed a
second, more direct form for an equally subtle reason**: Python 3.14
(this project's dev environment) masked the bug entirely — its stdlib
`tarfile.open()` auto-detects and transparently decompresses
zstd-framed input natively (new that release), silently absorbing
exactly the bug this fix addresses, while the Docker deployment target
(Python 3.11) has no such support and is where the live bug actually
happened. A direct unit test of `_decompress_prefix` itself, asserting
a real round-trip through the `zstandard` library, became the actual
regression guard, since it can't be masked by which Python version runs
the suite.

**Windows ACLs: investigated and confirmed infeasible, not just
untested (2026-09-28).** The NTFS Security Descriptor survives intact
inside a PBS backup (PBS backs up VMs as a raw block image, not
filesystem-aware) — the loss happens entirely in Proxmox's file-restore
helper, which only ever emits `tar`/`zip`, and neither format has a
field for a Windows Security Descriptor. No other Proxmox-exposed API
surfaces it either. A Proxmox forum thread shows a maintainer
confirming this is a known, bugzilla-tracked, unresolved gap in
Proxmox's own ecosystem broadly, not specific to this app's restricted
API surface.

## Original-location restore and drive-letter display (issues #68, #77)

**Windows disk identity is resolvable by real guest-reported bus
address, not just ordinal guessing (2026-09-27).** Confirmed live
against a Proxmox VM with one `ide0`, two `sata` disks (with a cdrom
deliberately occupying `sata0` to test gap-tolerance), and multiple
`scsi` disks including one reassigned to `scsi5`. `sataN` matches via
`Get-PhysicalDisk`'s `BusType=SATA` + `Win32_DiskDrive.SCSIBus`,
confirmed gap-tolerant since cdrom drives never enumerate through
`Win32_DiskDrive` at all. `scsiN` (virtio-scsi) needed the LUN from a
PnP `DEVPKEY_Device_LocationInfo` property instead, since
`virtio-scsi-single` gives every scsi disk its own dedicated controller
instance — `Win32_DiskDrive`'s own `SCSIBus`/`SCSITargetId` read `0`/`0`
identically for every scsi disk regardless of PVE index, useless as a
discriminator. `Win32_DiskDrive.InterfaceType` itself proved unreliable
as a bus discriminator in this same test — a real SATA/AHCI disk
reported `InterfaceType: IDE`, indistinguishable from genuine legacy
IDE without cross-checking `Get-PhysicalDisk`'s `BusType`. `ideN` was
only ever verified with a single IDE disk present; `virtio` (virtio-blk)
was never tested at all. Both fall back to the pre-existing ordinal
guess, same as Linux (still ordinal-only throughout).

**A `[string]`/numeric type mismatch silently broke the bus-address
match on first real deployment (2026-09-28).** `Get-PhysicalDisk`'s
`DeviceId` is a string (`"0"`, `"1"`, ...) while `Win32_DiskDrive.Index`
is numeric — the original script built a hashtable keyed by the former
and looked it up by the latter with no cast, so every `BusType` came
back `$null` and every match silently fell through to the (wrong)
ordinal guess. Live-reported as original-location restore resolving to
a disk with no drive letter and the browse-tree annotation showing
nothing, both looking like unrelated failures until debug logging
showed identical `BusType: None` rows for every disk. Fixed by casting
both sides to `[string]` explicitly. **Not caught by this project's own
test suite** — every test here mocks `run_guest_exec`'s JSON *output*
directly rather than executing real PowerShell, so a bug living
entirely in the script's own text (a type-coercion issue only
PowerShell's runtime would surface) was invisible to unit tests by
construction. Worth a specific eye on any future PowerShell script
added to this module for the same reason.

**A follow-on change that reinterprets crumb-trail shape broke a module
designed against the old shape, on its first real use (2026-09-25).**
`guest_original_location.py` was written against the raw `part`/`lvm`
disk structure, but issue #66's LVM-elevation/`part`-flattening change
landed in `main.py` in the same session, *after* this module was
designed — so every crumb trail a real user could actually produce
through the current UI stopped matching what the parser expected, and
it fell through to "can't determine the original location"
unconditionally. Fixed by recognizing all three shapes
`resolve_original_directory` can actually see: elevated LVM, flattened
partition, and unflattened partition (kept as a defensive fallback).
Lesson: a change that reinterprets a shared data shape needs checking
against every other feature that parses that same shape, not just the
endpoints it was written for.

**Windows junctions/reparse points surfaced a raw, unexplained "File
Not Found" (2026-09-02).** The Windows subfolder listing originally
used `cmd /c dir <path> /b /ad` (bare names only), which can't
distinguish a real directory from a reparse point — clicking into a
legacy compatibility junction like `C:\Documents and Settings` (which
Windows deliberately blocks normal enumeration into, even for SYSTEM)
surfaced the raw error with no indication why. Switched to PowerShell
(`Get-ChildItem -Directory` filtered on the `ReparsePoint` attribute)
so junctions are excluded from the listing outright rather than merely
erroring when clicked.

**A PowerShell argument-passing assumption didn't survive contact with
reality (2026-09-02).** The first version of the subfolder-listing
PowerShell call passed `path` as a trailing argv element after
`-Command`, expecting it to bind to `$args[0]` inside the script —
confirmed live this does not work (`powershell -Command` appends
trailing CLI arguments onto the command *string* itself, not into
`$args`). Fixed by embedding `path` directly as a single-quoted
PowerShell string literal — safe specifically because
`_check_path_safe()` (already called earlier) rejects `'` along with
every shell metacharacter, so nothing reaching this point can break out
of the literal.

**`wmic` is slow, confirmed live, not just "legacy" in the abstract
(2026-09-02).** The Windows drive list originally used `cmd /c wmic
logicaldisk get caption` and was noticeably sluggish on first use —
`wmic` goes through the WMI provider host (`winmgmt`), with real
cold-start overhead especially right after boot. Switched to
PowerShell's `Get-PSDrive -PSProvider FileSystem` (no WMI round trip),
which also unified the drive-list and subfolder-listing calls onto one
tool instead of two.

**Per-vmid caching for the Windows disk-bus/drive-letter queries was a
straight win once confirmed both already covered every disk in one call
(2026-09-28).** `_windows_disk_number_by_bus`'s query and (once
widened) `list_windows_drive_letters`'s query each already return every
disk's/partition's data in one guest-exec call regardless of which one
a given request needs — so drilling into a second or third disk in the
same session was needlessly re-running an identical whole-VM query.
Fixed with a short-TTL (120s), per-vmid cache, caching only successful
fetches so a transient failure doesn't poison the cache for the rest of
the window. Deliberately cache-on-first-use rather than eager-prefetch,
so a user who never drills into a disk never pays the guest-exec cost
at all.

**Separately confirmed and not fixed anywhere:** `file-restore/list`'s
disk-level entries report `size: 0` even for a real multi-GB disk — the
`size` field is evidently only meaningful for leaf files, not
disk/folder-shaped entries.

## Direct Network Transfer

**Live end-to-end verification (2026-09-01), both guest OS families.**
First real run against a real Proxmox host: an LXC container with a
second NIC (`pct set -netN`), a Windows VM on the matching subnet.
`select_data_nic()` matched the guest's own subnet, `detect_fetch_tool()`
found `Invoke-WebRequest`, and 3.9 MB transferred over the guest's own
network in ~8 seconds — versus the many sequential QMP round-trips the
chunked path would have needed. A Linux guest was confirmed separately
below, after two bugs that first attempt surfaced.

**A pre-existing memory-scaling problem, not a DNT bug, OOM-killed the
whole process on a large Linux restore (2026-09-01).** The LXC
container's `pve-flr-portal.service` got OOM-killed on its 512 MB
default memory limit before the restore even reached DNT's eligibility
check. Root cause predated this feature: `run_restore()` downloaded the
whole source file into memory, then built a *second*, full copy as a
list of wire-ready chunk strings — doubling peak memory for content DNT
doesn't even use in wire-string form. A first fix (removing the
redundant copy) wasn't enough — retried against a real large file and
the process was OOM-killed again, this time dying during the initial
`content = await response.aread()` itself, before logging a single
byte count. Fixed properly: `run_restore()` now reads via
`response.aiter_bytes()`, never `aread()`, keeping at most one
`DEFAULT_CHUNK_SIZE_BYTES` piece of raw content alive at a time for the
whole restore regardless of file size — locked in with a test double
that raises if `aread()` is ever called again.

**Immediately after the memory fix, the next Linux attempt failed on a
hardcoded timeout (2026-09-01).** `run_guest_exec()`'s poll budget was
a fixed ~15 seconds, sized for commands whose duration doesn't scale
with file size — exactly wrong for DNT's fetch, whose whole duration
*is* the transfer. Fixed with an optional `timeout_seconds` parameter
and a new `RESTORE_LONG_RUNNING_EXEC_TIMEOUT_SECONDS` setting (default
1800s), applied only to the three calls that actually scale with
content size (the DNT fetch, checksum hashing, chunk-reassembly
concatenation) — every other guest-exec call (mkdir, exists checks,
fetch-tool probes) stays on the fast default.

**Live end-to-end verification, Linux guest, at real scale
(2026-09-01).** With both fixes in place: a 706 MB file (over the
512 MB container's own RAM) fetched via `curl` in ~21s and
checksum-verified in ~17s, both comfortably inside the new timeout
budget — first real proof the streaming and timeout fixes solved the
problems they were written for, not just that they passed their own
unit tests.

### HTTPS data-plane shakeout (issue #47, 2026-09-08)

First deploy + live-verification on a real cluster (Windows + Linux
VMs, self-signed and step-ca certs) surfaced a cluster of real bugs,
all fixed on the same branch:

- A `RESTORE_DATA_NICS` entry whose `local_ip` wasn't actually local
  made uvicorn's `create_server` raise `EADDRNOTAVAIL`, which
  `sys.exit()`s the whole process — one misconfigured listener took
  down the entire portal, not just itself. Even with a correct IP, a
  specific-IP listener on the main port collided (`EADDRINUSE`) on
  Linux. Fixed: the data port now defaults to `PORT+1`; each listener
  is preflight-`bind()`-tested and skipped with a logged reason rather
  than crashing the process; a later bind failure is logged, not fatal.
- A large chunked restore sat at a displayed ~99% with no log output —
  PVE's file-restore download stream has no `Content-Length` in
  practice. Fixed by sending the file's known size from
  `file-restore/list` through to `RestoreJob.source_size`, so the
  progress bar tracks a real chunk count.
- Iterating on `local_ip` left stale certs behind, and a restart
  landing mid-write left a fresh key next to a stale cert, raising a
  fatal `KEY_VALUES_MISMATCH`. Fixed: certs are tagged as this app's
  own and written atomically; a broken pair is only ever re-issued when
  it's self-signed (ours); a broken CA-issued cert is left untouched
  and logged as an error, never overwritten.
- `PVE_VERIFY_SSL=true` trusted only `certifi`, not the container's
  system CA store, so an internal-CA Proxmox cert 500'd login with
  "self-signed certificate in certificate chain." Fixed to use
  `ssl.create_default_context()` (system store, `SSL_CERT_FILE`
  honored) for `true`.
- The TLS downgrade ladder didn't step down on a *runtime* cert-trust
  failure — with `verify` and no CA install, an untrusting guest failed
  the whole restore instead of retrying one rung down. Fixed by
  classifying a failed fetch (0 bytes moved = handshake/trust failure)
  and retrying `verify → insecure` before applying `ON_UNMET`.
- Windows `Invoke-WebRequest` + skip-verify proved fragile on PS 5.1 (a
  bare validation-callback closure throws inside schannel). Fixed the
  callback and `SecurityProtocol` handling, but real `curl.exe`
  (Win10 1803+/Server 2019+) became the preferred Windows tool anyway —
  its schannel `-k` behavior is far more predictable.

## Multi-file / directory bundle restore (issue #24)

**The first streaming-bundle-builder implementation reintroduced the
exact disk-exhaustion risk it was supposed to avoid, just worse
(2026-09-01).** It downloaded *every* selected item to its own local
temp file before starting to build the output bundle at all, so peak
local disk usage was every source item combined plus the full output
bundle, all at once. Live-tested against a real multi-item selection on
the hosting LXC container, this exhausted the container's own rootfs
(`OSError: ... No space left on device`). The fix didn't need the
zero-buffer bridge tracked separately as issue #25 — only interleaving
download and build so at most one item's temp file exists alongside the
growing output bundle at any moment. A regression test snapshots the
temp directory mid-build to confirm no more than one item's temp file
exists on disk at a time.

**A live ~1.5GB/3-item restore projected tens of thousands of chunks
and looked stuck at 100% almost immediately (2026-09-02).** Fixed two
things together: `_write_chunks_to_scratch()` gained a
`total_bytes_hint` so a bundle's already-known total size sets
`progress_total` immediately instead of chasing it upward chunk by
chunk; and DNT was extended to bundles too (previously single-file
only) via an optional `local_path` on the download token, so
`GET /api/restore-downloads/{token}` streams the already-built local
bundle file instead of trying to re-proxy a synthesized multi-item
bundle from PVE (which it can't hand back as one item).

**Immediately after DNT-for-bundles shipped, the progress display
regressed one phase later (2026-09-02).** `progress_total` was left at
the chunked-write unit count even when DNT (not the chunked path)
actually ran, so the displayed percentage rounded to 100% the instant
the fetch finished — before extraction or verification had even
started, the same "looks done/stuck early" symptom the earlier fix was
written to prevent, just relocated. Fixed by rescaling to 3 units
(transfer/extract/verify) whenever DNT is the path actually taken.
Separately confirmed the same day: the embedded manifest file was left
behind in the destination permanently after a successful restore — now
removed via a best-effort guest-exec cleanup call after verification
succeeds.

**A real directory restore landed its contents one level too deep
(2026-09-02).** Restoring a `Downloads` directory produced a doubled
`Downloads/Downloads/` in the destination. Root cause: the bundle
builder re-prefixed each source zip member's filename with
`{item_name}/` on top of what was already there — but PVE's own zip
encoding for a directory selection already roots every entry under the
directory's own name. The *ordinary browser-download* multi-select
feature (`download_bundle()`) had the exact same bug, unnoticed until
now because its own tests never covered a directory selection's exact
output paths — fixed in the same change. This was the first real,
live-verified directory selection either code path had actually seen.

**A large Windows directory build looked hung for 282 seconds with no
progress signal at all (2026-09-02).** The whole bundle-build phase
(download each item, add to the output bundle) had no progress
callback, unlike the write-to-guest phase. Fixed by threading an
`on_item_progress` callback through the build path, throttled to log on
a new item and at most every ~5s. The very next fix, the same day: the
progress bar itself sat frozen at a flat "0%" during an active
download, because the item in question had no `Content-Length` header
from PVE, so `progress_current`/`progress_total` were never touched and
stayed at their field defaults — which `progress_percent` computed as a
real-looking "0%" rather than recognizing "nothing measured yet." Root
cause was the default itself: `progress_total` defaulted to `1`, not
`0`, even though the guard clause (`<= 0: return None`) was clearly
written to treat `0` as "nothing to report." Changed the default to
`0` — no call site needed to change, since every real assignment
already overwrites the default outright.
