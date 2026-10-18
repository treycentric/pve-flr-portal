#!/usr/bin/env bash
# Installs (or updates) the `pve-flr-portal` host command on this PVE host -
# for a host that has an older portal install and doesn't have the command
# yet, or whose copy is out of date. Run as root on the PVE host:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/treycentric/pve-flr-portal/main/deploy/install-host-tools.sh)"
#
#   bash install-host-tools.sh [CTID]
#
# With a CTID the command is copied from that container's own checkout (the
# release it runs). Without one it is downloaded from GitHub: PFR_VERSION (a
# tag or a branch) if set, else the latest release, falling back to main if
# that release predates the command.
#
# Once the command is installed, `pve-flr-portal host-tools` keeps it (and the
# host-backup patch script, if installed) in step with a container's release,
# and `pve-flr-portal adopt` starts managing an existing install.
set -euo pipefail

SBIN="${PFR_SBIN:-/usr/local/sbin}"
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"
APP_DIR="/opt/pve-flr-portal"
CTID="${1:-}"

die() {
  echo "install-host-tools: $*" >&2
  exit 1
}

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# from_container CTID - copy the command out of the container's checkout.
# `pct pull` can report success and leave an empty file for a missing source,
# so check the file is there and comes back non-empty.
from_container() {
  command -v pct >/dev/null 2>&1 || die "pct not found - run this on the PVE host."
  pct exec "$1" -- test -f "$APP_DIR/deploy/host/pve-flr-portal" 2>/dev/null \
    || die "Container $1 runs a release that doesn't include the host command. Update it first (see the README), or run this without a CTID to download it."
  pct pull "$1" "$APP_DIR/deploy/host/pve-flr-portal" "$TMP" 2>/dev/null && [ -s "$TMP" ] \
    || die "Could not copy the host command out of container $1."
  echo "Copied from container $1's checkout."
}

# from_github - the latest release, or PFR_VERSION; main if the release lacks it.
from_github() {
  local slug tag ref
  case "$REPO_URL" in
    https://github.com/*) ;;
    *) die "REPO_URL must be a GitHub URL." ;;
  esac
  slug="${REPO_URL#https://github.com/}"
  slug="${slug%.git}"
  tag="${PFR_VERSION:-}"
  if [ -z "$tag" ]; then
    tag=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${slug}/releases/latest" 2>/dev/null || true)
    tag="${tag##*/}"
    case "$tag" in
      v[0-9]*) ;;
      *) tag="" ;;
    esac
  fi
  for ref in "$tag" main; do
    if [ -z "$ref" ]; then
      continue
    fi
    if curl -fsSL "https://raw.githubusercontent.com/${slug}/${ref}/deploy/host/pve-flr-portal" -o "$TMP" 2>/dev/null \
      && [ -s "$TMP" ]; then
      echo "Downloaded from $ref."
      return 0
    fi
  done
  die "Could not download the host command from GitHub (${slug}). Check the network, or pass the CTID of a container that has it."
}

if [ -n "$CTID" ]; then
  [[ "$CTID" =~ ^[0-9]+$ ]] || die "'$CTID' is not a container ID."
  from_container "$CTID"
else
  from_github
fi

# Never install something that isn't the command (an error page, a truncated
# download) - it would replace a working copy with nothing useful.
bash -n "$TMP" 2>/dev/null || die "The downloaded file isn't a valid script - nothing was installed."
grep -q '^TAG="pve-flr-portal"' "$TMP" || die "The downloaded file isn't the pve-flr-portal command - nothing was installed."

install -m 755 "$TMP" "$SBIN/pve-flr-portal.new"
mv -f "$SBIN/pve-flr-portal.new" "$SBIN/pve-flr-portal"
ln -sf pve-flr-portal "$SBIN/pve-flr-portal-config"
ln -sf pve-flr-portal "$SBIN/pve-flr-portal-update"

echo "Installed $SBIN/pve-flr-portal (and the -config / -update links)."
echo
echo "Next:"
echo "  pve-flr-portal list     # the containers it manages"
echo "  pve-flr-portal adopt    # start managing an existing install"
echo "  pve-flr-portal update   # update it (older installs are handled too)"
