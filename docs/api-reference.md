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
