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
# Guided by default at a real terminal (whiptail dialogs if available,
# plain read prompts otherwise): a Welcome screen, a short Back/Next
# wizard for the essentials, then a review screen where any setting -
# including the Advanced ones - can be changed before anything is
# created. It then builds the container, installs the app, and writes
# the resolved values straight into the container's .env. Every setting
# can also be supplied up front via an environment variable, which skips
# its wizard question - so this still runs unattended if every value it
# would ask for is already set, or with no TTY attached (a piped/scripted
# invocation), in which case it silently uses defaults for anything not
# pre-set instead of hanging on a prompt nobody can answer.
#
# The settings registry and dialogs live in deploy/lib/settings.sh, shared
# with the management tool (pve-flr-portal config). From a checkout it is
# used as-is; when this script is curl-piped it is fetched from the latest
# release, so it matches the app that gets installed.
#
# Override any of these via environment variables before running, e.g.
# STORAGE=local-zfs BRIDGE=vmbr1 VLAN_TAG=100 bash deploy/lxc-create.sh
set -euo pipefail

# ---------------------------------------------------------------------
# Colour + status helpers
# ---------------------------------------------------------------------
if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""
fi
msg_info() { echo "${C_YELLOW}==>${C_RESET} $*"; }
msg_ok()   { echo "${C_GREEN}==>${C_RESET} $*"; }
msg_err()  { echo "${C_RED}==>${C_RESET} $*" >&2; }

cat <<BANNER
${C_BOLD}pve-flr-portal${C_RESET} - guided LXC install
File Level Restore Portal for Proxmox Backup Server
BANNER
echo

# ---------------------------------------------------------------------
# Installer-only inputs: environment variables, never prompted for.
# ---------------------------------------------------------------------
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"
INSTALL_CERTBOT="${INSTALL_CERTBOT:-1}"

# ---------------------------------------------------------------------
# The shared settings library (deploy/lib/settings.sh): from this checkout
# when run from one; otherwise from the latest release, falling back to main
# if that release predates the library. It must have the API this script
# was written for.
# ---------------------------------------------------------------------
PFR_LIB_API_WANTED=1

load_lib() {
  local self_dir lib tmp slug tag ref
  if [ -f "$0" ]; then
    self_dir="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$self_dir/lib/settings.sh" ]; then
      # shellcheck source=lib/settings.sh
      source "$self_dir/lib/settings.sh"
      return 0
    fi
  fi
  case "$REPO_URL" in
    https://github.com/*) ;;
    *) msg_err "REPO_URL must be a GitHub URL (or run this from a checkout) so the shared library can be fetched."; exit 1 ;;
  esac
  slug="${REPO_URL#https://github.com/}"
  slug="${slug%.git}"
  tag=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${slug}/releases/latest" 2>/dev/null || true)
  tag="${tag##*/}"
  case "$tag" in
    v[0-9]*) ;;
    *) tag="" ;;
  esac
  tmp=$(mktemp)
  for ref in "$tag" main; do
    if [ -z "$ref" ]; then
      continue
    fi
    if curl -fsSL "https://raw.githubusercontent.com/${slug}/${ref}/deploy/lib/settings.sh" -o "$tmp" 2>/dev/null \
      && grep -q "^PFR_LIB_API=${PFR_LIB_API_WANTED}\$" "$tmp"; then
      # shellcheck disable=SC1090
      source "$tmp"
      rm -f "$tmp"
      return 0
    fi
  done
  rm -f "$tmp"
  msg_err "Could not download the installer's shared library from GitHub (${slug}). Check the network, or run this from a checkout of the repository."
  exit 1
}
load_lib

pfr_detect_ui
pfr_init_install
pfr_run_install_ui
pfr_derive_install

# ---------------------------------------------------------------------
# Container creation, consuming the resolved variables above.
# ---------------------------------------------------------------------

# TEMPLATE can still be overridden explicitly (TEMPLATE=debian-12-standard_...
# bash deploy/lxc-create.sh) for a pinned/offline/reproducible run - but the
# default now discovers whatever the latest debian-12-standard build
# actually is, rather than a hardcoded version string that inevitably goes
# stale as Debian ships point releases (a run failed outright with "no such template" against the
# previously pinned 12.7-1, which pveam's catalog had already moved past).
if [ -z "${TEMPLATE:-}" ]; then
  msg_info "Looking up the latest debian-12-standard template"
  pveam update
  TEMPLATE=$(pveam available --section system \
    | awk '{print $2}' \
    | grep '^debian-12-standard_' \
    | sort -t_ -k2 -V \
    | tail -1)
  if [ -z "$TEMPLATE" ]; then
    msg_err "Could not find any debian-12-standard template via 'pveam available' - override TEMPLATE=<exact-name> explicitly and re-run."
    exit 1
  fi
  echo "    Using $TEMPLATE"
fi

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  msg_info "Downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

NET0="$(pfr_build_net0)"

# DNS only when given; otherwise the container uses the PVE host's settings.
DNS_ARGS=()
if [ -n "$DNS_SERVERS" ]; then
  DNS_ARGS+=(--nameserver "$(printf '%s' "$DNS_SERVERS" | tr -s ', ' ' ' | sed 's/^ //; s/ $//')")
fi
if [ -n "$SEARCH_DOMAIN" ]; then
  DNS_ARGS+=(--searchdomain "$SEARCH_DOMAIN")
fi

msg_info "Creating container $CTID"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" \
  --unprivileged 1 \
  --features nesting=0 \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --swap 512 \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --net0 "$NET0" \
  --tags "$PFR_CT_TAG" \
  "${DNS_ARGS[@]}" \
  --onboot 1 \
  --start 1
msg_ok "Created container $CTID"

# No ip6 setting leaves the kernel free to autoconfigure from router
# advertisements, so "Disabled" also turns IPv6 off inside the container.
if [ "$IPV6_MODE" = "disabled" ]; then
  pfr_apply_ipv6_sysctl "$CTID"
fi

msg_info "Waiting for network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

msg_info "Installing pve-flr-portal inside container $CTID"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null"
# Full clone (not --depth 1) so the tag lookup below has the full tag
# history to search - depth-1 clones don't fetch tags at all. Then pin
# to the latest tagged GitHub Release (docs/dev/versioning.md) rather
# than leaving the checkout on whatever unreleased commit happens to be
# on main. `deploy/update.sh` (also cloned in) is the
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
# `dirname "$0"` resolves to "." A curl-piped
# run failed with "failed to open ./install.sh for reading".
pct exec "$CTID" -- env "INSTALL_CERTBOT=${INSTALL_CERTBOT}" "CERTBOT_DNS_PLUGIN=${CERTBOT_DNS_PLUGIN}" \
  bash /opt/pve-flr-portal/deploy/install.sh
msg_ok "Installed pve-flr-portal"

# ---------------------------------------------------------------------
# Write the resolved app settings into the container's .env.
# ---------------------------------------------------------------------
pfr_resolve_dnt "$CTID"
pfr_collect_env_overrides
if [ "$ENABLE_DNT" = "yes" ]; then
  ENV_OVERRIDES+=("RESTORE_DATA_NICS=${RESTORE_DATA_NICS_JSON}")
fi
if [ "${#ENV_OVERRIDES[@]}" -gt 0 ]; then
  msg_info "Writing settings into the container's .env"
  pfr_write_env "$CTID"
  pct exec "$CTID" -- systemctl restart pve-flr-portal
  msg_ok "Wrote settings and restarted the service"
fi

# The host-side management command, taken from the container's own checkout
# (the release that was just installed). A release that predates it simply
# doesn't have the file.
HOST_CMD="/usr/local/sbin/pve-flr-portal"
HOST_CMD_INSTALLED=no
if pct pull "$CTID" "$PFR_APP_DIR/deploy/host/pve-flr-portal" "$HOST_CMD" 2>/dev/null; then
  chmod 755 "$HOST_CMD"
  ln -sf pve-flr-portal /usr/local/sbin/pve-flr-portal-config
  ln -sf pve-flr-portal /usr/local/sbin/pve-flr-portal-update
  HOST_CMD_INSTALLED=yes
  msg_ok "Installed the pve-flr-portal management command"
fi

if [ "$SETUP_LE" = "yes" ] && [ -n "$LE_DOMAINS" ]; then
  echo
  msg_info "Let's Encrypt requested for: $LE_DOMAINS"
  echo "    Place the ${CERTBOT_DNS_PLUGIN} plugin's credentials file inside the"
  echo "    container now (see deploy/${CERTBOT_DNS_PLUGIN}-credentials.ini.example),"
  echo "    e.g.: pct push $CTID <local-file> /etc/letsencrypt/${CERTBOT_DNS_PLUGIN}-credentials.ini"
  echo "          pct exec $CTID -- chmod 600 /etc/letsencrypt/${CERTBOT_DNS_PLUGIN}-credentials.ini"
  CERTBOT_ENV=("CERTBOT_DNS_PLUGIN=${CERTBOT_DNS_PLUGIN}")
  [ -n "$ACME_SERVER" ] && CERTBOT_ENV+=("ACME_SERVER=${ACME_SERVER}")
  if [ "$CERTBOT_KEY_TYPE" = "rsa" ]; then
    CERTBOT_ENV+=("CERTBOT_KEY_TYPE=rsa" "CERTBOT_RSA_KEY_SIZE=${CERTBOT_RSA_KEY_SIZE}")
  fi

  LATER_CMD="pct exec $CTID -- env"
  for kv in "${CERTBOT_ENV[@]}"; do
    LATER_CMD="$LATER_CMD \"$kv\""
  done
  LATER_CMD="$LATER_CMD bash /opt/pve-flr-portal/deploy/certbot-setup.sh $LE_DOMAINS"

  RUN_LE_NOW=no
  if [ "$IS_TTY" = "1" ] && ui_confirm "Credentials file in place - issue the certificate now?"; then
    RUN_LE_NOW=yes
  fi
  if [ "$RUN_LE_NOW" = "yes" ]; then
    # shellcheck disable=SC2086 # LE_DOMAINS is deliberately word-split into separate -d arguments
    pct exec "$CTID" -- env "${CERTBOT_ENV[@]}" \
      bash /opt/pve-flr-portal/deploy/certbot-setup.sh $LE_DOMAINS
    msg_ok "Certificate issued"
  else
    echo "    Run later with: $LATER_CMD"
  fi
fi

if [ "$ENABLE_HOST_BACKUPS" = "true" ]; then
  msg_info "Enabling host-backup support on this PVE node"
  # Runs on the PVE HOST, not inside the container - this script patches
  # a file that only exists on the host. The patch script is taken from the
  # container's own checkout, so it is exactly the release that was just
  # installed rather than whatever is on main today.
  PATCH_SCRIPT=$(mktemp)
  if pct pull "$CTID" /opt/pve-flr-portal/deploy/patch-pve-host-backups.sh "$PATCH_SCRIPT" 2>/dev/null; then
    bash "$PATCH_SCRIPT"
  elif [ -f "$(dirname "$0")/patch-pve-host-backups.sh" ]; then
    bash "$(dirname "$0")/patch-pve-host-backups.sh"
  else
    msg_err "Could not get the host-backup patch script from the container - run deploy/patch-pve-host-backups.sh from the repository on this node."
  fi
  rm -f "$PATCH_SCRIPT"
fi

CT_IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
msg_ok "Done. pve-flr-portal is running in CT $CTID."
echo "    https://${CT_IP}:${PORTAL_PORT}/"
if [ "$HOST_CMD_INSTALLED" = "yes" ]; then
  echo "    To change settings later: pve-flr-portal config"
  echo "    To update later:          pve-flr-portal update"
else
  echo "    To update later: pct exec $CTID -- bash /opt/pve-flr-portal/deploy/update.sh"
fi
if [ "$ENABLE_DNT" = "yes" ] && [ "$IP_CONFIG" = "dhcp" ]; then
  echo "    Direct Network Transfer is bound to the container's current DHCP address ($DNT_LOCAL_IP)."
  echo "    Give the container a DHCP reservation (or a static IP); if the address changes,"
  echo "    update RESTORE_DATA_NICS in /opt/pve-flr-portal/.env and restart the service."
fi
if [ "$ENABLE_HOST_BACKUPS" = "true" ]; then
  echo "    Host-backup support patched on THIS node only - on a cluster,"
  echo "    also run deploy/patch-pve-host-backups.sh on every other node."
fi
