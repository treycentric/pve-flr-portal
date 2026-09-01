#!/usr/bin/env bash
# Creates an unprivileged Debian 12 LXC container on this PVE host for
# pve-flr-portal, then runs install.sh inside it. Run this ON THE PVE
# HOST (not inside a container/VM) as root, e.g.:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/treycentric/pve-flr-portal/main/deploy/lxc-create.sh)"
#
# or, from a local clone:
#
#   bash deploy/lxc-create.sh
#
# Override any of these via environment variables before running, e.g.
# STORAGE=local-zfs BRIDGE=vmbr1 bash deploy/lxc-create.sh
set -euo pipefail

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
# NOT just HOSTNAME - bash (and most shells) auto-populate that from the
# *running system's own* hostname, so "${HOSTNAME:-pve-flr-portal}" would
# silently pick up e.g. the PVE host's own name instead of the intended
# default the moment this runs anywhere HOSTNAME is already set (which is
# effectively always - it's not something a clean environment lacks).
# CT_HOSTNAME avoids the collision. Confirmed live 2026-09-01: an actual
# run named the container after the PVE host itself ("titan") instead of
# "pve-flr-portal".
CT_HOSTNAME="${CT_HOSTNAME:-pve-flr-portal}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
DISK_GB="${DISK_GB:-4}"
MEMORY_MB="${MEMORY_MB:-512}"
CORES="${CORES:-1}"
BRIDGE="${BRIDGE:-vmbr0}"
IP_CONFIG="${IP_CONFIG:-dhcp}"   # or e.g. "10.0.0.50/24,gw=10.0.0.1"
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"

echo "==> pve-flr-portal LXC setup"
echo "    CTID=$CTID  HOSTNAME=$CT_HOSTNAME  STORAGE=$STORAGE  DISK=${DISK_GB}G  MEM=${MEMORY_MB}MB"

# TEMPLATE can still be overridden explicitly (TEMPLATE=debian-12-standard_...
# bash deploy/lxc-create.sh) for a pinned/offline/reproducible run - but the
# default now discovers whatever the latest debian-12-standard build
# actually is, rather than a hardcoded version string that inevitably goes
# stale as Debian ships point releases (issue #16 - confirmed live
# 2026-09-01: a run failed outright with "no such template" against the
# previously pinned 12.7-1, which pveam's catalog had already moved past).
if [ -z "${TEMPLATE:-}" ]; then
  echo "==> Looking up the latest debian-12-standard template"
  pveam update
  TEMPLATE=$(pveam available --section system \
    | awk '{print $2}' \
    | grep '^debian-12-standard_' \
    | sort -t_ -k2 -V \
    | tail -1)
  if [ -z "$TEMPLATE" ]; then
    echo "Could not find any debian-12-standard template via 'pveam available' -" >&2
    echo "override TEMPLATE=<exact-name> explicitly and re-run." >&2
    exit 1
  fi
  echo "    Using $TEMPLATE"
fi

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  echo "==> Downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Creating container $CTID"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" \
  --unprivileged 1 \
  --features nesting=0 \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --swap 512 \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=${IP_CONFIG}" \
  --onboot 1 \
  --start 1

echo "==> Waiting for network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Installing pve-flr-portal inside container $CTID"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null"
pct exec "$CTID" -- bash -c "git clone --depth 1 '${REPO_URL}' /opt/pve-flr-portal"
pct push "$CTID" "$(dirname "$0")/install.sh" /opt/pve-flr-portal/deploy/install.sh --perms 0755
pct exec "$CTID" -- bash /opt/pve-flr-portal/deploy/install.sh

CT_IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
echo "==> Done. pve-flr-portal is running in CT $CTID."
echo "    https://${CT_IP}:8008/"
echo "    Edit /opt/pve-flr-portal/.env inside the container for PVE_HOST/PVE_STORAGE,"
echo "    then: pct exec $CTID -- systemctl restart pve-flr-portal"
