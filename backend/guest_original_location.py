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
(docs/plan.md §3's confirmed disk/part/lvm structure: a disk's own
listing is always `part/<N>/...` or `lvm/<vg>/<lv>/...`, never the
guest's real filesystem root directly). The only place that mapping
exists is the *live* running guest, so this needs the same guest-exec
channel guest_browse.py already uses - the same "agent must be running
and reachable" constraint every other PH.5 restore path already has.

Deliberately conservative throughout: any crumb structure this doesn't
recognize, or any live lookup that comes back ambiguous/empty/failed,
resolves to "unavailable" with a human reason - never a guess. A wrong
destination here is a wrong write into a live guest, not a cosmetic
mistake.

**Known open assumption, not yet live-verified:** for a plain (non-LVM)
partition, the disk this item's partition lives on is correlated to the
running guest's own disk numbering (Windows `Get-Partition -DiskNumber`,
Linux `lsblk`) purely by *ordinal position in PVE's own root
file-restore/list response* - the best available proxy for attachment
order, since nothing in the API exposes a real, guest-independent disk
identity (no raw partition-table bytes are reachable through this API -
see docs/plan.md §3). A guest with multiple disks on *mixed* bus types
(e.g. one scsi + one sata) is the scenario most likely to break this
assumption. The existing restore modal already shows the resolved
destination path prominently before the user confirms (and requires an
explicit "I understand this overwrites..." checkbox) - that's the real
safety net if this guess is ever wrong, not just the "disable when
ambiguous" fallback below, which only protects against an *empty* or
*failed* lookup, not a wrong-but-plausible one.
"""
from dataclasses import dataclass

import httpx

from .auth import SessionData
from .pve_client import GuestExecTimeout, UnsafePathError, check_path_safe, list_path, run_guest_exec


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
    in order. Root only ever lists disks for a VM, so crumbs[1] is
    always one; what follows is either `part`/<N>/... (a raw partition)
    or `lvm`/<vg>/<lv>/... (an assembled LVM volume)."""
    labels = [c.get("label", "") for c in crumbs[1:]]
    if len(labels) < 3:
        return _unavailable(_GENERIC_UNAVAILABLE)
    disk_label, kind, *rest = labels

    if kind == "part" and rest and rest[0].isdigit():
        partition_number = int(rest[0])
        inner = rest[1:]
        return await _resolve_partition(
            session, vmid, guest_os_family, node, volume, disk_label, partition_number, inner
        )
    if kind == "lvm" and len(rest) >= 2:
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
    disk_number = await _disk_ordinal(session, volume, disk_label)
    if disk_number is None:
        return _unavailable("Could not match this item's disk to one in the running guest.")

    if guest_os_family == "windows":
        return await _resolve_windows_partition(session, vmid, node, disk_number, partition_number, inner)
    return await _resolve_linux_partition(session, vmid, node, disk_number, partition_number, inner)


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
