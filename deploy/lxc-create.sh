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
# plain read prompts otherwise) - answer a handful of questions and it
# builds the container, installs the app, and writes the resolved
# values straight into the container's .env. Every question can also be
# answered up front via an environment variable (see below), which
# skips that question entirely - so this still runs unattended exactly
# as before if every value it would ask for is already set, or with no
# TTY attached (a piped/scripted invocation), in which case it silently
# falls back to defaults for anything not pre-set instead of hanging on
# a prompt nobody can answer.
#
# Override any of these via environment variables before running, e.g.
# STORAGE=local-zfs BRIDGE=vmbr1 bash deploy/lxc-create.sh
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
# Interactive-prompt helpers. Every one is env-var-first: if the
# caller already set the variable (e.g. STORAGE=local-zfs bash
# deploy/lxc-create.sh), that value is used as-is and the question is
# never asked - this is the escape hatch that keeps unattended/scripted
# use working. With no TTY attached and no whiptail, an unanswered
# question silently takes its default instead of blocking on `read`.
# ---------------------------------------------------------------------
IS_TTY=0
if [ -t 0 ] && [ -t 1 ]; then
  IS_TTY=1
fi

HAS_WHIPTAIL=0
if [ "$IS_TTY" = "1" ]; then
  if command -v whiptail >/dev/null 2>&1; then
    HAS_WHIPTAIL=1
  elif apt-get install -y -qq whiptail >/dev/null 2>&1 && command -v whiptail >/dev/null 2>&1; then
    HAS_WHIPTAIL=1
  fi
fi

# ask_value VAR "Prompt text" "default"
ask_value() {
  local var="$1" prompt="$2" default="$3" current
  current="${!var:-}"
  if [ -n "$current" ]; then
    return 0
  fi
  if [ "$IS_TTY" != "1" ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  local val
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    val=$(whiptail --backtitle "pve-flr-portal setup" --inputbox "$prompt" 10 70 "$default" 3>&1 1>&2 2>&3) \
      || { msg_err "Cancelled."; exit 1; }
  else
    read -rp "$prompt [$default]: " val
  fi
  printf -v "$var" '%s' "${val:-$default}"
}

# ask_yesno VAR "Prompt text" default(yes|no) -> sets VAR to "yes"/"no"
ask_yesno() {
  local var="$1" prompt="$2" default="$3" current
  current="${!var:-}"
  if [ -n "$current" ]; then
    return 0
  fi
  if [ "$IS_TTY" != "1" ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    local flag="--defaultno"
    [ "$default" = "yes" ] && flag=""
    if whiptail --backtitle "pve-flr-portal setup" $flag --yesno "$prompt" 10 70; then
      printf -v "$var" '%s' "yes"
    else
      printf -v "$var" '%s' "no"
    fi
  else
    local suffix="y/N"
    [ "$default" = "yes" ] && suffix="Y/n"
    local val
    read -rp "$prompt [$suffix]: " val
    val="${val:-$default}"
    case "$val" in
      y|Y|yes|Yes|YES) printf -v "$var" '%s' "yes" ;;
      *) printf -v "$var" '%s' "no" ;;
    esac
  fi
}

# ask_menu VAR "Prompt text" "default" "opt1" "opt2" ...
ask_menu() {
  local var="$1" prompt="$2" default="$3" current
  current="${!var:-}"
  shift 3
  local opts=("$@")
  if [ -n "$current" ] || [ "${#opts[@]}" -eq 0 ]; then
    if [ -z "$current" ]; then
      printf -v "$var" '%s' "$default"
    fi
    return 0
  fi
  if [ "$IS_TTY" != "1" ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    local items=()
    local o
    for o in "${opts[@]}"; do
      items+=("$o" "$o")
    done
    local val
    val=$(whiptail --backtitle "pve-flr-portal setup" --menu "$prompt" 18 70 8 "${items[@]}" 3>&1 1>&2 2>&3) \
      || { msg_err "Cancelled."; exit 1; }
    printf -v "$var" '%s' "$val"
  else
    echo "$prompt"
    local i=1 o
    for o in "${opts[@]}"; do
      echo "  $i) $o"
      i=$((i + 1))
    done
    local choice
    read -rp "Choice [1-${#opts[@]}, default: $default]: " choice
    if [ -n "$choice" ] && [ "$choice" -ge 1 ] 2>/dev/null && [ "$choice" -le "${#opts[@]}" ] 2>/dev/null; then
      printf -v "$var" '%s' "${opts[$((choice - 1))]}"
    else
      printf -v "$var" '%s' "$default"
    fi
  fi
}

# ---------------------------------------------------------------------
# Basic settings - asked up front regardless of default/advanced mode,
# since CTID/hostname/network/storage are unavoidable per-user choices,
# not tunables with a one-size-fits-all default.
# ---------------------------------------------------------------------
DEFAULT_CTID="$(pvesh get /cluster/nextid 2>/dev/null || echo 100)"
ask_value CTID "Container ID (CTID)" "$DEFAULT_CTID"

# NOT just HOSTNAME - bash (and most shells) auto-populate that from the
# *running system's own* hostname, so "${HOSTNAME:-pve-flr-portal}" would
# silently pick up e.g. the PVE host's own name instead of the intended
# default the moment this runs anywhere HOSTNAME is already set (which is
# effectively always - it's not something a clean environment lacks).
# CT_HOSTNAME avoids the collision. Confirmed live 2026-09-01: an actual
# run named the container after the PVE host itself ("titan") instead of
# "pve-flr-portal".
ask_value CT_HOSTNAME "Container hostname" "pve-flr-portal"

STORAGE_CHOICES=()
if command -v pvesm >/dev/null 2>&1; then
  while IFS= read -r line; do
    STORAGE_CHOICES+=("$line")
  done < <(pvesm status 2>/dev/null | awk 'NR>1{print $1}')
fi
ask_menu STORAGE "Container storage (where the LXC disk lives)" "local-lvm" "${STORAGE_CHOICES[@]}"
ask_menu PVE_STORAGE_CHOICE "PBS storage to browse backups from (PVE_STORAGE in .env)" "pbs" "${STORAGE_CHOICES[@]}"
PVE_STORAGE="${PVE_STORAGE:-$PVE_STORAGE_CHOICE}"

BRIDGE_CHOICES=()
while IFS= read -r line; do
  BRIDGE_CHOICES+=("$line")
done < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}')
ask_menu BRIDGE "Network bridge" "vmbr0" "${BRIDGE_CHOICES[@]}"

ask_yesno USE_STATIC_IP "Use a static IP instead of DHCP?" "no"
if [ "$USE_STATIC_IP" = "yes" ] && [ -z "${IP_CONFIG:-}" ]; then
  ask_value STATIC_CIDR "Static IP + prefix (e.g. 10.0.0.50/24)" "10.0.0.50/24"
  ask_value STATIC_GW "Gateway" "10.0.0.1"
  IP_CONFIG="${STATIC_CIDR},gw=${STATIC_GW}"
fi
IP_CONFIG="${IP_CONFIG:-dhcp}"

TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"

# ---------------------------------------------------------------------
# Advanced settings - off by default. Resource sizing already has
# sensible defaults for this app (a small stateless Python process);
# Direct Network Transfer, its TLS policy, and Let's Encrypt are all
# specific enough to a given deployment that most installs don't need
# to touch them on day one.
# ---------------------------------------------------------------------
ask_yesno USE_ADVANCED "Configure advanced options (resource sizing, Direct Network Transfer, TLS policy, Let's Encrypt)?" "no"

DISK_GB="${DISK_GB:-4}"
MEMORY_MB="${MEMORY_MB:-512}"
CORES="${CORES:-1}"
PVE_HOST="${PVE_HOST:-}"
ENABLE_DNT="${ENABLE_DNT:-no}"
RESTORE_DATA_NICS_JSON="${RESTORE_DATA_NICS_JSON:-}"
TLS_PREFERRED="${TLS_PREFERRED:-verify}"
TLS_MINIMUM="${TLS_MINIMUM:-insecure}"
SETUP_LE="${SETUP_LE:-no}"
LE_DOMAINS="${LE_DOMAINS:-}"
ACME_SERVER="${ACME_SERVER:-}"
CERTBOT_KEY_TYPE="${CERTBOT_KEY_TYPE:-}"
CERTBOT_RSA_KEY_SIZE="${CERTBOT_RSA_KEY_SIZE:-}"

if [ "$USE_ADVANCED" = "yes" ]; then
  ask_value DISK_GB "Disk size (GB)" "$DISK_GB"
  ask_value MEMORY_MB "Memory (MB)" "$MEMORY_MB"
  ask_value CORES "CPU cores" "$CORES"

  DEFAULT_PVE_HOST="$(hostname -f 2>/dev/null || hostname)"
  ask_value PVE_HOST "PVE host (hostname/IP the portal uses to reach the PVE API)" "$DEFAULT_PVE_HOST"

  ask_yesno ENABLE_DNT "Enable Direct Network Transfer (faster guest restore over a dedicated NIC)?" "no"
  if [ "$ENABLE_DNT" = "yes" ]; then
    ask_value DNT_CIDR "Data NIC subnet (CIDR the restore target guest lives on)" "10.0.5.0/24"
    ask_value DNT_LOCAL_IP "This host's address on that subnet (RESTORE_DATA_NICS local_ip)" ""
    if [ -n "$DNT_LOCAL_IP" ]; then
      RESTORE_DATA_NICS_JSON="[{\"cidr\":\"${DNT_CIDR}\",\"local_ip\":\"${DNT_LOCAL_IP}\"}]"
    fi
    ask_menu TLS_PREFERRED "Data-plane TLS - preferred mode" "verify" "verify" "insecure" "plaintext"
    ask_menu TLS_MINIMUM "Data-plane TLS - minimum acceptable mode" "insecure" "verify" "insecure" "plaintext"
  fi

  ask_yesno SETUP_LE "Set up a certificate now via ACME/DNS-01 (Let's Encrypt or an internal ACME server) - needs a DNS plugin's credentials file already in place?" "no"
  if [ "$SETUP_LE" = "yes" ]; then
    ask_value LE_DOMAINS "Domain(s) for the certificate (space-separated; include the data-plane hostname too if using it)" ""
    ask_value CERTBOT_DNS_PLUGIN "certbot DNS plugin" "${CERTBOT_DNS_PLUGIN:-rfc2136}"
    ask_yesno USE_INTERNAL_ACME "Use an internal ACME server instead of Let's Encrypt (e.g. acme2certifier)?" "no"
    if [ "$USE_INTERNAL_ACME" = "yes" ]; then
      ask_value ACME_SERVER "Internal ACME server directory URL" ""
      ask_menu CERTBOT_KEY_TYPE "Key type (leave default unless your CA doesn't support it)" "ecdsa" "ecdsa" "rsa"
      if [ "$CERTBOT_KEY_TYPE" = "rsa" ]; then
        ask_value CERTBOT_RSA_KEY_SIZE "RSA key size" "2048"
      fi
    fi
  fi
fi

INSTALL_CERTBOT="${INSTALL_CERTBOT:-1}"
CERTBOT_DNS_PLUGIN="${CERTBOT_DNS_PLUGIN:-rfc2136}"

echo
msg_info "Settings:"
echo "    CTID=$CTID  HOSTNAME=$CT_HOSTNAME"
echo "    Container storage=$STORAGE  Disk=${DISK_GB}G  Mem=${MEMORY_MB}MB  Cores=$CORES"
echo "    Bridge=$BRIDGE  IP=$IP_CONFIG"
echo "    PVE_STORAGE=$PVE_STORAGE  PVE_HOST=${PVE_HOST:-<default: this host>}"
[ "$ENABLE_DNT" = "yes" ] && echo "    Direct Network Transfer: $RESTORE_DATA_NICS_JSON (TLS $TLS_MINIMUM..$TLS_PREFERRED)"
ACME_SERVER_LABEL="$ACME_SERVER"
if [ -z "$ACME_SERVER_LABEL" ]; then
  ACME_SERVER_LABEL="Let's Encrypt"
fi
[ "$SETUP_LE" = "yes" ] && echo "    Certificate: $LE_DOMAINS (plugin: $CERTBOT_DNS_PLUGIN, ACME server: $ACME_SERVER_LABEL)"
echo
ask_yesno CONFIRM_PROCEED "Proceed with these settings?" "yes"
if [ "$CONFIRM_PROCEED" != "yes" ]; then
  msg_err "Aborted."
  exit 1
fi

# ---------------------------------------------------------------------
# Container creation (unchanged from the non-interactive version below
# this point, just consuming the resolved variables above)
# ---------------------------------------------------------------------

# TEMPLATE can still be overridden explicitly (TEMPLATE=debian-12-standard_...
# bash deploy/lxc-create.sh) for a pinned/offline/reproducible run - but the
# default now discovers whatever the latest debian-12-standard build
# actually is, rather than a hardcoded version string that inevitably goes
# stale as Debian ships point releases (issue #16 - confirmed live
# 2026-09-01: a run failed outright with "no such template" against the
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

msg_info "Creating container $CTID"
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
msg_ok "Created container $CTID"

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
msg_ok "Installed pve-flr-portal"

# ---------------------------------------------------------------------
# Write the resolved app settings into the container's .env - pushed as
# a KEY=VALUE file and patched in with a small Python script (already a
# guaranteed-present dependency) rather than sed, so JSON values like
# RESTORE_DATA_NICS don't need shell/sed delimiter escaping.
# ---------------------------------------------------------------------
ENV_OVERRIDES=()
[ -n "${PVE_HOST:-}" ] && ENV_OVERRIDES+=("PVE_HOST=${PVE_HOST}")
ENV_OVERRIDES+=("PVE_STORAGE=${PVE_STORAGE}")
if [ "$ENABLE_DNT" = "yes" ] && [ -n "$RESTORE_DATA_NICS_JSON" ]; then
  ENV_OVERRIDES+=("RESTORE_DATA_NICS=${RESTORE_DATA_NICS_JSON}")
  ENV_OVERRIDES+=("RESTORE_DATA_NIC_TLS_PREFERRED=${TLS_PREFERRED}")
  ENV_OVERRIDES+=("RESTORE_DATA_NIC_TLS_MINIMUM=${TLS_MINIMUM}")
fi

if [ "${#ENV_OVERRIDES[@]}" -gt 0 ]; then
  msg_info "Writing settings into the container's .env"
  OVERRIDES_TMP=$(mktemp)
  printf '%s\n' "${ENV_OVERRIDES[@]}" > "$OVERRIDES_TMP"
  pct push "$CTID" "$OVERRIDES_TMP" /tmp/pfr-overrides.env
  rm -f "$OVERRIDES_TMP"
  pct exec "$CTID" -- python3 -c '
import re
overrides = {}
with open("/tmp/pfr-overrides.env") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        overrides[k] = v
path = "/opt/pve-flr-portal/.env"
with open(path) as f:
    lines = f.readlines()
out = []
for line in lines:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
    if m and m.group(1) in overrides:
        out.append(f"{m.group(1)}={overrides[m.group(1)]}\n")
    else:
        out.append(line)
with open(path, "w") as f:
    f.writelines(out)
'
  pct exec "$CTID" -- rm -f /tmp/pfr-overrides.env
  pct exec "$CTID" -- systemctl restart pve-flr-portal
  msg_ok "Wrote settings and restarted the service"
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
  [ -n "$CERTBOT_KEY_TYPE" ] && CERTBOT_ENV+=("CERTBOT_KEY_TYPE=${CERTBOT_KEY_TYPE}")
  [ -n "$CERTBOT_RSA_KEY_SIZE" ] && CERTBOT_ENV+=("CERTBOT_RSA_KEY_SIZE=${CERTBOT_RSA_KEY_SIZE}")

  LATER_CMD="pct exec $CTID -- env"
  for kv in "${CERTBOT_ENV[@]}"; do
    LATER_CMD="$LATER_CMD \"$kv\""
  done
  LATER_CMD="$LATER_CMD bash /opt/pve-flr-portal/deploy/certbot-setup.sh $LE_DOMAINS"

  ask_yesno RUN_LE_NOW "Credentials file in place - issue the certificate now?" "no"
  if [ "$RUN_LE_NOW" = "yes" ]; then
    # shellcheck disable=SC2086 # LE_DOMAINS is deliberately word-split into separate -d arguments
    pct exec "$CTID" -- env "${CERTBOT_ENV[@]}" \
      bash /opt/pve-flr-portal/deploy/certbot-setup.sh $LE_DOMAINS
    msg_ok "Certificate issued"
  else
    echo "    Run later with: $LATER_CMD"
  fi
fi

CT_IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
msg_ok "Done. pve-flr-portal is running in CT $CTID."
echo "    https://${CT_IP}:8008/"
if [ "${#ENV_OVERRIDES[@]}" -eq 0 ]; then
  echo "    Edit /opt/pve-flr-portal/.env inside the container for PVE_HOST/PVE_STORAGE,"
  echo "    then: pct exec $CTID -- systemctl restart pve-flr-portal"
fi
echo "    To update later: pct exec $CTID -- bash /opt/pve-flr-portal/deploy/update.sh"
