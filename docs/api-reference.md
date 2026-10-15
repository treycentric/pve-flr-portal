# API Reference — PVE file-restore and QEMU Guest Agent

This reference documents the undocumented or under-documented Proxmox API surfaces this app
depends on. None of this is in Proxmox's published API reference
(`https://pve.proxmox.com/pve-docs/api-viewer/`) beyond the bare
parameter list — it's reverse-engineered from the real GUI's traffic,
Proxmox's own source, and live testing against real guests. Each fact
below is confirmed, not guessed; if a future PVE/QGA version behaves
differently, update this doc from the same kind of evidence rather than
assuming the old facts still hold.

If a new gap shows up (auth, download/extract, edge cases), repeat the
same capture-from-real-traffic approach and append the findings here.

## `file-restore/list`

```
GET https://<pve-host>:8006/api2/json/nodes/localhost/storage/<storage-id>/file-restore/list
    ?volume=<storage-id>:backup/<vm|ct>/<vmid>/<ISO8601 backup timestamp, Z-suffixed>
    &filepath=<path>
```

- The node segment is always the literal string `localhost`, not a real
  hostname — PVE resolves it to whichever node the request lands on,
  and proxies cluster-wide, so this is correct on a multi-node cluster
  too.
- `filepath=/` for the root is a literal, unencoded `/`. Any path deeper
  than root is base64-encoded first, then URL-encoded — the two cases
  use different encodings, don't assume one scheme for both.
- `volume` is `<storage-id>:backup/<vm|ct>/<vmid>/<ISO8601 timestamp>Z`
  — the familiar PVE volid shape.
- Both `file-restore/list` and `file-restore/download` are marked
  `"allowtoken": 1` in Proxmox's published schema — a scoped API token
  works, no session-ticket fallback needed. Permission required is
  `"You need read access for the volume"` (`"user": "all"` — any
  authenticated principal with read access, no special admin role).
- `PVE::Storage::check_volume_access` requires, for a `backup`-type
  volume, *both* `Datastore.AllocateSpace` on `/storage/{storage}` and
  `VM.Backup` on `/vms/{vmid}` — `Datastore.Audit` alone is not
  sufficient (confirmed via 403s). `VM.Audit` is separately needed for
  guest-name resolution (`/cluster/resources`), not for file-restore
  access itself. See README's "Provisioning access" for the `pveum`
  commands this implies.
- Response: an array of `{filepath: string (base64), leaf: boolean,
  mtime?: integer (unix ts), size?: integer, text: string, type:
  string}`. **Navigability is governed by `leaf`, not `type`** — a
  virtual disk (e.g. `drive-scsi0.img.fidx`) has `type: "f"` but `leaf:
  false`; it looks like a file but is drillable, and drilling into it
  reaches the guest's real filesystem. Branching on `type == "d"`
  instead gets this backwards.
- No `uid`/`gid`/`mode` field exists anywhere in this response, on any
  PVE version — only `mtime` and `size`. Ownership/permissions are only
  recoverable via `file-restore/download?tar=1` (below), not this
  endpoint.
- Cold-lookup latency: an uncached root listing is typically under
  100ms; drilling into an actual disk image costs ~3s — the ephemeral
  helper VM booting to read the guest filesystem.

### Disk structure: `part` and `lvm`

Drilling into a root-level disk entry never lands directly on the
guest's filesystem — PVE always nests it one level deeper, under one or
both synthetic classification folders:

- **`part`** — the disk's raw partition table, one numbered folder per
  partition (`part/1`, `part/2`, ...). The only folder present for a
  disk with no LVM on it.
- **`lvm`** — present only when the disk holds a physical volume that's
  part of an LVM volume group, one folder per volume group name
  (`lvm/<vg>`). A volume group spanning multiple virtual disks shows an
  **identical** `lvm/<vg>` subtree under every disk it spans — PVE's
  helper VM attaches every disk in the snapshot at once and assembles
  LVM across them, the same as a real boot would.

This app elevates each distinct VG to a root-level entry (deduped
across the disks it spans) and hides the now-redundant `lvm` folder
from each disk's own listing; `part` is flattened away when it's left
as a disk's only remaining child (`main.py`'s
`_discover_lvm_volumes`/`_disk_level_entries`). Only `part` and `lvm`
have been observed as classification folders under a disk. Not
confirmed either way: whether an analogous folder exists for other
block-layer assemblies (software RAID/`mdadm`, ZFS).

### Unmountable partitions/disks

Browsing into a `part/N` (or `lvm/<vg>/<lv>`) folder PVE's helper VM
can't mount any filesystem on returns a genuine PVE API error — e.g.
`mounting 'drive-sata2.img.fidx/part/2' failed: all mounts failed or no
supported file system` — never an empty/degenerate listing. This is
the signal `main.py`'s `_filter_unmountable_children`/`_readable_entries`
key off: an actual `httpx.HTTPStatusError`, never "the listing came
back empty," since a genuinely empty-but-mountable partition/volume
also lists empty and must not be hidden by the same logic.
`_readable_entries` takes a `digits_only` flag since a disk's numbered
partitions and an LVM volume group's arbitrarily-named logical volumes
need different matching; `_is_partition_listing`/
`_is_lvm_volume_group_listing` pick the right one (mutually exclusive —
a real disk label is never `LVM <name>`). A disk with nothing visible
anywhere under it (`_disk_has_visible_content`) is hidden from the root
listing entirely, unless it contributed to an elevated LVM volume
group (whose content already shows up at root via that group's own
elevated entry).

## `file-restore/download`

```
GET /api2/json/nodes/{node}/storage/{storage}/file-restore/download
    ?volume=<volid>
    &filepath=<base64-path-or-/>
    &tar=<0|1>            (optional, default 0)
```

- `tar=1` downloads a directory as `tar.zst` instead of the default
  `zip`.
- `returns: {"type": "any"}` — a raw byte stream, not JSON.
- Same `volume`/`filepath` encoding as `file-restore/list` above. Same
  permission requirement.
- **`tar=1`'s archive carries real uid/gid/mode/mtime** — unlike the
  JSON listing API, this is the only way to recover a restored file's
  original ownership and permissions (Linux/BSD only; NTFS has no
  uid/gid/mode concept, and no Proxmox API surfaces a Windows Security
  Descriptor either — a confirmed upstream gap, not something fixable
  from this app).

## `host`-type backups

A `host`-type backup (`proxmox-backup-client backup --backup-type
host`) is PBS's generic file-level backup type — `backup-type` has
exactly three values (`vm`, `ct`, `host`), no fourth type exists. Its
volid is `<storage-id>:backup/host/<hostname>/<ISO8601 timestamp>Z` —
same shape as `vm`/`ct`, with a hostname in place of a numeric vmid.
It's stored as a `pxar.didx` archive, the same format `ct` backups
use (not an `img.fidx` disk image like `vm`). This app's browse/tree/
download code is gated on `== "vm"` for every disk/LVM-specific code
path (`_apply_lvm_view`, `_is_partition_listing`, etc.), so a `host`
volid gets the same flat-pxar-tree treatment as a `ct` backup, with
no VM-specific logic ever applying to it — browse and download work
with no app-code changes, including through an encrypted host backup
(encryption key uploaded to the PVE storage entry's own Encryption
Key tab, README's "Browse Host Backups") — PVE decrypts transparently
during file-restore, same as for any other encrypted backup.

**PVE's own `PVE::Storage::PBSPlugin::list_volumes`
(`pve-storage.git`, `src/PVE/Storage/PBSPlugin.pm`) drops `host`
backups from every storage content listing before any permission
check runs:**

```perl
next if !($btype eq 'vm' || $btype eq 'ct');
next if $bid !~ m/^\d+$/;
```

Both lines gate independently — the first restricts the type, the
second separately requires the backup-id to be numeric, which a
hostname never is — so both have to be patched together. This is the
function behind both PVE's own "Backups" content-tab listing and this
app's `pve_client.list_backup_archives()` (via `GET /nodes/localhost/
storage/{storage}/content?content=backup`) — there is no other PVE
API surface this app is willing to use instead (CLAUDE.md's "never
talks to PBS directly"). Neither line runs after any ACL check, so no
permission grant, for any caller including `root@pam`, routes around
either one.

A few lines further down, `list_volumes` also sets `vmid =>
int($bid)` and `subtype => $btype eq 'vm' ? 'qemu' : 'lxc'` for every
entry — wrong for a `host` backup (a hostname isn't a vmid, a host
backup isn't a container), and `int()` on a non-numeric `$bid` prints
a warning to stderr on every listing query. This app doesn't read
either field (it parses guest type from the `volid` itself, never
`subtype`), but both are patched anyway to keep PVE's own output
clean.

**The fix: `deploy/patch-pve-host-backups.sh`** (CLAUDE.md's "last
resort" patching exception) — an opt-in, exact-match-or-refuse patch
to the lines above, installed with a dpkg hook so it survives
`pve-storage` package updates. Needs to run on every node in a cluster
— the content-listing call can be served by any node. See that
script's own header for the full rationale, and README's "Browse Host
Backups" for the user-facing setup flow. The patch also unlocks host
backups in PVE's own native "Backups" tab and File Restore button,
not just this app — it's the same underlying endpoints either way.

**Known rough edge, left as-is:** PVE's "Backups" grid renders the
same row actions for every backup regardless of type — `Restore`,
`Show Configuration`, and `Change Protection` all assume a VM/CT guest
behind the entry, none of which exist for a `host` backup, and none of
the three work correctly on one. `File Restore` is the one that
matters here and it works fine. Fixing the other three would mean
patching PVE's own frontend to recognize `host`-type rows — out of
scope for this app.

**Permissions: `Datastore.Allocate` + `Datastore.Audit`, no narrower
grant exists.** Traced through `PVE::Storage::check_volume_access`
(`pve-storage.git`, `src/PVE/Storage.pm`): for a `backup`-type volid it
only takes the `Datastore.AllocateSpace` + `VM.Backup`-on-`/vms/{vmid}`
path (what `FileRestoreReader` grants) when `$ownervm` is defined —
and `PBSPlugin.pm`'s `parse_volname` only ever sets `$ownervm` when
`$btype` is `vm` or `ct` *and* the id is digits-only. For `host`,
`$ownervm` is always `undef`, so that branch never matches; every
other branch is for a different `$vtype` (`iso`/`vztmpl`/`import`,
`images`/`rootdir`, `snippets`), so a `host` backup falls through to
`die "missing privileges to access $volid"` unless the caller already
holds `Datastore.Allocate` on `/storage/{sid}`, checked unconditionally
before any of those branches. That's the only path — there's no
privilege between "nothing" and "storage admin" for a host backup
specifically.

`Datastore.Allocate` alone isn't sufficient either — a backup has to
pass `PVE::API2::Storage::Content`'s own listing gate first, which
needs `Datastore.Audit` *or* `Datastore.AllocateSpace`
(`Datastore.Allocate` is not in that list and does not imply either —
they're independent privilege bits). `FileRestoreReaderHost`'s
`Datastore.Allocate,Datastore.Audit` (README's "Browse Host Backups")
is the minimal pair that clears both gates.

PBS has its own, separate ACL layer the PVE storage entry's own
credential has to clear too: a PVE storage entry whose PBS-side
user/token only holds `DatastoreBackup` on the datastore can't see
backups it didn't itself create — `DatastoreBackup` is ownership-
scoped on the PBS side, not purely namespace-scoped. A different PBS
identity's backups (e.g. a separate service account doing host
backups directly via `proxmox-backup-client`) need `DatastoreReader`
(or broader) granted on PBS's own Permissions page for that namespace,
independent of anything in PVE.

## `qemu-guest-agent` (QGA) — `agent/file-write`, `agent/file-read`, `guest-exec`

```
POST /api2/json/nodes/{node}/qemu/{vmid}/agent/file-write
    {file: <path>, content: <string>}
```

- **One-shot, no append.** Only parameters are `file` and `content` —
  no `handle`/`offset`, no separate `agent/file-open` to hold a handle
  across calls. A second call to the same guest path truncates and
  overwrites the first.
- **Per-call ceiling: exactly 61440 characters/bytes**, confirmed via
  the server's own validation error at the boundary (60 KiB succeeds,
  70 KiB fails).
- **`content` is a raw literal string, not base64**, on the PVE/QGA
  version this was checked against (neither direction decodes/encodes
  regardless of the `encode` param) — confirmed by round-tripping an
  11-character non-base64-shaped string unchanged, and separately by
  round-tripping the full 0–255 byte range (via a Latin-1-decoded
  Python `str`, one byte per codepoint) byte-for-byte identical. The
  61440 ceiling is therefore 61440 raw bytes per chunk, not reduced by
  base64 overhead. `backend/restore_chunking.py`'s
  `DEFAULT_CHUNK_SIZE_BYTES = 61440` uses this directly. Re-verify
  against a meaningfully older PVE/QGA version before relying on this —
  an older version may encode as base64.
- **`agent/info`** (wraps QMP `guest-info`) is the capability-detection
  call — any `agent/info` failure (including "QEMU guest agent is not
  running" when the in-guest service isn't started, even with `agent:
  1` set in the VM config) should degrade to "unavailable," not error
  out (`backend/guest_agent.py`).
- **`guest-exec`/`guest-exec-status`** run/poll an arbitrary command in
  the guest. Used for: scratch-file concatenation (multi-chunk
  writes), metadata/ownership restore, checksum verification, and
  bundle extraction.

### `VM.GuestAgent.*` privileges

Five privileges (Proxmox access-control patch that introduced them):

| Privilege | Covers |
|---|---|
| `Audit` | issue informational QGA commands (incl. `agent/info`) |
| `FileRead` | read files from the guest |
| `FileWrite` | write files in the guest |
| `FileSystemMgmt` | freeze/thaw/trim filesystems |
| `Unrestricted` | issue arbitrary QGA commands |

**`guest-exec`/`guest-exec-status` are not named under any specific
privilege** — only `Unrestricted` covers "arbitrary" commands, so any
use of `guest-exec` for any reason requires it. There is no narrower
"exec" privilege. A single-chunk write with no metadata/verify/ownership
options needs only `FileWrite`; anything that needs `guest-exec` (a
second chunk, metadata restore, verify, ownership restore, bundle
extraction) needs `Unrestricted`.

## OIDC/SSO (`/access/openid/*`)

Covered in [`architecture.md`](architecture.md)'s "Auth & sessions"
section — the two-leg `auth-url`/`login` exchange, redirect-url
handling, and route-registration gotcha are auth/session mechanics, not
file-restore/QGA facts.
