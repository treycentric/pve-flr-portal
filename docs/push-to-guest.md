# Push-to-Guest Restore

Living developer reference for restoring a file (or files) directly
into a *running* guest via `qemu-guest-agent` (QGA) — a separate
feature from browse+download, on its own privilege model (see
[`architecture.md`](architecture.md) for the hard constraint that keeps
the two decoupled). For the underlying QGA/PVE API facts this mechanism
is built on, see [`api-reference.md`](api-reference.md). For dated
bug-investigation narrative, see [`archive/`](archive/).

VM only — containers have no `qemu-guest-agent`, so `file_write`/
`guest_exec` are always unavailable for a CT.

## Capability detection

`GET /api/restore-capabilities?type=<qemu|lxc>&vmid=<id>`
(`backend/guest_agent.py`) gathers, before any restore is offered:

| Check | How | Tells us |
|---|---|---|
| Agent enabled in VM config | `GET /nodes/localhost/qemu/{vmid}/config` → `agent` field | Is QGA wired up at all |
| What the guest agent allows | `POST .../agent/info` (`supported_commands[]`, per-command `enabled`) | Whether `guest-file-write`/`guest-exec`/`guest-file-read` are allowed on *this* guest |
| Guest OS | Same `agent/info` call, or `get-osinfo` | Windows vs. Linux/BSD conventions (scratch-dir path, hasher choice) |
| Caller's privilege | `GET /access/permissions?path=/vms/{vmid}` | Which `VM.GuestAgent.*` grants the session holds |
| PVE version | `GET /version` | PVE 8 only has coarse `VM.Monitor` — restore unavailable regardless of other checks |

Response shape:
```json
{
  "agent_running": true,
  "pve_version_ok": true,
  "guest_os_family": "linux",
  "file_write": {"available": true, "reason": null},
  "guest_exec": {"available": false, "reason": "guest-exec not enabled in qemu-guest-agent config"},
  "verify_supported": false
}
```
`reason` is always populated when `available: false`, so the UI can
explain why a path is greyed out. This is a UI convenience only — every
privilege is re-checked server-side on every actual restore step, never
trusted from the capability response alone.

**`agent/info` is the critical path for availability, and shares a
single-command-at-a-time channel with every other QGA call.**
`backend/guest_agent_lock.py`'s `guest_agent_command(vmid)` (an async
context manager holding a per-vmid `asyncio.Lock`) serializes this
app's own `agent/*` calls for the full request/response cycle — used
by every actual agent call site (`guest_agent.py`, `pve_client.
write_guest_file()`, `guest_browse.py`), never plain PVE API calls like
`/config` that don't touch the QGA channel. `call_with_retries()`
retries only on a definite `httpx.HTTPStatusError`, never a
timeout/connection error — a client-side timeout doesn't prove the
in-guest command was abandoned, and retrying anyway can desync the
channel until the agent restarts. PVE/QEMU surfaces genuine external
contention (another admin's `qm agent` call, a scheduled backup's
fs-freeze) as a completed error response, not a timeout, so retrying on
that specific signal is safe.
`GUEST_AGENT_MIN_COMMAND_GAP_SECONDS` (default 0/off) adds an optional
minimum gap between this app's own consecutive commands on one guest,
as a courtesy to other channel users during a long multi-chunk restore.

## Single file: single-shot write vs. chunked guest-exec concat

The write mechanics decide the split, not a user-facing "quick vs.
full" choice:

- **Single-shot write (`file_write`) — quick restore.** The whole
  content fits in one `agent/file-write` call (≤61440 bytes, see
  [`api-reference.md`](api-reference.md)). No `guest-exec` anywhere —
  works even where exec is blocked. Needs only `VM.GuestAgent.FileWrite`.
  Lands `root:root`/SYSTEM, mode `0644`, fresh mtime.
- **Chunked write + guest-exec concat (`guest_exec`) — full restore.**
  Anything the single-shot path can't do in one call: larger content,
  or metadata/verify requested. Each chunk writes to
  its own file in a per-restore scratch directory (discovered per OS
  from `agent/info`'s reported guest OS — `%TEMP%` on Windows, `/tmp`
  or `$TMPDIR` on Linux/BSD), then one `guest-exec` concatenates them
  in order into the destination, then the scratch directory is removed.
  Needs `VM.GuestAgent.Unrestricted` (the concat step alone forces
  this, independent of whether metadata/verify are also requested).

The backend decides the actual mechanism at write time from three
independent facts: content needing >1 chunk, "restore metadata"
checked, or "verify" checked. Any one of the three means `guest-exec`
and therefore `Unrestricted`; none of them means the single-call
`FileWrite`-only path.

Destination is always a directory (`dest_dir`, existing, user-chosen),
never a full file path — a single file lands at `dest_dir/<original
filename>`; a directory/multi-file selection preserves its relative
structure under `dest_dir`.

**Metadata and verify are independent opt-ins**, both off by default,
shown only when `Unrestricted` is available:
- **Restore metadata** — mtime only (`file-restore/list`'s schema has
  no uid/gid/mode). A follow-up `guest-exec` applies it (`touch -d
  @<ts>` on Linux/BSD; PowerShell `(Get-Item).LastWriteTime = ...` on
  Windows — `cmd` has no built-in for this).
- **Restore original owner/permissions** (Linux/BSD only; separate
  checkbox from metadata, issue #20) — a *second* `file-restore/
  download?tar=1` call on the same `source_filepath` recovers real
  uid/gid/mode from the tar header (`_fetch_source_ownership`, reads
  only a bounded 16KB prefix, never re-downloads full content), applied
  via `chown`/`chmod` guest-exec. **`tar=1`'s output is not reliably
  one format** — it can come back as a genuine plain tar or
  zstd-framed, for the same endpoint/parameter; detect via the zstd
  magic number (`\x28\xb5\x2f\xfd`) and handle either
  (`_decompress_prefix`'s streaming zstd reader, since a deliberately
  truncated prefix has no known size/complete frame for a single-shot
  decompress). A fetch/parse failure degrades to "skip this cosmetic
  step," logged, not a failed job. Disabled in the UI for a Windows
  guest — see "Known gaps" below.
- **Verify** — sha256, computed app-side while streaming the source
  from `file-restore/download` (no extra guest round-trip for that
  half), compared against a guest-side hash from one `guest-exec`
  (`sha256sum` on Linux/BSD; `certutil -hashfile <path> SHA256` on
  Windows — a `cmd`-only tool, no PowerShell dependency).

**Guest-exec reliability notes, all confirmed live, all current
behavior:**
- Windows commands needing more than one embedded double-quoted segment
  on a single `cmd /c` line are unreliable (`cmd.exe`'s own quoting).
  `_ensure_destination_dir()`, `_verify_destination_exists()`, and
  `_concat_chunks()`'s Windows path all use PowerShell with
  single-quoted literals instead (`New-Item -Force`, `Test-Path
  -LiteralPath`, a small `[System.IO.File]::Open`/`CopyTo` script for
  concatenation) — never `cmd /c copy /b`.
- A destination write proactively clears the **ReadOnly** attribute
  (ubiquitous on `desktop.ini` and similar) before every write, when
  `Unrestricted` is available — Windows' `CreateFile` refuses write
  access to a ReadOnly file for any principal, including SYSTEM.
  Covers Linux too (owner-write bit, immutable flag). Best-effort and
  silent — nothing to clear when the destination doesn't exist yet.
- `_ensure_destination_dir()` creates the destination's parent
  directory up front; `_verify_destination_exists()` checks
  immediately after concatenation — a Windows shell command's exit
  code alone isn't trustworthy evidence that a write actually landed.
- `pve_client.check_path_safe()` gates any destination before it's
  embedded in a shell/PowerShell command string — but only once
  `guest-exec` is actually going to run; a destination with shell
  metacharacters that only ever needs plain `agent/file-write` must
  keep working without `Unrestricted`.
- The source stream is read via `response.aiter_bytes()`, never
  `response.aread()` — at most one `DEFAULT_CHUNK_SIZE_BYTES` piece of
  raw content (and one piece's wire string) is ever held in memory at
  once, regardless of file size. A streaming sha256 digest covers
  verify without a second pass.

## Direct Network Transfer (DNT)

A third mechanism on top of the two above: the guest fetches the file
itself over its own network, at normal throughput, instead of moving
every byte over the QMP/virtio-serial control channel (the chunked
guest-exec path's real bottleneck — one sequential round-trip per
≤61440-byte chunk).

**Mechanism:**
1. Backend writes a small bootstrap script into the guest via
   `agent/file-write` — a one-liner against a per-job, single-use,
   short-TTL, signed download URL this app serves.
2. `guest-exec` runs it. The guest fetches the file directly from the
   portal over its own NIC — not through PVE, not through QMP.
3. A follow-up `guest-exec` fixes ownership/mode/mtime as requested,
   same as the chunked guest-exec path.

**Auth:** a random, single-use token scoped to exactly one job's one
file, short TTL (`RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS`), stored
server-side (`backend/restore_download.py`, same in-memory/
lost-on-restart tradeoff as `auth._sessions`). The guest never sees the
operator's PVE ticket. `GET /api/restore-downloads/{token}` is
deliberately unauthenticated — the guest has no PVE session — and
consumes the token on first use or expiry. For a bundle (below), the
token's `local_path` is set and the endpoint streams the already-built
local bundle file instead of re-proxying from PVE.

**Requires:** `VM.GuestAgent.Unrestricted` (same as the chunked
guest-exec path, plus "the guest can reach this app" as a new
dependency); guest→portal IP
reachability; a usable fetch tool in the guest; `guest-exec` unblocked.

**Fetch-tool fallback chain**, probed via `guest-exec` (cheap
`--version`/`where`/`command -v` checks, cached per job) rather than
assumed:
- **Windows:** `curl.exe` (preferred — more predictable TLS than
  `Invoke-WebRequest` on PS 5.1) → `Invoke-WebRequest` → `certutil
  -urlcache -f` → `bitsadmin /transfer` → `cscript`/VBScript
  (`WinHttpRequest` COM object; **detected but not actually usable
  yet** — needs a staged `.vbs` file via `agent/file-write`, not wired
  up, so a guest whose only tool is `cscript` falls back to the chunked
  guest-exec path).
- **Linux/BSD:** `curl` → `wget` → `python3`/`python`
  (`urllib.request`) → bash's `/dev/tcp` (hand-rolled raw HTTP GET, no
  external binary, but bash-specific — not reachable under a POSIX
  `/bin/sh` guest-exec shell).
- If nothing on the list is available, DNT simply isn't offered — the
  same silent fallback to the chunked guest-exec path that happens when
  `Unrestricted` isn't granted.

**Network segmentation: one data-plane NIC per non-routable subnet.**
The existing interface keeps serving the UI and outbound PVE calls
(management plane) — unchanged. N additional data-plane interfaces,
one per subnet that isn't routable to the others, each serving *only*
the token-gated download endpoint, firewalled so inbound traffic can
reach only that one path. A compromised guest on one subnet can
therefore reach, at most, one narrow self-expiring endpoint on the NIC
facing its own subnet — never the UI, never PVE-management, never a
NIC facing an unrelated subnet.

`RESTORE_DATA_NICS` (JSON array of `{cidr, local_ip, hostname?}`, empty
by default — zero behavior change until an admin opts in) configures
this. `select_data_nic()` matches a guest's own reported IP(s) (`agent/
network-get-interfaces`) against the configured subnets, first match
wins, `None` if nothing matches — never guesses. `run.py` runs one
additional `uvicorn.Server` per distinct data-NIC IP, in the same
process as the main listener (required: the token store and job
manager are both in-memory/process-local, so a guest's fetch has to
land in the same process that minted its token), binding each to that
NIC's specific IP, never `0.0.0.0`. The unconfigured default keeps the
single-listener `uvicorn.run(..., reload=True)` dev path, which can't
run multiple concurrent `Server` instances.

Docker's default bridge/NAT networking doesn't give a container the
host's real LAN IP — a `RESTORE_DATA_NICS` entry naming a real
VM-subnet IP needs Docker's host-networking mode or `macvlan`/`ipvlan`
to bind correctly; LXC gets the real interface directly via `pct set
-netN`.

### HTTPS on the data plane

NIC segmentation limits *who can reach* the listener but not passive
eavesdropping on the segment — a sniffer captures the token and the
file bytes. HTTPS is on by default with a configurable security policy:

- **Three modes + a downgrade ladder.** `verify` (full chain + IP/
  hostname validation in the guest), `insecure` (encryption only, never
  touches a guest trust store), `plaintext` (HTTP). Two knobs:
  `RESTORE_DATA_NIC_TLS_PREFERRED` (default `verify`) and
  `_MINIMUM` (floor, validated ≤ PREFERRED). Per guest, the app resolves
  the strongest mode the detected fetch tool can do, stepping
  `verify → insecure → plaintext` down to MINIMUM — clamped up to
  `insecure` when PREFERRED isn't `plaintext`, since there's no second
  plain-HTTP listener. `RESTORE_DATA_NIC_TLS_ON_UNMET` (`fallback` to
  the chunked guest-exec path, or `fail`) decides when nothing qualifies.
  `RESTORE_DATA_NIC_TLS_MIN_VERSION` (`1.2`/`1.3`) sets the listener's
  TLS floor.
- **Per-tool capability:** skip-verify (`insecure`) works for `curl -k`,
  `wget --no-check-certificate`, Python's unverified `ssl` context,
  `Invoke-WebRequest`'s callback, WinHttpRequest's ignore-flags — but
  **not** `certutil` (WinINet, no flag), `bitsadmin` (one-shot form),
  or bash `/dev/tcp` (no TLS at all); those raise and the caller steps
  down the ladder. Trusting an injected CA works for every Windows tool
  and every POSIX tool honoring the system store — the more capable
  path on Windows, since it's the only way `certutil`/`bitsadmin` reach
  HTTPS at all.
- **Server identity:** `RESTORE_DATA_NIC_TLS_CERT_FILE`/`_KEY_FILE`/
  `_CA_FILE`. Auto-generated (if absent) with `IP:<addr>` SANs for
  every configured data-NIC IP — an IP-literal URL needs IP SANs, not a
  CN. An optional per-NIC `hostname` puts a DNS name in the URL and SAN
  instead. Regenerated on startup whenever the configured SAN set
  changes; an admin-supplied cert is never regenerated (a mismatch logs
  a warning, `verify` clients reject it until reissued).
- **Guest trust-store management:** `RESTORE_DATA_NIC_TLS_INSTALL_CA`
  (`never`/`if-missing`/`always`, default `never`). Installs the CA PEM
  via `agent/file-write` + `certutil -addstore -f Root` (Windows) or
  `update-ca-certificates`/`update-ca-trust extract` (Linux).
  `guest_ca.is_ca_cert()` refuses to install anything whose first cert
  isn't a real CA.
- **Fallback semantics:** a CA-install failure steps the job down to
  `insecure` when the ladder allows it. A fetch failure classified as a
  TLS handshake/trust failure (`is_tls_negotiation_failure` — moved
  zero bytes) retries one rung down; any other fetch failure (refused
  connection, mid-transfer error, disk full) is a hard failure
  regardless of the ladder.

## Multi-file / directory restore-to-guest

Extends A/B/C to a multi-select or whole-directory restore, reusing
`/api/download-bundle`'s existing `item: list[{filepath, name, leaf}]`
convention — no new PVE API surface. A directory selection already
means the full recursive tree, since PVE's own zip encoding for a
directory root already nests everything under it.

- **`RestoreJob.items: list[BundleItem] | None`** — `None` means an
  ordinary single-file job (`source_filepath`/`source`/`destination`
  keep their single-file meaning); a non-empty list means a bundle job,
  where `destination` is the target directory the bundle extracts
  into. `run_restore()` dispatches to `_run_bundle_restore()` vs.
  `_run_single_file_restore()`.
- **Extraction format, decided by actually probing the guest**
  (`restore_bundle.probe_tar_zst_support`) — a tiny known-good
  `.tar.zst` blob is written and actually extracted; only a correct
  exit code *and* correct content count as capable, never a
  `--version`/`--help` string. If the guest can extract `.zst` natively,
  PVE's raw `.tar.zst` streams straight through (chunked write or DNT)
  and `tar -xf` extracts it — no server-side repackaging. If not, the
  app re-bundles server-side into `.zip` (Windows, `Expand-Archive`) or
  `.tar.gz` (Linux/BSD) before anything reaches the guest.
- **Verify via an embedded manifest, checked entirely guest-side.**
  While streaming each item into the output bundle, the backend
  computes its SHA256 and writes a `sha256sum -c`-compatible manifest
  as one extra bundle entry. After extraction, one `guest-exec` call
  verifies everything at once (`sha256sum -c` on Linux/BSD; a short
  PowerShell script using `Get-FileHash` on Windows) — no app-side
  per-file hash map. The manifest file is removed via a best-effort
  guest-exec call after verification succeeds (a cleanup failure logs a
  note, doesn't fail the restore). mtime is free — both tar and zip
  preserve each entry's original modified time, restored automatically
  by extraction.
- **Streaming bundle builder** (`restore_bundle.build_bundle()`):
  downloads each selected item to its own local temp file one at a
  time (never all items' temp files on disk simultaneously — peak disk
  usage is one item plus the growing output bundle, not every item's
  combined), adds it to the open output bundle via a background thread
  (`asyncio.to_thread`, since `tarfile`/`zipfile` are blocking), deletes
  that item's temp file before moving to the next. `_HashingReader`
  lets the archive library's own chunked reads double as the manifest
  hash computation — no second pass over content. Directory-marker
  entries in a fetched source zip are skipped, not treated as
  zero-byte manifest lines. Not yet built: a true zero-buffer
  `os.pipe()` bridge (tracked separately, issue #25) — staging through
  local temp files is simpler and accepted unless disk usage/throughput
  becomes a real problem.
- **Direct Network Transfer applies to bundles too** — the
  already-built local bundle file streams from disk via the
  download-token endpoint (`DownloadToken.local_path`) rather than
  re-proxying from PVE, since PVE can't hand back a synthesized
  multi-item bundle as one item.
- **Progress** is phase-coarse (building / transferring / extracting /
  verifying), not per-file. A bundle's `progress_total` is set to the
  real expected unit count up front when known (the source is already
  fully materialized locally before the write phase starts, unlike a
  streaming single-file download) — 3 units (transfer/extract/verify)
  when DNT is the path actually taken, since DNT fetches the whole
  bundle in one shot with no separate concat step.

## Known gaps

- **Windows ACLs are not restorable through this app, confirmed
  infeasible via any Proxmox-exposed API, not just unimplemented.**
  Proxmox's file-restore helper only ever emits `tar`/`zip`, and
  neither format can represent a Windows Security Descriptor; no other
  Proxmox API surfaces it either (`qemu-guest-agent` only talks to a
  *live* guest, never a backup snapshot). A known, upstream-tracked gap
  in Proxmox's own ecosystem. The "Restore original owner/permissions"
  checkbox is disabled for a Windows guest for exactly this reason.
- **Windows software RAID (Storage Spaces, Dynamic Disks/LDM) isn't
  readable through the portal** — PBS's helper VM is a minimal Linux
  environment that can't assemble Windows' proprietary volume-manager
  metadata. Out of scope; recovering from it needs Proxmox's own
  `proxmox-backup-client map` + helper-VM workaround.
- **`cscript`/VBScript fetch-tool support is detected but not wired
  up** — needs a staged `.vbs` script via `agent/file-write`, which
  `_try_direct_network_transfer()` doesn't do. Falls back to the
  chunked guest-exec path.
- **"Original location" restore (issue #68) for a plain (non-LVM)
  partition is ordinal-position-only** — correlated to the running
  guest's own disk numbering purely by position in PVE's own
  file-restore root listing, since nothing in the API exposes a
  guest-independent disk identity. Windows resolves this more reliably
  via real bus-address matching (`Get-PhysicalDisk`'s `BusType` +
  `Win32_DiskDrive.SCSIBus`/a PnP `LocationInfo` LUN for `sata`/`scsi`
  respectively — **not** `Win32_DiskDrive.InterfaceType` alone, which
  isn't a reliable bus discriminator); Linux and ambiguous/multi-disk
  Windows buses still fall back to the ordinal guess. A guest with
  mixed bus types is the scenario most likely to break this and hasn't
  been live-verified.
- **`file-restore/list`'s `size` field is `0` for disk/folder-shaped
  entries** — only meaningful for leaf files; don't use it for
  disk-level size display or corroboration.
- **A guest that migrates mid-restore isn't handled** — the resolved
  node (see `architecture.md`'s "`localhost` node segment" risk) goes
  stale; the job fails cleanly and can be re-run, not a supported
  scenario.
