#!/usr/bin/env bash
# Patches PVE's own PVE::Storage::PBSPlugin to stop dropping "host"-type
# backups (proxmox-backup-client --backup-type host) from storage content
# listings. Run this ON THE PVE HOST (not inside a container/VM) as root,
# once per node (a cluster needs this on every node), e.g.:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/treycentric/pve-flr-portal/main/deploy/patch-pve-host-backups.sh)"
#
# or, from a local clone:
#
#   bash deploy/patch-pve-host-backups.sh
#
# To remove the patch and the dpkg hook that reapplies it:
#
#   bash deploy/patch-pve-host-backups.sh --remove
#
# WHY THIS EXISTS (CLAUDE.md's "last resort" patching guidance, issue
# #155): PVE::Storage::PBSPlugin::list_volumes - the code behind both
# this app's and PVE's own "Backups" content listing - drops host
# backups via TWO separate lines, not one: a `$btype` check restricted
# to vm/ct, and (confirmed live 2026-10 - the first patch alone wasn't
# enough, host backups still didn't list even after it) a `$bid`
# numeric-only check right after it, which a host backup's hostname-
# shaped id always fails regardless of the first check. Neither check
# runs after any permission/ACL check - both run before it, so no
# amount of PVE role/ACL configuration can route around either one.
# There is no supported PVE API path around this (docs/api-reference.md)
# - the only fix is this patch, NOT officially supported by Proxmox,
# reverted by every pve-storage package update (the dpkg hook below
# reapplies it automatically after that happens).
#
# Two more lines (also confirmed live, once the two above let host
# entries through at all) set `vmid => int($bid)` and `subtype =>
# 'lxc'` unconditionally for every entry - wrong for a host backup
# (whose id is a hostname, not a vmid) and noisy: int() on a non-
# numeric string prints "Argument ... isn't numeric in int" to stderr
# on every single listing query. Patched too, same way, even though
# this app itself doesn't read either field (it parses guest type from
# the volid - see docs/api-reference.md).
#
# Safety: this only ever does EXACT, literal string replacements of the
# four lines above, applied atomically (if any target isn't found
# exactly once, NONE are changed - see the script body for the exact
# per-line resume/mismatch rules). If a future PVE version has
# reworded/refactored one of them, the match naturally fails and this
# refuses to touch the file at all rather than guessing at a fuzzy
# match - re-run this script (it'll tell you) once docs/api-reference.md
# has been updated for whatever changed.
set -euo pipefail

PBS_PLUGIN_PATH="${PBS_PLUGIN_PATH:-/usr/share/perl5/PVE/Storage/PBSPlugin.pm}"
INSTALLED_SCRIPT_PATH="/usr/local/sbin/pve-flr-portal-patch-host-backups.sh"
DPKG_HOOK_PATH="/etc/apt/apt.conf.d/85pve-flr-portal-host-backups"

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root (writes to $PBS_PLUGIN_PATH and /etc/apt/apt.conf.d)." >&2
  exit 1
fi

if [ ! -f "$PBS_PLUGIN_PATH" ]; then
  echo "No $PBS_PLUGIN_PATH found - is this a PVE host with pve-storage installed?" >&2
  exit 1
fi

_patch_py() {
  python3 - "$PBS_PLUGIN_PATH" "$1" <<'PYEOF'
import sys

path, mode = sys.argv[1], sys.argv[2]
pairs = [
    (
        "next if !($btype eq 'vm' || $btype eq 'ct');",
        "next if !($btype eq 'vm' || $btype eq 'ct' || $btype eq 'host');",
    ),
    (
        r"next if $bid !~ m/^\d+$/;",
        r"next if $btype ne 'host' && $bid !~ m/^\d+$/;",
    ),
    (
        # Confirmed live 2026-10: int() on a hostname-shaped $bid prints
        # "Argument ... isn't numeric in int" to stderr on every single
        # listing query once the two lines above let host entries
        # through - cosmetic (this app reads guest type from the volid,
        # never vmid/subtype), but worth silencing rather than spamming
        # the journal indefinitely.
        "vmid => int($bid),",
        "vmid => ($btype eq 'host' ? undef : int($bid)),",
    ),
    (
        "subtype => $btype eq 'vm' ? 'qemu' : 'lxc',",
        "subtype => $btype eq 'vm' ? 'qemu' : $btype eq 'ct' ? 'lxc' : undef,",
    ),
]
if mode == "remove":
    pairs = [(new, old) for old, new in pairs]

text = open(path, encoding="utf-8").read()

# Each line is judged independently - already-applied-for-this-line is
# not a mismatch, it's the normal case for resuming after an earlier,
# older version of this script only patched some of the lines (#155).
# A real mismatch (a line that's neither the expected search nor its
# already-applied replacement) still aborts the whole batch before
# writing anything - this pass is read-only until every line checks out.
to_apply = []
for search, replace in pairs:
    if replace in text and search not in text:
        continue  # already applied for this specific line
    count = text.count(search)
    if count != 1:
        print(f"MISMATCH: expected exactly 1 occurrence of {search!r}, found {count}", file=sys.stderr)
        sys.exit(1)
    to_apply.append((search, replace))

if not to_apply:
    print("NOOP")
    sys.exit(0)
for search, replace in to_apply:
    text = text.replace(search, replace, 1)
open(path, "w", encoding="utf-8").write(text)
print("CHANGED")
PYEOF
}

if [ "${1:-}" = "--remove" ]; then
  echo "==> Reverting $PBS_PLUGIN_PATH"
  result=$(_patch_py remove)
  rm -f "$DPKG_HOOK_PATH" "$INSTALLED_SCRIPT_PATH"
  if [ "$result" = "CHANGED" ]; then
    echo "==> Restarting pvedaemon/pveproxy to pick it up"
    systemctl restart pvedaemon pveproxy
    echo "Removed. Host backups will no longer appear in PVE's own listings or this app."
  else
    echo "Already not patched (or never was) - removed the dpkg hook/installed copy anyway."
  fi
  exit 0
fi

echo "==> Patching $PBS_PLUGIN_PATH"
if ! result=$(_patch_py apply); then
  echo "Refusing to patch - see the MISMATCH detail above. This almost certainly" >&2
  echo "means a PVE update reworded the target line; check docs/api-reference.md" >&2
  echo "for an update, or open an issue with your PVE version." >&2
  exit 1
fi

if [ "$result" = "NOOP" ]; then
  echo "Already patched."
else
  echo "==> Restarting pvedaemon/pveproxy to pick it up (brief PVE API/GUI blip)"
  systemctl restart pvedaemon pveproxy
fi

echo "==> Installing self + dpkg hook to reapply after every pve-storage update"
# $0 is a real file in the local-clone case (bash deploy/patch-...sh); in
# the curl-piped case (bash -c "$(curl ...)") it's just "bash", so the cp
# fails and this falls back to fetching the published copy instead.
if ! cp -- "$0" "$INSTALLED_SCRIPT_PATH" 2>/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/treycentric/pve-flr-portal/main/deploy/patch-pve-host-backups.sh \
    -o "$INSTALLED_SCRIPT_PATH"
fi
chmod 755 "$INSTALLED_SCRIPT_PATH"
cat > "$DPKG_HOOK_PATH" <<EOF
// Installed by pve-flr-portal (deploy/patch-pve-host-backups.sh, issue #155).
// Reapplies the host-backup-listing patch after every apt run - cheap/
// idempotent (the script no-ops if already patched), so this doesn't
// bother checking whether pve-storage specifically was touched.
DPkg::Post-Invoke { "$INSTALLED_SCRIPT_PATH >/dev/null 2>&1 || true"; };
EOF

echo "==> Done. Host backups should now appear in PVE's storage content listing."
echo "    To undo: bash deploy/patch-pve-host-backups.sh --remove"
