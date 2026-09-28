"""PH.5 follow-on (issue #68): resolves a destination directory from the
browsed item's own *original* location on the guest, as a third option
alongside the existing Browse/Manual-entry destination pickers.

VM guests only - LXC containers can never reach this at all, since
push-to-guest restore (guest_agent.get_restore_capabilities) is gated on
qemu-guest-agent, which containers don't have (design_a/design_b are
always unavailable for guest_type == "ct" - see
test_get_restore_capabilities_lxc_skips_agent_calls). The restore modal
this feeds is simply never reachable for a container, so there is
nothing to resolve for one.

The file-restore browser only ever exposes a path *within* a partition
(or, for LVM, within a logical volume) - drive letters and Linux
mountpoints are guest-side state nothing in the backup itself records
(docs/plan.md §3's confirmed disk/part/lvm structure, and issue #66's
LVM-elevation/part-flattening on top of it - see
resolve_original_directory's docstring for the exact shapes this
parses). The only place that drive-letter/mountpoint mapping exists is
the *live* running guest, so this needs the same guest-exec channel
guest_browse.py already uses - the same "agent must be running and
reachable" constraint every other PH.5 restore path already has.

Deliberately conservative throughout: any crumb structure this doesn't
recognize, or any live lookup that comes back ambiguous/empty/failed,
resolves to "unavailable" with a human reason - never a guess. A wrong
destination here is a wrong write into a live guest, not a cosmetic
mistake.

**Disk identity for a plain (non-LVM) partition, Windows.** Confirmed
live 2026-09-27 (docs/plan.md §7 "original location" real-world
findings): PVE's disk label carries its own bus+index verbatim
(`drive-<bus><N>.img.fidx`), and for `sata`/`scsi` that maps to a real,
address-based identity in the running guest - not just attachment
order - via `_windows_disk_number_by_bus` below:
- `sataN` -> `Get-PhysicalDisk` `BusType=SATA` + `Win32_DiskDrive.
  SCSIBus == N`. Confirmed gap-tolerant (a cdrom sitting at `sata0`
  didn't shift `sata1`/`sata2`'s `SCSIBus` values).
- `scsiN` (virtio-scsi, PVE's default `scsihw=virtio-scsi-single`) ->
  `BusType=SAS` + the LUN from `DEVPKEY_Device_LocationInfo == N`.
  Confirmed gap-tolerant up to `scsi5`. `Win32_DiskDrive`'s own
  `SCSITargetId`/`SCSIBus` are useless here - `virtio-scsi-single` gives
  every disk its own controller instance, so those read `0`/`0` for
  every scsi disk regardless of PVE index.
- `ideN` -> `BusType=ATA`, but only ever verified with a single IDE
  disk present - PVE caps IDE at 4 slots and multi-IDE guests are rare,
  so this only resolves when exactly one ATA disk exists; falls back
  to the ordinal guess otherwise (see below), exactly like before this
  existed.
- `virtio` (virtio-blk, not virtio-scsi) was never tested - always
  falls through to the ordinal guess too.
- **Also confirmed 2026-09-27:** `Win32_DiskDrive.InterfaceType` is
  *not* a reliable bus discriminator on its own - a real SATA/AHCI disk
  reported `InterfaceType: IDE` here, indistinguishable from a genuine
  legacy-IDE disk without `Get-PhysicalDisk`'s modern `BusType` (which
  correctly separated `ATA`/`SATA`/`SAS`). Don't reach for
  `Win32_DiskDrive.InterfaceType` alone for this.

Any bus this can't resolve (an ambiguous/multi-disk `ide`, `virtio`, or
a guest-exec failure) falls back to the pre-existing ordinal-position
guess: the disk is correlated to the running guest's own disk numbering
purely by *ordinal position in PVE's own root file-restore/list
response* - the best available proxy for attachment order when no
address-based signal resolved it. A guest with multiple same-bus disks
that the address-based match can't disambiguate (or mixed bus types,
for Linux, which still only uses the ordinal guess) is the scenario
most likely to break this fallback. The existing restore modal already
shows the resolved destination path prominently before the user
confirms (and requires an explicit "I understand this overwrites..."
checkbox) - that's the real safety net if this guess is ever wrong, not
just the "disable when ambiguous" fallback below, which only protects
against an *empty* or *failed* lookup, not a wrong-but-plausible one.
"""
import json
import re
from dataclasses import dataclass

import httpx

from .auth import SessionData
from .pve_client import GuestExecTimeout, UnsafePathError, check_path_safe, list_path, run_guest_exec

_DISK_LABEL_RE = re.compile(r"^drive-(ide|sata|scsi|virtio)(\d+)\.img\.fidx$")
_BUS_TYPE_BY_PVE_BUS = {"ide": "ATA", "sata": "SATA", "scsi": "SAS"}
_LUN_RE = re.compile(r"LUN (\d+)")


@dataclass(frozen=True)
class OriginalLocationResult:
    available: bool
    directory: str | None = None
    reason: str | None = None


def _unavailable(reason: str) -> OriginalLocationResult:
    return OriginalLocationResult(available=False, reason=reason)


_GENERIC_UNAVAILABLE = "Could not determine the original location for this item."


async def resolve_original_directory(
    session: SessionData,
    vmid: str,
    guest_os_family: str | None,
    node: str,
    volume: str,
    crumbs: list[dict],
) -> OriginalLocationResult:
    """`crumbs` is the breadcrumb trail (as tracked client-side) of the
    folder the item(s) being restored live in - crumbs[0] is always the
    synthetic "Root" entry, crumbs[1:] are real file-restore/list labels
    in order. Three shapes are recognized, all stemming from issue #66's
    LVM-elevation/part-flattening (added to main.py after this module was
    first written - a real regression this fixed, not a defensive
    "just in case"):

    - Elevated LVM (the only shape reachable through today's UI for LVM):
      `LVM <vg>`/<lv>/... - root-level entries are now labelled this way
      instead of nesting under a disk's own `lvm` folder, so there's no
      disk/`lvm` indirection to strip here at all.
    - Flattened partition (the common case for a plain disk): a disk's
      `part` folder is dropped from the crumb trail whenever it was the
      disk's only child, so it's `<disk>`/<partition number>/... directly.
    - Unflattened partition: `<disk>`/`part`/<partition number>/... -
      `part` stays nested when something else sits alongside it (e.g. an
      un-elevated `lvm` folder, or some other, currently unobserved,
      category).
    """
    labels = [c.get("label", "") for c in crumbs[1:]]
    if len(labels) < 2:
        return _unavailable(_GENERIC_UNAVAILABLE)

    if labels[0].startswith("LVM "):
        vg = labels[0][len("LVM ") :]
        lv, *inner = labels[1:]
        return await _resolve_lvm(session, vmid, node, vg, lv, inner)

    disk_label, second, *rest = labels
    if second.isdigit():
        return await _resolve_partition(session, vmid, guest_os_family, node, volume, disk_label, int(second), rest)
    if second == "part" and rest and rest[0].isdigit():
        return await _resolve_partition(
            session, vmid, guest_os_family, node, volume, disk_label, int(rest[0]), rest[1:]
        )
    # Legacy/defensive: an un-elevated `lvm` folder directly under a disk
    # - not reachable through today's UI (issue #66 always hides it under
    # a disk's own listing), but handled here rather than assumed
    # impossible, same spirit as the rest of this module.
    if second == "lvm" and len(rest) >= 2:
        vg, lv, *inner = rest
        return await _resolve_lvm(session, vmid, node, vg, lv, inner)
    return _unavailable(_GENERIC_UNAVAILABLE)


async def _disk_ordinal(session: SessionData, volume: str, disk_label: str) -> int | None:
    """The disk's position among PVE's own (re-fetched, unsorted) root
    listing - see the module docstring's "known open assumption" for why
    this, and not something more authoritative, is what's available."""
    try:
        root_entries = await list_path(session, volume, "/")
    except httpx.HTTPStatusError:
        return None
    disk_labels = [e.get("text", "") for e in root_entries if not bool(e.get("leaf", True))]
    try:
        return disk_labels.index(disk_label)
    except ValueError:
        return None


async def _resolve_partition(
    session: SessionData,
    vmid: str,
    guest_os_family: str | None,
    node: str,
    volume: str,
    disk_label: str,
    partition_number: int,
    inner: list[str],
) -> OriginalLocationResult:
    if guest_os_family == "windows":
        disk_number = await resolve_windows_disk_number(session, vmid, node, volume, disk_label)
        if disk_number is None:
            return _unavailable("Could not match this item's disk to one in the running guest.")
        return await _resolve_windows_partition(session, vmid, node, disk_number, partition_number, inner)

    disk_number = await _disk_ordinal(session, volume, disk_label)
    if disk_number is None:
        return _unavailable("Could not match this item's disk to one in the running guest.")
    return await _resolve_linux_partition(session, vmid, node, disk_number, partition_number, inner)


def disk_label_for_partition_listing(crumbs: list[dict]) -> str | None:
    """Issue #77: given the crumb trail of a folder currently being
    listed, returns the disk label if - and only if - that folder's own
    children are the numbered partition folders directly (i.e. this is
    a disk's flattened listing, or an unflattened disk's `part`
    folder) - the same two shapes `resolve_original_directory` already
    recognizes, minus the LVM case (a volume group's children are named
    logical volumes, not partition numbers, so there's nothing here to
    annotate). Returns None for every other position (root, inside a
    partition/LV's own filesystem, etc.)."""
    labels = [c.get("label", "") for c in crumbs[1:]]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2 and labels[1] == "part":
        return labels[0]
    return None


async def resolve_windows_disk_number(
    session: SessionData, vmid: str, node: str, volume: str, disk_label: str
) -> int | None:
    """Address-based match first (see module docstring for what's
    confirmed live), falling back to the ordinal-position guess when the
    bus can't be resolved (ambiguous/multi-disk `ide`, untested
    `virtio`, or a guest-exec failure)."""
    disk_number = await _windows_disk_number_by_bus(session, vmid, node, disk_label)
    if disk_number is not None:
        return disk_number
    return await _disk_ordinal(session, volume, disk_label)


async def list_windows_drive_letters(session: SessionData, vmid: str, node: str, disk_number: int) -> dict[str, str]:
    """Issue #77: one batched `Get-Partition -DiskNumber` call for every
    partition on a disk, instead of resolve_original_directory's
    one-item-at-a-time query - used to annotate the browse tree's
    partition labels, not to resolve a single restore destination.
    Returns {} (never raises) on any guest-exec failure, same
    "unavailable, not a guess" posture as the rest of this module -
    the tree simply shows plain partition numbers when this comes back
    empty."""
    script = (
        f"@(Get-Partition -DiskNumber {disk_number} -ErrorAction Stop | "
        "Where-Object { $_.DriveLetter -and $_.DriveLetter -ne [char]0 } | "
        "Select-Object PartitionNumber, DriveLetter) | ConvertTo-Json -Compress"
    )
    try:
        exitcode, out, _err = await run_guest_exec(
            session, "vm", vmid, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], node=node
        )
    except GuestExecTimeout:
        return {}
    if exitcode != 0 or not out.strip():
        return {}
    try:
        rows = json.loads(out)
    except ValueError:
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    return {
        str(row["PartitionNumber"]): f"{row['DriveLetter']}:"
        for row in rows
        if row.get("PartitionNumber") is not None and row.get("DriveLetter")
    }


async def _windows_disk_number_by_bus(
    session: SessionData, vmid: str, node: str, disk_label: str
) -> int | None:
    """Address-based disk match for sata/scsi (see module docstring for
    what's confirmed live) - returns None for any unsupported bus,
    unresolvable/ambiguous result, or guest-exec failure, in which case
    the caller falls back to the ordinal-position guess exactly as
    before this existed."""
    match = _DISK_LABEL_RE.match(disk_label)
    if match is None:
        return None
    bus, index = match.group(1), int(match.group(2))
    wanted_bus_type = _BUS_TYPE_BY_PVE_BUS.get(bus)
    if wanted_bus_type is None:
        return None  # virtio (virtio-blk) - never tested, always falls back

    script = (
        "$phys = @{}; Get-PhysicalDisk | ForEach-Object { $phys[$_.DeviceId] = $_.BusType }; "
        "@(Get-CimInstance Win32_DiskDrive | ForEach-Object { "
        "$loc = $null; "
        "try { $loc = (Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID "
        "-KeyName DEVPKEY_Device_LocationInfo -ErrorAction Stop).Data } catch {}; "
        "[pscustomobject]@{DiskNumber=$_.Index; BusType=$phys[$_.Index]; SCSIBus=$_.SCSIBus; Location=$loc} "
        "}) | ConvertTo-Json -Compress"
    )
    try:
        exitcode, out, _err = await run_guest_exec(
            session, "vm", vmid, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], node=node
        )
    except GuestExecTimeout:
        return None
    if exitcode != 0 or not out.strip():
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    if isinstance(rows, dict):
        rows = [rows]

    candidates = [r for r in rows if r.get("BusType") == wanted_bus_type]
    if bus == "sata":
        matches = [r for r in candidates if r.get("SCSIBus") == index]
    elif bus == "scsi":
        matches = []
        for r in candidates:
            lun_match = _LUN_RE.search(r.get("Location") or "")
            if lun_match and int(lun_match.group(1)) == index:
                matches.append(r)
    else:  # ide - only resolvable when exactly one ATA disk is present
        matches = candidates

    if len(matches) != 1:
        return None
    return matches[0]["DiskNumber"]


async def _resolve_windows_partition(
    session: SessionData, vmid: str, node: str, disk_number: int, partition_number: int, inner: list[str]
) -> OriginalLocationResult:
    script = (
        f"Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction Stop | "
        "Select-Object -ExpandProperty DriveLetter"
    )
    try:
        exitcode, out, _err = await run_guest_exec(
            session, "vm", vmid, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], node=node
        )
    except GuestExecTimeout:
        return _unavailable("Timed out asking the running guest for its drive letters.")
    letter = out.strip()
    if exitcode != 0 or not letter:
        return _unavailable(
            "This partition has no drive letter in the running guest (e.g. a Recovery or EFI partition)."
        )
    directory = f"{letter}:\\" + "\\".join(inner) if inner else f"{letter}:\\"
    return OriginalLocationResult(available=True, directory=directory)


async def _resolve_linux_partition(
    session: SessionData, vmid: str, node: str, disk_number: int, partition_number: int, inner: list[str]
) -> OriginalLocationResult:
    try:
        exitcode, out, _err = await run_guest_exec(session, "vm", vmid, ["lsblk", "-dn", "-o", "NAME"], node=node)
    except GuestExecTimeout:
        return _unavailable("Timed out asking the running guest for its disks.")
    disk_names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if exitcode != 0 or disk_number >= len(disk_names):
        return _unavailable("Could not match this item's disk to one in the running guest.")
    disk_name = disk_names[disk_number]

    try:
        exitcode, out, _err = await run_guest_exec(
            session, "vm", vmid, ["lsblk", "-rno", "NAME,MOUNTPOINT", f"/dev/{disk_name}"], node=node
        )
    except GuestExecTimeout:
        return _unavailable("Timed out asking the running guest for its partitions.")
    if exitcode != 0:
        return _unavailable("Could not list this disk's partitions in the running guest.")
    # lsblk's first row is the disk itself; the rest are its partitions,
    # in the same order the kernel enumerates them (matching partition
    # numbers 1..N).
    partition_rows = out.splitlines()[1:]
    if partition_number < 1 or partition_number > len(partition_rows):
        return _unavailable("Could not match this item's partition to one in the running guest.")
    columns = partition_rows[partition_number - 1].split(maxsplit=1)
    mountpoint = columns[1].strip() if len(columns) > 1 else ""
    if not mountpoint:
        return _unavailable("This partition isn't mounted in the running guest.")
    directory = mountpoint.rstrip("/") + "/" + "/".join(inner) if inner else mountpoint
    return OriginalLocationResult(available=True, directory=directory)


async def _resolve_lvm(
    session: SessionData, vmid: str, node: str, vg: str, lv: str, inner: list[str]
) -> OriginalLocationResult:
    """LVM paths carry their own stable, semantic key (the volume-group
    and logical-volume names PVE's own listing already gave us) rather
    than needing the disk-ordinal guess plain partitions require -
    findmnt resolves the live mountpoint directly from it."""
    for label in (vg, lv, *inner):
        try:
            check_path_safe(label)
        except UnsafePathError:
            return _unavailable(_GENERIC_UNAVAILABLE)

    for source in (f"/dev/{vg}/{lv}", f"/dev/mapper/{vg}-{lv}"):
        try:
            exitcode, out, _err = await run_guest_exec(
                session, "vm", vmid, ["findmnt", "-rno", "TARGET", "-S", source], node=node
            )
        except GuestExecTimeout:
            return _unavailable("Timed out asking the running guest for this volume's mount point.")
        lines = out.strip().splitlines()
        target = lines[0].strip() if lines else ""
        if exitcode == 0 and target:
            directory = target.rstrip("/") + "/" + "/".join(inner) if inner else target
            return OriginalLocationResult(available=True, directory=directory)
    return _unavailable("This volume isn't mounted in the running guest.")
