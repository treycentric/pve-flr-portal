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
# STORAGE=local-zfs BRIDGE=vmbr1 VLAN_TAG=100 bash deploy/lxc-create.sh
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
VLAN_TAG="${VLAN_TAG:-}"        # optional, e.g. "100" - leave unset for no tag
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"
# Passed straight through to install.sh - see its own
# comment for what these do.
INSTALL_CERTBOT="${INSTALL_CERTBOT:-1}"
CERTBOT_DNS_PLUGIN="${CERTBOT_DNS_PLUGIN:-rfc2136}"
ENABLE_HOST_BACKUPS="${ENABLE_HOST_BACKUPS:-}"  # "true"/"false" to skip the prompt below

echo "==> pve-flr-portal LXC setup"
echo "    CTID=$CTID  HOSTNAME=$CT_HOSTNAME  STORAGE=$STORAGE  DISK=${DISK_GB}G  MEM=${MEMORY_MB}MB"

# Opt-in only, and only asked interactively - a non-interactive run (no
# ENABLE_HOST_BACKUPS override, no tty) defaults to "false" rather than
# hanging on `read` or silently patching a Proxmox-owned file. See
# deploy/patch-pve-host-backups.sh and docs/api-reference.md for the
# full detail behind this prompt's wording.
if [ -z "$ENABLE_HOST_BACKUPS" ]; then
  if [ -t 0 ]; then
    echo
    echo "This modifies the Proxmox Storage PBS backend to remove a"
    echo "limitation preventing the browsing and file-level restore of"
    echo "host backups, which is necessary for this application to"
    echo "support them. A benefit is that host backups will also become"
    echo "browsable in the PVE UI. It reapplies itself automatically"
    echo "after pve-storage package updates. See"
    echo "deploy/patch-pve-host-backups.sh for details."
    read -r -p "Enable host-backup support now? [y/N] " _reply || _reply=""
    case "$_reply" in
      [yY]*) ENABLE_HOST_BACKUPS=true ;;
      *) ENABLE_HOST_BACKUPS=false ;;
    esac
  else
    ENABLE_HOST_BACKUPS=false
  fi
fi

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

NET0="name=eth0,bridge=${BRIDGE},ip=${IP_CONFIG}"
if [ -n "$VLAN_TAG" ]; then
  NET0="${NET0},tag=${VLAN_TAG}"
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
  --net0 "$NET0" \
  --onboot 1 \
  --start 1

echo "==> Waiting for network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Installing pve-flr-portal inside container $CTID"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null"
# Full clone (not --depth 1) so the tag lookup below has the full tag
# history to search - depth-1 clones don't fetch tags at all. Then pin
# to the latest tagged GitHub Release (docs/dev/versioning.md) rather
# than leaving the checkout on whatever unreleased commit happens to be
# on main - issue #89. `deploy/update.sh` (also cloned in) is the
# supported way to move to a different/later release afterwards.
pct exec "$CTID" -- bash -c "git clone '${REPO_URL}' /opt/pve-flr-portal"
pct exec "$CTID" -- bash -c "
  cd /opt/pve-flr-portal
  TAG=\$(git tag -l 'v*.*.*' --sort=-v:refname | head -1)
  if [ -n \"\$TAG\" ]; then
    echo \"    Checking out latest release: \$TAG\"
    git checkout --quiet \"\$TAG\"
  else
    echo '    No release tags found yet - staying on main'
  fi
"
# install.sh already exists at this path from the clone above - no need
# to push a local copy over. That push used to assume "$0" (this
# script's own path) points at a real file on disk, which is only true
# when run as `bash deploy/lxc-create.sh` from a local checkout - not
# when curl-piped (`bash -c "$(curl ...)"`, the invocation this file's
# own header comment documents first), where "$0" is just "bash" and
# `dirname "$0"` resolves to "." Confirmed live 2026-09-01: a curl-piped
# run failed with "failed to open ./install.sh for reading".
pct exec "$CTID" -- env "INSTALL_CERTBOT=${INSTALL_CERTBOT}" "CERTBOT_DNS_PLUGIN=${CERTBOT_DNS_PLUGIN}" \
  bash /opt/pve-flr-portal/deploy/install.sh

if [ "$ENABLE_HOST_BACKUPS" = "true" ]; then
  echo "==> Enabling host-backup support on this PVE node"
  # Runs on the PVE HOST, not inside the container - this script patches
  # a file that only exists on the host. Same local-clone-vs-curl-piped
  # fallback install.sh above already has to handle.
  if [ -f "$(dirname "$0")/patch-pve-host-backups.sh" ]; then
    bash "$(dirname "$0")/patch-pve-host-backups.sh"
  else
    bash -c "$(curl -fsSL https://raw.githubusercontent.com/treycentric/pve-flr-portal/main/deploy/patch-pve-host-backups.sh)"
  fi
fi

CT_IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
echo "==> Done. pve-flr-portal is running in CT $CTID."
echo "    https://${CT_IP}:8008/"
echo "    Edit /opt/pve-flr-portal/.env inside the container for PVE_HOST/PVE_STORAGE,"
echo "    then: pct exec $CTID -- systemctl restart pve-flr-portal"
echo "    To update later: pct exec $CTID -- bash /opt/pve-flr-portal/deploy/update.sh"
if [ "$ENABLE_HOST_BACKUPS" = "true" ]; then
  echo "    Host-backup support patched on THIS node only - on a cluster,"
  echo "    also run deploy/patch-pve-host-backups.sh on every other node."
fi
