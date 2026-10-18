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
# Guided setup: a short wizard (Back/Next) for the essentials, then a
# review screen where anything - including the Advanced settings - can be
# changed before anything is created.
#
# Every setting is env-var-first: a variable already set when the script
# starts is used as-is and its wizard question is skipped (it can still be
# changed from the review screen). With no TTY attached the whole UI is
# skipped and defaults are used, so scripted/piped runs behave unattended.
# Without whiptail the same flow runs as plain read prompts ('<' = back).
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

TITLE="pve-flr-portal setup"
NAV_CANCEL="Back"

wt() { whiptail --backtitle "$TITLE" "$@" 3>&1 1>&2 2>&3; }


term_rows() { tput lines 2>/dev/null || echo 24; }
term_cols() { tput cols 2>/dev/null || echo 80; }

# dlg_w -> a dialog width that uses wide terminals but never less than 72.
dlg_w() {
  local c=$(($(term_cols) - 4))
  if [ "$c" -gt 100 ]; then
    c=100
  fi
  if [ "$c" -lt 72 ]; then
    c=72
  fi
  echo "$c"
}

# box_h "text" extra -> a dialog height that fits the wrapped text plus
# `extra` rows of controls, capped to the terminal.
box_h() {
  local lines h rows
  lines=$(printf '%b' "$1" | fold -s -w 68 | wc -l)
  h=$((lines + $2))
  rows=$(($(term_rows) - 2))
  if [ "$h" -gt "$rows" ]; then
    h="$rows"
  fi
  echo "$h"
}

# cap_h HEIGHT -> HEIGHT, limited to the terminal.
cap_h() {
  local rows=$(($(term_rows) - 2))
  if [ "$1" -gt "$rows" ]; then
    echo "$rows"
  else
    echo "$1"
  fi
}

ui_msg() {
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    wt --title "$TITLE" --msgbox "$1" "$(box_h "$1" 7)" 72 || true
  else
    printf '%b\n' "$1"
  fi
}

ui_confirm() {
  local reply
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    wt --title "$TITLE" --defaultno --yesno "$1" "$(box_h "$1" 7)" 72
  else
    read -rp "$1 [y/N]: " reply || reply=""
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  fi
}

# ---------------------------------------------------------------------
# Settings registry. One row per setting:
#   def KEY "Label" type default ENV_NAME group [opts] [cond] [help]
# type: int (opts "min:max"), str (opts = validation regex), enum (opts
#   "value=description|value=description"). ENV_NAME is the key written to
#   the container's .env (blank = installer-only). cond "OTHER=value" hides
#   the setting unless OTHER has that value.
# ---------------------------------------------------------------------
declare -A S_LABEL S_TYPE S_DEF S_ENV S_GROUP S_OPTS S_COND S_HELP PRESET
ALL_KEYS=()
def() {
  S_LABEL[$1]="$2"; S_TYPE[$1]="$3"; S_DEF[$1]="$4"; S_ENV[$1]="$5"; S_GROUP[$1]="$6"
  S_OPTS[$1]="${7:-}"; S_COND[$1]="${8:-}"; S_HELP[$1]="${9:-}"
  ALL_KEYS+=("$1")
}

DEFAULT_CTID="$(pvesh get /cluster/nextid 2>/dev/null || echo 100)"
DEFAULT_PVE_HOST="$(hostname -f 2>/dev/null || hostname)"

# --- Basic (the wizard) ---
# NOT just HOSTNAME - bash auto-populates that from the running system's own
# hostname, so it would silently pick up the PVE host's name. CT_HOSTNAME
# avoids the collision.
def CTID "Container ID" int "$DEFAULT_CTID" "" basic "100:999999999" "" "The Proxmox ID for the new container."
def CT_HOSTNAME "Container hostname" str pve-flr-portal "" basic '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$' "" "Letters, digits and hyphens."
def STORAGE "Container storage" str local-lvm "" basic "" "" "Choose the storage that will hold the virtual disk (root filesystem) of this application's container. Only storages that can hold container disks are listed."
def BRIDGE "Network bridge" str vmbr0 "" basic "" "" "Choose the network bridge the container's network interface will attach to. This decides which network the portal is reachable on."
def VLAN_TAG "VLAN tag" str "" "" basic '^([0-9]{1,4})?$' "" "Leave blank for no VLAN tag."
def USE_STATIC_IP "Static IP instead of DHCP" enum no "" basic "no=DHCP|yes=Static address" "" "Use a fixed address instead of DHCP?"
def STATIC_CIDR "Static IP + prefix" str 10.0.0.50/24 "" basic '^[0-9.]+/[0-9]+$' "USE_STATIC_IP=yes" "For example 10.0.0.50/24."
def STATIC_GW "Gateway" str 10.0.0.1 "" basic '^[0-9.]+$' "USE_STATIC_IP=yes" ""
def PVE_HOST "PVE host" str "$DEFAULT_PVE_HOST" PVE_HOST basic '^[^[:space:]]+$' "" "The hostname or IP the portal uses to reach the Proxmox API (usually this host)."
def PVE_STORAGE "PBS storage(s) to browse" str pbs PVE_STORAGE basic "" "" "Select the Proxmox Backup Server storage(s) the portal will browse backups from. Choose more than one if your backups are spread across several PBS storages or namespaces."
def ENABLE_HOST_BACKUPS "Host-backup support" enum false "" basic "false=No|true=Yes - patch this PVE node now" "" "Lets the portal browse and restore from HOST-type backups.\n\nThis modifies the Proxmox Storage PBS backend on this PVE host to remove a limitation that prevents browsing and file-level restore of host backups. A side benefit: host backups also become browsable in the PVE UI.\n\nThe change reapplies itself automatically after pve-storage package updates. See deploy/patch-pve-host-backups.sh for details. On a cluster, run that script on every other node too."

# --- Advanced ---
def DISK_GB "Disk size (GB)" int 4 "" resources "1:65536" "" ""
def MEMORY_MB "Memory (MB)" int 512 "" resources "128:1048576" "" ""
def CORES "CPU cores" int 1 "" resources "1:256" "" ""

def PORTAL_PORT "HTTPS port" int 8008 PORT portal "1:65535" "" "The port the portal's web UI listens on."
def PVE_VERIFY_SSL "Verify the PVE API certificate" str false PVE_VERIFY_SSL portal '^(false|true|/.+)$' "" "Whether the portal checks the certificate of the Proxmox API it talks to. Choose Yes only if Proxmox serves a certificate issued by a CA the container trusts."
def SESSION_IDLE_TIMEOUT_MINUTES "Idle timeout (minutes)" int 30 SESSION_IDLE_TIMEOUT_MINUTES portal "1:100000" "" "Force logout after this many minutes of no activity."
def DEFAULT_THEME "Default colour theme" enum auto DEFAULT_THEME portal "auto=Follow the browser/OS|light=Light|dark=Dark|proxmox-dark=Proxmox dark" "" ""

def RESTRICT_JOBS_TO_OWN "Restrict restore jobs to their owner" enum false RESTRICT_JOBS_TO_OWN access "false=No - everyone sees one shared job list|true=Yes - users see only their own jobs" "" ""
def JOB_ADMIN_PRIVILEGE "Privilege that bypasses the restriction" str Sys.Audit JOB_ADMIN_PRIVILEGE access '^[A-Za-z.]+$' "RESTRICT_JOBS_TO_OWN=true" "A PVE privilege granted at the root path '/'."
def JOB_HISTORY_RETENTION_DAYS "Job history retention (days)" int 7 JOB_HISTORY_RETENTION_DAYS access "1:36500" "" "How long a finished restore job's history and log are kept."

def FILE_RESTORE_LIST_MAX_CONCURRENCY "Concurrent file-restore listings" int 2 FILE_RESTORE_LIST_MAX_CONCURRENCY limits "1:64" "" "Max file-restore/list calls in flight to PVE at once. Each cold call boots a helper VM, so keep this low."
def GUEST_AGENT_MIN_COMMAND_GAP_SECONDS "Guest-agent command gap (seconds)" int 0 GUEST_AGENT_MIN_COMMAND_GAP_SECONDS limits "0:3600" "" "Minimum pause between this app's own commands on one guest. 0 disables it."
def RESTORE_LONG_RUNNING_EXEC_TIMEOUT_SECONDS "Long-running command timeout (s)" int 1800 RESTORE_LONG_RUNNING_EXEC_TIMEOUT_SECONDS limits "30:864000" "" "Poll budget for guest commands whose duration scales with file size."

def ENABLE_DNT "Direct Network Transfer" enum yes "" basic "yes=Yes - guests fetch files over the network (fast)|no=No - transfer through the guest agent (slower)" "" "Direct Network Transfer lets a guest fetch the restored file straight from this portal over the network, instead of every byte being relayed through the Proxmox guest agent one small chunk at a time. For large files it is dramatically faster.\n\nIt uses this container's own address and subnet, detected automatically once the container is up, so it works for guests on the same network. Guests on another subnet fall back to the slower path (more networks can be added later in .env).\n\nIf the container uses DHCP, give it a DHCP reservation (or choose a static IP) so its address doesn't change.\n\nChoose No to always use the guest agent."
def DNT_CIDR "Guest subnet (CIDR)" str "" "" dnt '^([0-9.]+/[0-9]+)?$' "ENABLE_DNT=yes" "The subnet the restore target guests live on, e.g. 10.0.5.0/24. Leave blank to use the container's own subnet (detected automatically)."
def DNT_LOCAL_IP "This host's address on that subnet" str "" "" dnt '^([0-9.]+)?$' "ENABLE_DNT=yes" "The address guests connect to: the portal's own address on that subnet, not the guest's. Leave blank to use the container's own address (detected automatically)."
def DNT_HOSTNAME "Data-plane hostname (optional)" str "" "" dnt '^([A-Za-z0-9.-]+)?$' "ENABLE_DNT=yes" "A DNS name for that segment, used in the download URL and certificate instead of the bare IP. Blank to use the IP."
def RESTORE_DATA_NIC_PORT "Data-plane port (0 = auto)" int 0 RESTORE_DATA_NIC_PORT dnt "0:65535" "ENABLE_DNT=yes" "0 resolves to the HTTPS port + 1."
def RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS "Download link lifetime (s)" int 120 RESTORE_DOWNLOAD_TOKEN_TTL_SECONDS dnt "5:86400" "ENABLE_DNT=yes" "How long a single-use download link stays valid."
def TLS_PREFERRED "TLS - preferred mode" enum verify RESTORE_DATA_NIC_TLS_PREFERRED dnt "verify=Verify - HTTPS, the guest validates the certificate|insecure=Insecure - HTTPS, encryption only|plaintext=Plaintext - HTTP" "ENABLE_DNT=yes" "The strongest mode to attempt."
def TLS_MINIMUM "TLS - minimum acceptable mode" enum insecure RESTORE_DATA_NIC_TLS_MINIMUM dnt "verify=Verify - HTTPS, the guest validates the certificate|insecure=Insecure - HTTPS, encryption only|plaintext=Plaintext - HTTP" "ENABLE_DNT=yes" "The floor. Must not be stricter than the preferred mode."
def TLS_ON_UNMET "If no mode qualifies" enum fallback RESTORE_DATA_NIC_TLS_ON_UNMET dnt "fallback=Fall back - chunked write through the guest agent|fail=Fail the restore" "ENABLE_DNT=yes" ""
def TLS_MIN_VERSION "Minimum TLS version" enum 1.2 RESTORE_DATA_NIC_TLS_MIN_VERSION dnt "1.2=TLS 1.2|1.3=TLS 1.3" "ENABLE_DNT=yes" ""
def TLS_INSTALL_CA "Install the data-plane CA into guests" enum never RESTORE_DATA_NIC_TLS_INSTALL_CA dnt "never=Never - the guest must already trust it|if-missing=Only when it is missing|always=On every restore" "ENABLE_DNT=yes" "Needs the same VM.GuestAgent.Unrestricted grant Direct Network Transfer already requires."

def SETUP_LE "Issue a certificate (ACME / DNS-01)" enum no "" cert "no=No - keep the self-signed certificate|yes=Yes - Let's Encrypt or an internal ACME server" "" "Needs a DNS plugin's credentials file in place before the certificate can actually be issued."
def LE_DOMAINS "Domain(s)" str "" "" cert '^[^[:space:]].*$' "SETUP_LE=yes" "Space-separated. Include the data-plane hostname too if you use Direct Network Transfer."
def CERTBOT_DNS_PLUGIN "certbot DNS plugin" str rfc2136 "" cert '^[a-z0-9-]+$' "SETUP_LE=yes" ""
def ACME_SERVER "ACME server URL" str "" "" cert '^(https://.+)?$' "SETUP_LE=yes" "Blank = Let's Encrypt. Set it to use an internal ACME server's directory URL instead."
def CERTBOT_KEY_TYPE "Key type" enum ecdsa "" cert "ecdsa=ECDSA - certbot's default|rsa=RSA" "SETUP_LE=yes" "Change only if your CA doesn't support ECDSA."
def CERTBOT_RSA_KEY_SIZE "RSA key size" int 2048 "" cert "2048:16384" "CERTBOT_KEY_TYPE=rsa" ""
def PFR_ACME_DATA_PLANE "Also use it for the data plane" enum 0 PFR_ACME_DATA_PLANE cert "0=No|1=Yes - the domains must include the data-plane hostname" "ENABLE_DNT=yes" ""

BASIC_KEYS=(CTID CT_HOSTNAME STORAGE BRIDGE VLAN_TAG IP PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)
# The review screen groups them under headings ("H:" rows are headings).
BASIC_LAYOUT=("H:Container" CTID CT_HOSTNAME STORAGE "H:Network" BRIDGE VLAN_TAG IP "H:Portal" PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)
ADV_GROUPS=(resources portal access limits dnt cert)
declare -A G_TITLE=(
  [resources]="Container resources"
  [portal]="Portal"
  [access]="Restore-job visibility"
  [limits]="Performance and limits"
  [dnt]="Direct Network Transfer"
  [cert]="Certificate (ACME / DNS-01)"
)

# Installer-only inputs that were never prompts: env-only, as before.
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
REPO_URL="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"
INSTALL_CERTBOT="${INSTALL_CERTBOT:-1}"

# Record what the caller pre-set (those questions are skipped), then fill
# every other setting with its default.
for _k in "${ALL_KEYS[@]}"; do
  if [ -n "${!_k:-}" ]; then
    PRESET[$_k]=1
  else
    printf -v "$_k" '%s' "${S_DEF[$_k]}"
  fi
done
if [ -n "${IP_CONFIG:-}" ]; then
  PRESET[USE_STATIC_IP]=1; PRESET[STATIC_CIDR]=1; PRESET[STATIC_GW]=1
fi
if [ -n "${RESTORE_DATA_NICS_JSON:-}" ]; then
  if [ -z "${PRESET[ENABLE_DNT]:-}" ]; then
    ENABLE_DNT=yes
    PRESET[ENABLE_DNT]=1
  fi
  PRESET[DNT_CIDR]=1; PRESET[DNT_LOCAL_IP]=1; PRESET[DNT_HOSTNAME]=1
fi

cond_ok() {
  local c="${S_COND[$1]}" ref
  if [ -z "$c" ]; then
    return 0
  fi
  ref="${c%%=*}"
  [ "${!ref}" = "${c#*=}" ]
}

ip_display() {
  if [ -n "${IP_CONFIG:-}" ]; then
    echo "$IP_CONFIG"
  elif [ "$USE_STATIC_IP" = "yes" ]; then
    echo "$STATIC_CIDR (gateway $STATIC_GW)"
  else
    echo "DHCP"
  fi
}

label_of() {
  if [ "$1" = "IP" ]; then
    echo "IP address"
  else
    echo "${S_LABEL[$1]}"
  fi
}

# show_val KEY -> the value as a person would read it (Yes/No, a label).
show_val() {
  local k="$1" v="${!1}" opts o d
  if [ "$k" = "PVE_VERIFY_SSL" ]; then
    case "$v" in
      false) echo "No" ;;
      true) echo "Yes" ;;
      *) echo "Yes (CA: $v)" ;;
    esac
    return
  fi
  if [ "${S_TYPE[$k]:-}" = "enum" ]; then
    IFS='|' read -ra opts <<< "${S_OPTS[$k]}"
    for o in "${opts[@]}"; do
      if [ "${o%%=*}" = "$v" ]; then
        d="${o#*=}"
        echo "${d%% - *}"
        return
      fi
    done
  fi
  echo "${v//,/, }"
}

row() {
  local v max
  if [ "$1" = "IP" ]; then
    v="$(ip_display)"
  else
    v="$(show_val "$1")"
  fi
  if [ -z "$v" ]; then
    v="(none)"
  fi
  max=$(($(dlg_w) - 36 - 8))
  if [ "${#v}" -gt "$max" ]; then
    v="${v:0:$((max - 3))}..."
  fi
  printf '%-34s %s' "$(label_of "$1")" "$v"
}

# validate_value KEY VALUE -> prints the problem and returns 1 if invalid.
validate_value() {
  local k="$1" v="$2" lo hi re
  case "${S_TYPE[$k]}" in
    int)
      lo="${S_OPTS[$k]%%:*}"; hi="${S_OPTS[$k]#*:}"
      if ! [[ "$v" =~ ^[0-9]+$ ]] || [ "$v" -lt "$lo" ] || [ "$v" -gt "$hi" ]; then
        echo "${S_LABEL[$k]} must be a whole number from $lo to $hi."
        return 1
      fi
      ;;
    str)
      re="${S_OPTS[$k]}"
      if [ -n "$re" ] && ! [[ "$v" =~ $re ]]; then
        echo "That doesn't look like a valid value for \"${S_LABEL[$k]}\"."
        return 1
      fi
      ;;
  esac
  if [ "$k" = "CTID" ] && { pct config "$v" >/dev/null 2>&1 || qm config "$v" >/dev/null 2>&1; }; then
    echo "ID $v is already in use on this node."
    return 1
  fi
  return 0
}

help_text() {
  local k="$1"
  if [ -n "${S_HELP[$k]}" ]; then
    printf '%s\n\n%s' "${S_LABEL[$k]}" "${S_HELP[$k]}"
  else
    printf '%s' "${S_LABEL[$k]}"
  fi
}

# --- UI primitives. Each returns 0 (value set) or 1 (back/cancel, value unchanged). ---

ui_input() {
  local k="$1" cur="${!1}" val err text
  text="$(help_text "$k")"
  while :; do
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      val=$(wt --title "${S_LABEL[$k]}" --cancel-button "$NAV_CANCEL" --inputbox "$text" "$(box_h "$text" 8)" 72 "$cur") || return 1
    else
      printf '%b\n' "$text"
      read -rp "[$cur] ('<' = back): " val || return 1
      if [ "$val" = "<" ]; then
        return 1
      fi
      val="${val:-$cur}"
    fi
    if err=$(validate_value "$k" "$val"); then
      printf -v "$k" '%s' "$val"
      return 0
    fi
    ui_msg "$err"
    cur="$val"
  done
}

# ui_pick VAR "title" "text" "value=description" ... - one of a list; the
# current value of VAR is preselected.
# ui_pick VAR "title" "text" "value=description" ... - one of a list; the
# current value of VAR is preselected. Only the descriptions are shown
# (a plain option shows its own text).
ui_pick() {
  local var="$1" title="$2" text="$3" cur="${!1}" val o n i=1 desc
  shift 3
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    local items=() mh=$#
    if [ "$mh" -gt 10 ]; then
      mh=10
    fi
    for o in "$@"; do
      desc="${o#*=}"
      if [ "$o" = "${o%%=*}" ]; then
        desc="$o"
      fi
      items+=("${o%%=*}" "$desc")
    done
    val=$(wt --title "$title" --notags --cancel-button "$NAV_CANCEL" --default-item "$cur" \
      --menu "$text" "$(cap_h $(($(box_h "$text" 0) + mh + 7)))" "$(dlg_w)" "$mh" "${items[@]}") || return 1
  else
    printf '%b\n' "$text"
    for o in "$@"; do
      if [ "$o" = "${o%%=*}" ]; then
        echo "  $i) $o"
      else
        echo "  $i) ${o#*=}"
      fi
      i=$((i + 1))
    done
    read -rp "Choice [current: $cur] ('<' = back): " n || return 1
    if [ "$n" = "<" ]; then
      return 1
    fi
    val="$cur"
    if [ -n "$n" ] && [ "$n" -ge 1 ] 2>/dev/null && [ "$n" -le "$#" ] 2>/dev/null; then
      o="${!n}"
      val="${o%%=*}"
    fi
  fi
  printf -v "$var" '%s' "$val"
}

# ui_multi VAR "title" "text" opt... - one or more; VAR becomes comma-separated.
# ui_multi VAR "title" "text" opt... - one or more; VAR becomes comma-separated.
# Starts with the current selection checked, or everything checked if none
# of the current value is on offer.
ui_multi() {
  local var="$1" title="$2" text="$3" cur=",${!1}," val o n i=1 j picked=()
  shift 3
  local full="${text}\n\nSpace selects or clears an entry; Enter confirms."
  local mh=$#
  if [ "$mh" -gt 8 ]; then
    mh=8
  fi
  while :; do
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      local items=() any_on=0
      for o in "$@"; do
        if [[ "$cur" == *",$o,"* ]]; then
          items+=("$o" "" ON); any_on=1
        else
          items+=("$o" "" OFF)
        fi
      done
      if [ "$any_on" = "0" ]; then
        for ((j = 2; j < ${#items[@]}; j += 3)); do
          items[j]=ON
        done
      fi
      val=$(wt --title "$title" --cancel-button "$NAV_CANCEL" --separate-output \
        --checklist "$full" "$(cap_h $(($(box_h "$full" 0) + mh + 7)))" "$(dlg_w)" "$mh" "${items[@]}") || return 1
      val=$(printf '%s' "$val" | tr '\n' ',' | sed 's/,$//')
    else
      printf '%b\n' "$text"
      for o in "$@"; do
        echo "  $i) $o"
        i=$((i + 1))
      done
      read -rp "Choice(s), comma-separated numbers ('<' = back): " n || return 1
      if [ "$n" = "<" ]; then
        return 1
      fi
      picked=()
      for i in $(printf '%s' "$n" | tr ',' ' '); do
        if [ "$i" -ge 1 ] 2>/dev/null && [ "$i" -le "$#" ] 2>/dev/null; then
          picked+=("${!i}")
        fi
      done
      val=$(IFS=,; printf '%s' "${picked[*]}")
      i=1
    fi
    if [ -n "$val" ]; then
      printf -v "$var" '%s' "$val"
      return 0
    fi
    ui_msg "Select at least one."
  done
}

# --- Lookups that talk to Proxmox (pvesm, pvesh) can take a few seconds - a
# storage that is slow or offline stalls `pvesm status` - so they start in
# the background while the Welcome screen is up and are collected when the
# question that needs them appears.
PREFETCH_DIR=""

# bridges_with_comments -> "name=name - comment" lines for each configured
# bridge (the comment is the one set on the bridge in the Proxmox network
# config), falling back to the kernel's bridge list if pvesh is unavailable.
bridges_with_comments() {
  local node out=""
  node="$(hostname -s 2>/dev/null || hostname)"
  if command -v pvesh >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    out=$(pvesh get "/nodes/${node}/network" --output-format json 2>/dev/null | python3 -c '
import json, sys
try:
    nics = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for n in sorted(nics, key=lambda x: x.get("iface", "")):
    if n.get("type") in ("bridge", "OVSBridge"):
        c = " ".join((n.get("comments") or n.get("comments6") or "").split())
        print(n["iface"] + "=" + n["iface"] + (" - " + c if c else ""))
' 2>/dev/null || true)
  fi
  if [ -z "$out" ]; then
    out=$(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | grep -v '^fwbr' | awk '{print $1"="$1}' || true)
  fi
  printf '%s\n' "$out"
}

start_prefetch() {
  PREFETCH_DIR=$(mktemp -d)
  trap 'rm -rf "$PREFETCH_DIR"' EXIT
  (
    if command -v pvesm >/dev/null 2>&1; then
      pvesm status --content rootdir 2>/dev/null | awk 'NR>1{print $1}' > "$PREFETCH_DIR/rootdir" || true
    fi
    touch "$PREFETCH_DIR/rootdir.done"
  ) &
  (
    if command -v pvesm >/dev/null 2>&1; then
      pvesm status 2>/dev/null | awk 'NR>1 && $2=="pbs"{print $1}' > "$PREFETCH_DIR/pbs" || true
    fi
    touch "$PREFETCH_DIR/pbs.done"
  ) &
  (
    bridges_with_comments > "$PREFETCH_DIR/bridges" || true
    touch "$PREFETCH_DIR/bridges.done"
  ) &
}

# prefetched NAME -> waits (up to a minute) for that lookup, prints its lines.
prefetched() {
  local i=0
  while [ ! -e "$PREFETCH_DIR/$1.done" ] && [ "$i" -lt 600 ]; do
    sleep 0.1
    i=$((i + 1))
  done
  cat "$PREFETCH_DIR/$1" 2>/dev/null || true
}

# --- Per-setting editors ---

edit_STORAGE() {
  local choices=() line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      choices+=("$line")
    fi
  done < <(prefetched rootdir)
  if [ "${#choices[@]}" -gt 0 ]; then
    ui_pick STORAGE "${S_LABEL[STORAGE]}" "$(help_text STORAGE)" "${choices[@]}"
  else
    ui_input STORAGE
  fi
}

edit_BRIDGE() {
  local choices=() line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      choices+=("$line")
    fi
  done < <(prefetched bridges)
  if [ "${#choices[@]}" -gt 0 ]; then
    ui_pick BRIDGE "${S_LABEL[BRIDGE]}" "$(help_text BRIDGE)" "${choices[@]}"
  else
    ui_input BRIDGE
  fi
}

# Only PBS-type storages can hold file-restore-able backups, and the app
# accepts several (comma-separated).
# Only PBS-type storages can hold file-restore-able backups, and the app
# accepts several (comma-separated).
edit_PVE_STORAGE() {
  local choices=() line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      choices+=("$line")
    fi
  done < <(prefetched pbs)
  if [ "${#choices[@]}" -gt 0 ]; then
    ui_multi PVE_STORAGE "${S_LABEL[PVE_STORAGE]}" "$(help_text PVE_STORAGE)" "${choices[@]}"
  else
    ui_input PVE_STORAGE
  fi
}

# Yes/No on screen; true/false (or a CA path) in .env.
edit_PVE_VERIFY_SSL() {
  local mode="$PVE_VERIFY_SSL" prev="$PVE_VERIFY_SSL"
  case "$mode" in
    false|true) ;;
    *) mode=path ;;
  esac
  ui_pick mode "${S_LABEL[PVE_VERIFY_SSL]}" "$(help_text PVE_VERIFY_SSL)" \
    "false=No - don't verify (Proxmox's default certificate is self-signed)" \
    "true=Yes - verify against the container's trust store" \
    "path=Yes - verify against a specific CA file or directory" || return 1
  if [ "$mode" = "path" ]; then
    case "$PVE_VERIFY_SSL" in
      false|true) PVE_VERIFY_SSL="" ;;
    esac
    if ! ui_input PVE_VERIFY_SSL; then
      PVE_VERIFY_SSL="$prev"
      return 1
    fi
  else
    PVE_VERIFY_SSL="$mode"
  fi
}

edit_IP() {
  edit_setting USE_STATIC_IP || return 1
  if [ "$USE_STATIC_IP" = "yes" ]; then
    edit_setting STATIC_CIDR || return 1
    edit_setting STATIC_GW || return 1
  fi
  IP_CONFIG=""
}

edit_setting() {
  local k="$1"
  if declare -F "edit_$k" >/dev/null; then
    "edit_$k"
    return
  fi
  case "${S_TYPE[$k]}" in
    enum)
      local opts
      IFS='|' read -ra opts <<< "${S_OPTS[$k]}"
      ui_pick "$k" "${S_LABEL[$k]}" "$(help_text "$k")" "${opts[@]}"
      ;;
    *) ui_input "$k" ;;
  esac
}

# After a setting changes, fill in whatever it newly requires.
# After a setting changes, fill in whatever it newly requires.
post_edit() {
  case "$1" in
    SETUP_LE)
      if [ "$SETUP_LE" = "yes" ] && [ -z "$LE_DOMAINS" ]; then
        edit_setting LE_DOMAINS || true
      fi
      ;;
  esac
}

# Settings that depend on each other can't be checked field by field.
validate_all() {
  local problems=""
  if [ "$ENABLE_DNT" = "yes" ] && [ -z "${RESTORE_DATA_NICS_JSON:-}" ]; then
    if { [ -n "$DNT_CIDR" ] && [ -z "$DNT_LOCAL_IP" ]; } || { [ -z "$DNT_CIDR" ] && [ -n "$DNT_LOCAL_IP" ]; }; then
      problems="${problems}- Direct Network Transfer: give both the guest subnet and this host's address on it, or leave both blank to detect them automatically.\n"
    fi
  fi
  if [ "$ENABLE_DNT" = "yes" ]; then
    local rank_pref rank_min
    rank_pref=$(tls_rank "$TLS_PREFERRED"); rank_min=$(tls_rank "$TLS_MINIMUM")
    if [ "$rank_min" -gt "$rank_pref" ]; then
      problems="${problems}- The minimum TLS mode ($TLS_MINIMUM) is stricter than the preferred mode ($TLS_PREFERRED).\n"
    fi
  fi
  if [ "$SETUP_LE" = "yes" ] && [ -z "$LE_DOMAINS" ]; then
    problems="${problems}- A certificate was requested but no domain was given.\n"
  fi
  if [ -z "$PVE_STORAGE" ]; then
    problems="${problems}- No PBS storage is selected.\n"
  fi
  if [ -n "$problems" ]; then
    printf '%b' "$problems"
    return 1
  fi
}

tls_rank() {
  case "$1" in plaintext) echo 0 ;; insecure) echo 1 ;; *) echo 2 ;; esac
}

# ---------------------------------------------------------------------
# Advanced settings: a menu of groups, each a menu of its settings.
# ---------------------------------------------------------------------
group_keys() {
  local k
  for k in "${ALL_KEYS[@]}"; do
    if [ "${S_GROUP[$k]}" = "$1" ] && cond_ok "$k"; then
      echo "$k"
    fi
  done
}

group_summary() {
  local k n=0
  for k in $(group_keys "$1"); do
    if [ "${!k}" != "${S_DEF[$k]}" ]; then
      n=$((n + 1))
    fi
  done
  if [ "$n" -eq 0 ]; then
    echo "defaults"
  else
    echo "$n changed"
  fi
}

group_editor() {
  local g="$1" k choice last="" n
  while :; do
    local items=()
    for k in $(group_keys "$g"); do
      items+=("$k" "$(row "$k")")
    done
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      choice=$(wt --title "${G_TITLE[$g]}" --notags --ok-button "Edit" --cancel-button "Back" \
        ${last:+--default-item "$last"} --menu "Select a setting to change." 22 78 14 "${items[@]}") || return 0
    else
      echo
      echo "${G_TITLE[$g]}"
      n=1
      for k in $(group_keys "$g"); do
        echo "  $n) $(row "$k")"
        n=$((n + 1))
      done
      read -rp "Number to edit, Enter to go back: " n || return 0
      if [ -z "$n" ]; then
        return 0
      fi
      choice=$(group_keys "$g" | sed -n "${n}p")
      if [ -z "$choice" ]; then
        continue
      fi
    fi
    last="$choice"
    NAV_CANCEL="Cancel"
    if edit_setting "$choice"; then
      post_edit "$choice"
    fi
  done
}

advanced_menu() {
  local g choice last="" n
  while :; do
    local items=()
    for g in "${ADV_GROUPS[@]}"; do
      items+=("$g" "$(printf '%-30s %s' "${G_TITLE[$g]}" "($(group_summary "$g"))")")
    done
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      choice=$(wt --title "Advanced settings" --notags --ok-button "Open" --cancel-button "Back" \
        ${last:+--default-item "$last"} --menu "Everything here has a sensible default - change only what you need." 18 78 8 "${items[@]}") || return 0
    else
      echo
      echo "Advanced settings"
      n=1
      for g in "${ADV_GROUPS[@]}"; do
        echo "  $n) ${G_TITLE[$g]} ($(group_summary "$g"))"
        n=$((n + 1))
      done
      read -rp "Number to open, Enter to go back: " n || return 0
      if [ -z "$n" ]; then
        return 0
      fi
      choice="${ADV_GROUPS[$((n - 1))]:-}"
      if [ -z "$choice" ]; then
        continue
      fi
    fi
    last="$choice"
    group_editor "$choice"
  done
}

adv_total_summary() {
  local g n=0 k
  for g in "${ADV_GROUPS[@]}"; do
    for k in $(group_keys "$g"); do
      if [ "${!k}" != "${S_DEF[$k]}" ]; then
        n=$((n + 1))
      fi
    done
  done
  if [ "$n" -eq 0 ]; then
    echo "all defaults"
  else
    echo "$n changed"
  fi
}

# ---------------------------------------------------------------------
# Review screen: every basic setting, Advanced, and Install.
# ---------------------------------------------------------------------
review_hub() {
  local last="GO" choice k n problems nsep rows
  while :; do
    local items=()
    nsep=0
    for k in "${BASIC_LAYOUT[@]}"; do
      if [[ "$k" == H:* ]]; then
        nsep=$((nsep + 1))
        items+=("SEP$nsep" "--- ${k#H:} ---")
      else
        items+=("$k" "$(row "$k")")
      fi
    done
    items+=("ADV" "Advanced settings...  ($(adv_total_summary))" "GO" ">>> Install now")
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      rows=$((${#items[@]} / 2))
      if [ "$rows" -gt 16 ]; then
        rows=16
      fi
      choice=$(wt --title "Review" --notags --ok-button "Select" --cancel-button "Quit" --default-item "$last" \
        --menu "Select a setting to change it, or choose \"Install now\" when you're ready." \
        "$(cap_h $((rows + 2 + 7)))" "$(dlg_w)" "$rows" "${items[@]}") || {
        if ui_confirm "Quit without installing anything?"; then
          msg_err "Cancelled."
          exit 1
        fi
        continue
      }
    else
      echo
      msg_info "Review"
      n=1
      for k in "${BASIC_LAYOUT[@]}"; do
        if [[ "$k" == H:* ]]; then
          echo "  --- ${k#H:} ---"
        else
          echo "  $n) $(row "$k")"
          n=$((n + 1))
        fi
      done
      echo "  a) Advanced settings ($(adv_total_summary))"
      read -rp "Number to change, 'a' for advanced, Enter to install, 'q' to quit: " n || n="q"
      case "$n" in
        "") choice="GO" ;;
        a|A) choice="ADV" ;;
        q|Q) msg_err "Cancelled."; exit 1 ;;
        *) choice="${BASIC_KEYS[$((n - 1))]:-}"; if [ -z "$choice" ]; then continue; fi ;;
      esac
    fi
    last="$choice"
    NAV_CANCEL="Cancel"
    case "$choice" in
      SEP*) ;;
      GO)
        if problems=$(validate_all); then
          return 0
        fi
        ui_msg "Please fix these first:\n\n$problems"
        ;;
      ADV) advanced_menu ;;
      *)
        if edit_setting "$choice"; then
          post_edit "$choice"
        fi
        ;;
    esac
  done
}

# ---------------------------------------------------------------------
# The wizard: Welcome, then one question at a time. Back (Cancel) steps
# back, skipping questions that were pre-set or don't apply.
# ---------------------------------------------------------------------
WELCOME_TEXT="Welcome to the pve-flr-portal installer.\n\nFile Level Restore Portal for Proxmox Backup Server: a scrubbable snapshot timeline for browsing and restoring files from your PBS backups.\n\nThis will create an unprivileged Debian 12 container on this PVE host, install the portal in it, and write your answers into its .env.\n\nA few essential questions come first (use Back to step back). A review screen then lets you change anything - including Advanced options - before anything is created.\n\nAnything you pre-set through environment variables is skipped."

WIZARD=(WELCOME CTID CT_HOSTNAME STORAGE BRIDGE VLAN_TAG USE_STATIC_IP STATIC_CIDR STATIC_GW PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)

step_skipped() {
  if [ "$1" = "WELCOME" ]; then
    return 1
  fi
  [ -n "${PRESET[$1]:-}" ] || ! cond_ok "$1"
}

show_welcome() {
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    wt --title "Welcome" --yes-button "Continue" --no-button "Exit" --yesno "$WELCOME_TEXT" "$(box_h "$WELCOME_TEXT" 8)" 72 || {
      msg_err "Cancelled."
      exit 1
    }
  else
    printf '%b\n\n' "$WELCOME_TEXT"
    read -rp "Press Enter to continue (Ctrl-C to quit) " _ || exit 1
  fi
}

run_wizard() {
  local i=0 n=${#WIZARD[@]} dir=fwd k rc
  while [ "$i" -lt "$n" ]; do
    k="${WIZARD[$i]}"
    if step_skipped "$k"; then
      if [ "$dir" = "back" ]; then
        i=$((i - 1))
      else
        i=$((i + 1))
      fi
      continue
    fi
    if [ "$k" = "WELCOME" ]; then
      show_welcome
      dir=fwd
      i=$((i + 1))
      continue
    fi
    NAV_CANCEL="Back"
    rc=0
    edit_setting "$k" || rc=$?
    if [ "$rc" -eq 0 ]; then
      dir=fwd
      i=$((i + 1))
    else
      dir=back
      i=$((i - 1))
    fi
  done
}

if [ "$IS_TTY" = "1" ]; then
  start_prefetch
  run_wizard
  review_hub
else
  if ! problems=$(validate_all); then
    msg_err "Invalid settings:"
    printf '%b' "$problems" >&2
    exit 1
  fi
fi

# Derived values the rest of the script consumes.
if [ -z "${IP_CONFIG:-}" ]; then
  if [ "$USE_STATIC_IP" = "yes" ]; then
    IP_CONFIG="${STATIC_CIDR},gw=${STATIC_GW}"
  else
    IP_CONFIG="dhcp"
  fi
fi

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

NET0="name=eth0,bridge=${BRIDGE},ip=${IP_CONFIG}"
if [ -n "$VLAN_TAG" ]; then
  NET0="${NET0},tag=${VLAN_TAG}"
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
# Write the resolved app settings into the container's .env - pushed as
# a KEY=VALUE file and patched in with a small Python script (already a
# guaranteed-present dependency) rather than sed, so JSON values like
# RESTORE_DATA_NICS don't need shell/sed delimiter escaping.
# ---------------------------------------------------------------------
# Direct Network Transfer: where the guests reach this portal. Unless a
# subnet/address was given, use the container's own - its address can only
# be read now that it's up.
resolve_dnt() {
  local addr detected
  if [ "$ENABLE_DNT" != "yes" ] || [ -n "${RESTORE_DATA_NICS_JSON:-}" ]; then
    return 0
  fi
  if [ -z "$DNT_CIDR" ] || [ -z "$DNT_LOCAL_IP" ]; then
    addr=$(pct exec "$CTID" -- sh -c "ip -4 -o addr show dev eth0 | awk '{print \$4}' | head -1" 2>/dev/null || true)
    detected=""
    if [ -n "$addr" ]; then
      detected=$(pct exec "$CTID" -- python3 -c 'import ipaddress, sys; i = ipaddress.ip_interface(sys.argv[1]); print(i.network, i.ip)' "$addr" 2>/dev/null || true)
    fi
    if [ -z "$detected" ]; then
      msg_err "Couldn't read the container's network address - Direct Network Transfer left off (set RESTORE_DATA_NICS in .env to enable it)."
      ENABLE_DNT=no
      return 0
    fi
    if [ -z "$DNT_CIDR" ]; then
      DNT_CIDR="${detected% *}"
    fi
    if [ -z "$DNT_LOCAL_IP" ]; then
      DNT_LOCAL_IP="${detected#* }"
    fi
  fi
  RESTORE_DATA_NICS_JSON="[{\"cidr\":\"${DNT_CIDR}\",\"local_ip\":\"${DNT_LOCAL_IP}\""
  if [ -n "$DNT_HOSTNAME" ]; then
    RESTORE_DATA_NICS_JSON="${RESTORE_DATA_NICS_JSON},\"hostname\":\"${DNT_HOSTNAME}\""
  fi
  RESTORE_DATA_NICS_JSON="${RESTORE_DATA_NICS_JSON}}]"
  msg_ok "Direct Network Transfer: guests on ${DNT_CIDR} will fetch from ${DNT_LOCAL_IP}"
}
resolve_dnt

ENV_OVERRIDES=()
for _k in "${ALL_KEYS[@]}"; do
  _env="${S_ENV[$_k]}"
  if [ -z "$_env" ] || ! cond_ok "$_k"; then
    continue
  fi
  # Only what differs from the app's own default is written, except the two
  # settings the portal can't work without.
  if [ "${!_k}" != "${S_DEF[$_k]}" ] || [ "$_k" = "PVE_HOST" ] || [ "$_k" = "PVE_STORAGE" ]; then
    ENV_OVERRIDES+=("${_env}=${!_k}")
  fi
done
if [ "$ENABLE_DNT" = "yes" ]; then
  ENV_OVERRIDES+=("RESTORE_DATA_NICS=${RESTORE_DATA_NICS_JSON}")
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
msg_ok "Done. pve-flr-portal is running in CT $CTID."
echo "    https://${CT_IP}:${PORTAL_PORT}/"
echo "    To update later: pct exec $CTID -- bash /opt/pve-flr-portal/deploy/update.sh"
if [ "$ENABLE_DNT" = "yes" ] && [ "$IP_CONFIG" = "dhcp" ]; then
  echo "    Direct Network Transfer is bound to the container's current DHCP address ($DNT_LOCAL_IP)."
  echo "    Give the container a DHCP reservation (or a static IP); if the address changes,"
  echo "    update RESTORE_DATA_NICS in /opt/pve-flr-portal/.env and restart the service."
fi
if [ "$ENABLE_HOST_BACKUPS" = "true" ]; then
  echo "    Host-backup support patched on THIS node only - on a cluster,"
  echo "    also run deploy/patch-pve-host-backups.sh on every other node."
fi
