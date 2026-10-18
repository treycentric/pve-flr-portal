# shellcheck shell=bash
# Shared settings + guided-UI library for the pve-flr-portal deployment
# scripts: the settings registry (what each setting is, its default and
# validation), the whiptail dialogs (with a plain-prompt fallback), and the
# helpers that read and write a container's configuration.
#
# Sourced - never executed - by deploy/lxc-create.sh (the installer) and
# deploy/pfr-manage.sh (the host management tool). Nothing here runs at
# source time beyond defining functions and the registry.
#
# Bump PFR_LIB_API on an incompatible change: the installer refuses a
# library whose API differs from the one it was written for.
PFR_LIB_API=1

# install: the installer (every setting, wizard + review screen).
# config: the management tool (only what can change after install).
PFR_MODE="${PFR_MODE:-install}"

# Wording of the review screen's last row, text and quit prompt.
# The review screen's proceed action is the dialog's own button (whiptail
# has OK and Cancel only, so Cancel carries it); Esc quits.
HUB_GO_BUTTON="Install now"
HUB_TEXT="Highlight a setting and press Enter to change it. Choose \"Install now\" when you're ready, or press Esc to quit."
HUB_QUIT_TEXT="Quit without installing anything?"

# pfr_cancel - the person quit. A non-zero status for the installer (nothing
# was installed); the management tool sets PFR_CANCEL_RC=0 (nothing changed).
pfr_cancel() {
  msg_err "Cancelled."
  exit "${PFR_CANCEL_RC:-1}"
}

# Temporary files and directories are registered here and removed when the
# script exits, however it exits (success, `die`, Ctrl-C, a failed command
# under `set -e`).
PFR_TMP_PATHS=()

# pfr_mktemp VAR [mktemp args] - create a temp file (or directory with -d)
# and store its path in VAR. Not used in a $(...), which would lose the
# registration.
pfr_mktemp() {
  local p
  p=$(mktemp "${@:2}")
  PFR_TMP_PATHS+=("$p")
  printf -v "$1" '%s' "$p"
}

pfr_cleanup() {
  if [ "${#PFR_TMP_PATHS[@]}" -gt 0 ]; then
    rm -rf "${PFR_TMP_PATHS[@]}"
  fi
}
trap pfr_cleanup EXIT

# Status helpers - the scripts that source this normally define their own.
if ! declare -F msg_info >/dev/null; then
  if [ -t 1 ]; then
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
  else
    C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""
  fi
  msg_info() { echo "${C_YELLOW}==>${C_RESET} $*"; }
  msg_ok()   { echo "${C_GREEN}==>${C_RESET} $*"; }
  msg_err()  { echo "${C_RED}==>${C_RESET} $*" >&2; }
fi

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
HAS_WHIPTAIL=0

# pfr_detect_ui - sets IS_TTY (a real terminal on stdin and stdout) and
# HAS_WHIPTAIL (whiptail present, installing it if needed). Without a
# terminal nothing is ever asked.
pfr_detect_ui() {
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
}

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

# box_h "text" extra [width] -> a dialog height that fits the wrapped text
# plus `extra` rows of controls, capped to the terminal. Wraps a little
# narrower than the dialog so an estimate errs on the tall side.
box_h() {
  local lines h rows w="${3:-72}"
  lines=$(printf '%b' "$1" | fold -s -w $((w - 8)) | wc -l)
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

# ui_ask_yes TEXT - a yes/no question whose default answer is Yes.
ui_ask_yes() {
  local reply
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    local need scroll=""
    need=$(box_h "$1" 7 "$(dlg_w)")
    if [ "$need" -ge "$(($(term_rows) - 2))" ]; then
      scroll="--scrolltext"
    fi
    wt --title "$TITLE" ${scroll:+$scroll} --yesno "$1" "$(cap_h "$need")" "$(dlg_w)"
  else
    printf '%b
' "$1"
    read -rp "[Y/n]: " reply || reply="n"
    case "$reply" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
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
# -g: this file is also sourced from inside a function (the installer's
# load_lib), where a plain declare would make these local and lose them.
declare -gA S_LABEL S_TYPE S_DEF S_ENV S_GROUP S_OPTS S_COND S_HELP PRESET
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
def USE_STATIC_IP "IPv4 configuration" enum no "" basic "no=DHCP - the address is assigned automatically|yes=Static - enter an address and gateway" "" "How should the container get its IPv4 network configuration?"
def STATIC_CIDR "IPv4/CIDR" str 10.0.0.50/24 "" basic '^[0-9.]+/[0-9]+$' "USE_STATIC_IP=yes" "The container's address and prefix length, e.g. 10.0.0.50/24."
def STATIC_GW "Gateway (IPv4)" str 10.0.0.1 "" basic '^[0-9.]+$' "USE_STATIC_IP=yes" "The default gateway for the container."
def IPV6_MODE "IPv6 configuration" enum auto "" basic "auto=SLAAC - automatic, from router advertisements|dhcp=DHCPv6|static=Static - enter an address and gateway|disabled=Disabled - turn IPv6 off in the container" "" "How should the container get its IPv6 network configuration? Choose Disabled to turn IPv6 off inside the container."
def STATIC6_CIDR "IPv6/CIDR" str "" "" basic '^([0-9A-Fa-f:]+/[0-9]{1,3})?$' "IPV6_MODE=static" "The container's IPv6 address and prefix length, e.g. 2001:db8::50/64."
def STATIC6_GW "Gateway (IPv6)" str "" "" basic '^([0-9A-Fa-f:]+)?$' "IPV6_MODE=static" "The default IPv6 gateway. Leave blank for none."
def DNS_SERVERS "DNS servers" str "" "" basic '^([0-9A-Fa-f:.]+([ ,]+[0-9A-Fa-f:.]+)*)?$' "USE_STATIC_IP=yes||IPV6_MODE=static" "One or more DNS server addresses (IPv4 or IPv6), separated by spaces or commas. Leave blank to use the PVE host's DNS settings."
def SEARCH_DOMAIN "DNS search domain" str "" "" basic '^([A-Za-z0-9.-]+)?$' "USE_STATIC_IP=yes||IPV6_MODE=static" "For example example.com. Leave blank to use the PVE host's setting."
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

BASIC_KEYS=(CTID CT_HOSTNAME STORAGE BRIDGE VLAN_TAG IP IP6 DNS PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)
# The review screen groups them under headings ("H:" rows are headings).
BASIC_LAYOUT=("H:Container" CTID CT_HOSTNAME STORAGE "H:Network" BRIDGE VLAN_TAG IP IP6 DNS "H:Portal" PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)
ADV_GROUPS=(resources portal access limits dnt cert)
declare -gA G_TITLE=(
  [resources]="Container resources"
  [portal]="Portal"
  [access]="Restore-job visibility"
  [limits]="Performance and limits"
  [dnt]="Direct Network Transfer"
  [cert]="Certificate (ACME / DNS-01)"
)

# pfr_init_install - installer start: note which settings the caller pre-set
# through the environment (their wizard questions are skipped), and fill
# every other setting with its default.
pfr_init_install() {
  local k
  for k in "${ALL_KEYS[@]}"; do
    if [ -n "${!k:-}" ]; then
      PRESET[$k]=1
    else
      printf -v "$k" '%s' "${S_DEF[$k]}"
    fi
  done
  if [ -n "${IP_CONFIG:-}" ]; then
    PRESET[USE_STATIC_IP]=1; PRESET[STATIC_CIDR]=1; PRESET[STATIC_GW]=1; PRESET[DNS_SERVERS]=1; PRESET[SEARCH_DOMAIN]=1
  fi
  if [ -n "${RESTORE_DATA_NICS_JSON:-}" ]; then
    if [ -z "${PRESET[ENABLE_DNT]:-}" ]; then
      ENABLE_DNT=yes
      PRESET[ENABLE_DNT]=1
    fi
    PRESET[DNT_CIDR]=1; PRESET[DNT_LOCAL_IP]=1; PRESET[DNT_HOSTNAME]=1
  fi
}

# A condition is "KEY=value", or alternatives joined with "||".
cond_ok() {
  local c="${S_COND[$1]:-}" alt ref
  if [ -z "$c" ]; then
    return 0
  fi
  for alt in ${c//||/ }; do
    ref="${alt%%=*}"
    if [ "${!ref}" = "${alt#*=}" ]; then
      return 0
    fi
  done
  return 1
}

# DNS is only asked for when an address is entered by hand (DHCP hands it out).
dns_needed() {
  [ "$USE_STATIC_IP" = "yes" ] || [ "$IPV6_MODE" = "static" ]
}

ip_display() {
  if [ -n "${IP_CONFIG:-}" ]; then
    echo "$IP_CONFIG"
  elif [ "$USE_STATIC_IP" = "manual" ]; then
    echo "Manual (not managed here)"
  elif [ "$USE_STATIC_IP" = "yes" ]; then
    echo "$STATIC_CIDR (gateway $STATIC_GW)"
  else
    echo "DHCP"
  fi
}

ip6_display() {
  case "$IPV6_MODE" in
    auto) echo "SLAAC" ;;
    dhcp) echo "DHCPv6" ;;
    static) echo "${STATIC6_CIDR:-(not set)}${STATIC6_GW:+ (gateway $STATIC6_GW)}" ;;
    none) echo "Not configured" ;;
    manual) echo "Manual (not managed here)" ;;
    *) echo "Disabled" ;;
  esac
}

dns_display() {
  local v="$DNS_SERVERS"
  if [ -z "$v" ]; then
    v="PVE host settings"
  fi
  echo "${v}${SEARCH_DOMAIN:+ (search $SEARCH_DOMAIN)}"
}

label_of() {
  case "$1" in
    IP) echo "IPv4 address" ;;
    IP6) echo "IPv6 address" ;;
    DNS) echo "DNS" ;;
    *) echo "${S_LABEL[$1]}" ;;
  esac
}

# show_val KEY -> the value as a person would read it (Yes/No, a label).
# show_val KEY [VALUE] -> the value as a person would read it (Yes/No, a
# label). VALUE defaults to the setting's current value.
show_val() {
  local k="$1" v="${2-${!1}}" opts o d
  if [ "$k" = "PVE_VERIFY_SSL" ]; then
    case "$v" in
      false) echo "No" ;;
      true) echo "Yes" ;;
      *) echo "Yes (CA: $v)" ;;
    esac
    return
  fi
  case "$v" in
    custom) echo "Custom (set in .env)"; return ;;
    none) echo "Not configured"; return ;;
    manual) echo "Manual (not managed here)"; return ;;
  esac
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
  local v max mark=""
  case "$1" in
    IP) v="$(ip_display)" ;;
    IP6) v="$(ip6_display)" ;;
    DNS) v="$(dns_display)" ;;
    *) v="$(show_val "$1")" ;;
  esac
  if [ -z "$v" ]; then
    v="(none)"
  fi
  if [ "$PFR_MODE" = "config" ] && pfr_changed "$1"; then
    mark=" *"
  fi
  max=$(($(dlg_w) - 36 - 10))
  if [ "${#v}" -gt "$max" ]; then
    v="${v:0:$((max - 3))}..."
  fi
  printf '%-34s %s%s' "$(label_of "$1")" "$v" "$mark"
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
  local var="$1" title="$2" text="$3" cur="${!1}" val o n i=1 desc need scroll
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
    # --scrolltext only when the text can't fit: it moves the initial focus
    # into the text area, which makes people tab to reach the list.
    need=$(($(box_h "$text" 0 "$(dlg_w)") + mh + 9))
    scroll=""
    if [ "$need" -gt "$(($(term_rows) - 2))" ]; then
      scroll="--scrolltext"
    fi
    val=$(wt --title "$title" --notags --cancel-button "$NAV_CANCEL" --default-item "$cur" \
      ${scroll:+$scroll} --menu "$text" "$(cap_h "$need")" "$(dlg_w)" "$mh" "${items[@]}") || return 1
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
  local var="$1" title="$2" text="$3" cur=",${!1}," val o n i=1 j picked=() need scroll
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
      need=$(($(box_h "$full" 0 "$(dlg_w)") + mh + 9))
      scroll=""
      if [ "$need" -gt "$(($(term_rows) - 2))" ]; then
        scroll="--scrolltext"
      fi
      val=$(wt --title "$title" --cancel-button "$NAV_CANCEL" --separate-output \
        ${scroll:+$scroll} --checklist "$full" "$(cap_h "$need")" "$(dlg_w)" "$mh" "${items[@]}") || return 1
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
  pfr_mktemp PREFETCH_DIR -d
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

edit_IP6() {
  edit_setting IPV6_MODE || return 1
  if [ "$IPV6_MODE" = "static" ]; then
    edit_setting STATIC6_CIDR || return 1
    edit_setting STATIC6_GW || return 1
  fi
}

edit_DNS() {
  edit_setting DNS_SERVERS || return 1
  edit_setting SEARCH_DOMAIN || return 1
}

edit_setting() {
  local k="$1" opts
  if declare -F "edit_$k" >/dev/null; then
    "edit_$k"
    return
  fi
  case "${S_TYPE[$k]}" in
    enum)
      IFS='|' read -ra opts <<< "${S_OPTS[$k]}"
      # A value found on an existing install that the dialogs can't create
      # (a hand-written net0, several data networks) stays selectable, so
      # looking at a setting never forces a change.
      case "${ORIG[$k]-}" in
        custom) opts+=("custom=Custom - several networks, set in .env (kept as is)") ;;
        none) opts+=("none=Not configured - left to the system") ;;
        manual) opts+=("manual=Manual - not managed here") ;;
      esac
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
  if [ "$IPV6_MODE" = "static" ] && [ -z "$STATIC6_CIDR" ]; then
    problems="${problems}- Static IPv6 was chosen but no IPv6 address was given.\n"
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

# pfr_differs KEY - installer: differs from the default. Management tool:
# differs from what the container had when the tool started (a pending change).
pfr_differs() {
  if [ "$PFR_MODE" = "config" ]; then
    [ "${!1}" != "${ORIG[$1]-}" ]
  else
    [ "${!1}" != "${S_DEF[$1]}" ]
  fi
}

group_summary() {
  local k n=0
  for k in $(group_keys "$1"); do
    if pfr_differs "$k"; then
      n=$((n + 1))
    fi
  done
  if [ "$n" -eq 0 ]; then
    if [ "$PFR_MODE" = "config" ]; then
      echo "no changes"
    else
      echo "defaults"
    fi
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
      if pfr_differs "$k"; then
        n=$((n + 1))
      fi
    done
  done
  if [ "$n" -eq 0 ]; then
    if [ "$PFR_MODE" = "config" ]; then
      echo "no changes"
    else
      echo "all defaults"
    fi
  else
    echo "$n changed"
  fi
}

# ---------------------------------------------------------------------
# Review screen: every basic setting, Advanced, and Install.
# ---------------------------------------------------------------------
review_hub() {
  local last="" choice k n problems nsep rows rc
  local PLAIN_MAP
  while :; do
    local items=()
    PLAIN_MAP=()
    nsep=0
    for k in "${BASIC_LAYOUT[@]}"; do
      if [[ "$k" == H:* ]]; then
        nsep=$((nsep + 1))
        items+=("SEP$nsep" "[ ${k#H:} ]")
      elif [ "$k" = "DNS" ] && ! dns_needed; then
        continue
      else
        items+=("$k" "$(row "$k")")
      fi
    done
    items+=("ADV" "Advanced settings...  ($(adv_total_summary))")
    if [ "$HAS_WHIPTAIL" = "1" ]; then
      rows=$((${#items[@]} / 2))
      if [ "$rows" -gt 16 ]; then
        rows=16
      fi
      rc=0
      choice=$(wt --title "Review" --notags --ok-button "Edit (Enter)" --cancel-button "$HUB_GO_BUTTON" \
        ${last:+--default-item "$last"} --menu "$HUB_TEXT" \
        "$(cap_h $((rows + 2 + 7)))" "$(dlg_w)" "$rows" "${items[@]}") || rc=$?
      if [ "$rc" -eq 1 ]; then
        choice="GO"
      elif [ "$rc" -ne 0 ]; then
        # Esc (or the dialog failing): offer to quit
        if ui_confirm "$HUB_QUIT_TEXT"; then
          pfr_cancel
        fi
        continue
      fi
    else
      echo
      msg_info "Review"
      n=1
      for k in "${BASIC_LAYOUT[@]}"; do
        if [[ "$k" == H:* ]]; then
          echo "  --- ${k#H:} ---"
        elif [ "$k" = "DNS" ] && ! dns_needed; then
          continue
        else
          echo "  $n) $(row "$k")"
          PLAIN_MAP[n]="$k"
          n=$((n + 1))
        fi
      done
      echo "  a) Advanced settings ($(adv_total_summary))"
      read -rp "Number to change, 'a' for advanced, Enter to install, 'q' to quit: " n || n="q"
      case "$n" in
        "") choice="GO" ;;
        a|A) choice="ADV" ;;
        q|Q) pfr_cancel ;;
        *) choice="${PLAIN_MAP[$n]:-}"; if [ -z "$choice" ]; then continue; fi ;;
      esac
    fi
    if [ "$choice" != "GO" ]; then
      last="$choice"
    fi
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

WIZARD=(WELCOME CTID CT_HOSTNAME STORAGE BRIDGE VLAN_TAG USE_STATIC_IP STATIC_CIDR STATIC_GW IPV6_MODE STATIC6_CIDR STATIC6_GW DNS_SERVERS SEARCH_DOMAIN PVE_HOST PVE_STORAGE ENABLE_DNT ENABLE_HOST_BACKUPS)

step_skipped() {
  if [ "$1" = "WELCOME" ]; then
    return 1
  fi
  [ -n "${PRESET[$1]:-}" ] || ! cond_ok "$1"
}

show_welcome() {
  if [ "$HAS_WHIPTAIL" = "1" ]; then
    wt --title "Welcome" --yes-button "Continue" --no-button "Exit" --yesno "$WELCOME_TEXT" "$(box_h "$WELCOME_TEXT" 8)" 72 || {
      pfr_cancel
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

# pfr_run_install_ui - the guided flow at a terminal; unattended otherwise
# (conflicting settings are an error, never a prompt).
pfr_run_install_ui() {
  local problems
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
}

# pfr_derive_install - values the rest of the installer consumes.
pfr_derive_install() {
  if [ -z "${IP_CONFIG:-}" ]; then
    if [ "$USE_STATIC_IP" = "manual" ]; then
      IP_CONFIG="manual"
    elif [ "$USE_STATIC_IP" = "yes" ]; then
      IP_CONFIG="${STATIC_CIDR},gw=${STATIC_GW}"
    else
      IP_CONFIG="dhcp"
    fi
  fi
}

# =====================================================================
# Container helpers - used once a container exists.
# =====================================================================
PFR_APP_DIR="/opt/pve-flr-portal"
PFR_ENV_FILE="$PFR_APP_DIR/.env"
PFR_CT_TAG="pve-flr-portal"
PFR_IPV6_SYSCTL="/etc/sysctl.d/99-disable-ipv6.conf"
PFR_ORIG_NICS=""
PFR_OVERRIDES_TMP="/tmp/pfr-overrides.env"

# The values at the start of a config session, so changes can be listed.
declare -gA ORIG
pfr_snapshot() {
  local k
  ORIG=()
  for k in "${ALL_KEYS[@]}"; do
    ORIG[$k]="${!k}"
  done
}

# pfr_changed KEY|IP|IP6|DNS - has the setting changed since pfr_snapshot?
pfr_changed() {
  local k
  case "$1" in
    IP) set -- USE_STATIC_IP STATIC_CIDR STATIC_GW ;;
    IP6) set -- IPV6_MODE STATIC6_CIDR STATIC6_GW ;;
    DNS) set -- DNS_SERVERS SEARCH_DOMAIN ;;
  esac
  for k in "$@"; do
    if [ "${!k}" != "${ORIG[$k]-}" ]; then
      return 0
    fi
  done
  return 1
}

# pfr_network_changed - did anything that lives in the container's own
# configuration (not .env) change?
pfr_network_changed() {
  local k
  for k in CT_HOSTNAME BRIDGE VLAN_TAG USE_STATIC_IP STATIC_CIDR STATIC_GW IPV6_MODE STATIC6_CIDR STATIC6_GW DNS_SERVERS SEARCH_DOMAIN; do
    if [ "${!k}" != "${ORIG[$k]-}" ]; then
      return 0
    fi
  done
  return 1
}

# pfr_build_net0 [ORIGINAL] - the net0 value for the current network
# settings. Given the container's existing net0, every field this tool
# doesn't manage (hwaddr, firewall, mtu, rate, ...) is carried over, so a
# change never alters the MAC address or other hand-set options.
pfr_build_net0() {
  local orig="${1:-}" part k out="" v4 parts=()
  local -A keep=()
  local order=()
  if [ -n "$orig" ]; then
    IFS=',' read -ra parts <<< "$orig"
    for part in "${parts[@]}"; do
      k="${part%%=*}"
      case "$k" in
        bridge|tag|ip|gw|ip6|gw6) ;;
        *) keep[$k]="${part#*=}"; order+=("$k") ;;
      esac
    done
  else
    keep[name]="eth0"
    order=(name)
  fi
  for k in "${order[@]}"; do
    out="${out}${k}=${keep[$k]},"
  done
  out="${out}bridge=${BRIDGE}"
  if [ -n "$VLAN_TAG" ]; then
    out="${out},tag=${VLAN_TAG}"
  fi
  if [ -n "${IP_CONFIG:-}" ]; then
    v4="$IP_CONFIG"
  elif [ "$USE_STATIC_IP" = "yes" ]; then
    v4="${STATIC_CIDR},gw=${STATIC_GW}"
  elif [ "$USE_STATIC_IP" = "manual" ]; then
    v4="manual"
  else
    v4="dhcp"
  fi
  out="${out},ip=${v4}"
  case "$IPV6_MODE" in
    auto) out="${out},ip6=auto" ;;
    dhcp) out="${out},ip6=dhcp" ;;
    manual) out="${out},ip6=manual" ;;
    static)
      out="${out},ip6=${STATIC6_CIDR}"
      if [ -n "$STATIC6_GW" ]; then
        out="${out},gw6=${STATIC6_GW}"
      fi
      ;;
  esac
  printf '%s' "$out"
}

# pfr_parse_net0 STRING - the inverse: set the network settings from a
# container's net0 value. IPv6 without an ip6 field reads as "none"
# (unmanaged); the caller refines that to "disabled" if the container has
# the IPv6-off sysctl file.
pfr_parse_net0() {
  local part k v ip="" gw="" ip6="" gw6="" parts=()
  BRIDGE="vmbr0"
  VLAN_TAG=""
  IFS=',' read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    k="${part%%=*}"
    v="${part#*=}"
    case "$k" in
      bridge) BRIDGE="$v" ;;
      tag) VLAN_TAG="$v" ;;
      ip) ip="$v" ;;
      gw) gw="$v" ;;
      ip6) ip6="$v" ;;
      gw6) gw6="$v" ;;
    esac
  done
  case "$ip" in
    dhcp) USE_STATIC_IP="no" ;;
    ""|manual) USE_STATIC_IP="manual" ;;
    *) USE_STATIC_IP="yes"; STATIC_CIDR="$ip"; STATIC_GW="$gw" ;;
  esac
  case "$ip6" in
    auto) IPV6_MODE="auto" ;;
    dhcp) IPV6_MODE="dhcp" ;;
    manual) IPV6_MODE="manual" ;;
    "") IPV6_MODE="none" ;;
    *) IPV6_MODE="static"; STATIC6_CIDR="$ip6"; STATIC6_GW="$gw6" ;;
  esac
}

# pfr_apply_ipv6_sysctl CTID - make the container's IPv6 match IPV6_MODE:
# "disabled" turns it off inside the container (an unset ip6 alone would
# still let the kernel autoconfigure from router advertisements); any other
# mode removes that file again.
pfr_apply_ipv6_sysctl() {
  if [ "$IPV6_MODE" = "disabled" ]; then
    pct exec "$1" -- sh -c "printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' > $PFR_IPV6_SYSCTL; sysctl -p $PFR_IPV6_SYSCTL >/dev/null 2>&1 || true"
  elif [ "$IPV6_MODE" != "none" ] && [ "$IPV6_MODE" != "manual" ]; then
    pct exec "$1" -- sh -c "rm -f $PFR_IPV6_SYSCTL; sysctl -w net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.default.disable_ipv6=0 >/dev/null 2>&1 || true"
  fi
}

# pfr_pull CTID SRC DEST - copy a file out of the container. Fails, leaving
# DEST alone, if the file isn't there or comes back empty: `pct pull` can
# report success and write an empty file for a missing source.
pfr_pull() {
  local tmp
  pct exec "$1" -- test -f "$2" 2>/dev/null || return 1
  pfr_mktemp tmp
  if pct pull "$1" "$2" "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    cp "$tmp" "$3"
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# pfr_install_host_command CTID - install the host command
# (/usr/local/sbin/pve-flr-portal, plus its -config/-update links) from the
# container's own checkout, so it is the release that was just installed.
# Returns 1, creating nothing, if that release doesn't have it yet.
pfr_install_host_command() {
  local sbin="${PFR_SBIN:-/usr/local/sbin}" tmp
  pfr_mktemp tmp
  if ! pfr_pull "$1" "$PFR_APP_DIR/deploy/host/pve-flr-portal" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  install -m 755 "$tmp" "$sbin/pve-flr-portal.new"
  mv -f "$sbin/pve-flr-portal.new" "$sbin/pve-flr-portal"
  ln -sf pve-flr-portal "$sbin/pve-flr-portal-config"
  ln -sf pve-flr-portal "$sbin/pve-flr-portal-update"
  rm -f "$tmp"
}

# pfr_ct_address CTID -> "<network> <address>" of the container's IPv4
# address on eth0 (e.g. "192.168.1.0/24 192.168.1.50"), empty if none.
pfr_ct_address() {
  local addr
  addr=$(pct exec "$1" -- sh -c "ip -4 -o addr show dev eth0 | awk '{print \$4}' | head -1" 2>/dev/null || true)
  if [ -n "$addr" ]; then
    pct exec "$1" -- python3 -c 'import ipaddress, sys; i = ipaddress.ip_interface(sys.argv[1]); print(i.network, i.ip)' "$addr" 2>/dev/null || true
  fi
}

# pfr_resolve_dnt CTID - Direct Network Transfer: where guests reach this
# portal. Unless a subnet/address was given, use the container's own (only
# readable now that it's up). Sets RESTORE_DATA_NICS_JSON, or turns the
# feature off if the address can't be read.
pfr_resolve_dnt() {
  local detected
  if [ "$ENABLE_DNT" != "yes" ] || [ -n "${RESTORE_DATA_NICS_JSON:-}" ]; then
    return 0
  fi
  if [ -z "$DNT_CIDR" ] || [ -z "$DNT_LOCAL_IP" ]; then
    detected="$(pfr_ct_address "$1")"
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

# pfr_collect_env_overrides - fill ENV_OVERRIDES with the KEY=VALUE lines to
# write. Install: whatever differs from the app's own default, plus the two
# settings the portal can't work without. Config: only what changed.
pfr_collect_env_overrides() {
  local k env
  ENV_OVERRIDES=()
  for k in "${ALL_KEYS[@]}"; do
    env="${S_ENV[$k]}"
    if [ -z "$env" ]; then
      continue
    fi
    if [ "$PFR_MODE" = "config" ]; then
      if [ "${!k}" != "${ORIG[$k]-}" ]; then
        ENV_OVERRIDES+=("${env}=${!k}")
      fi
    else
      if ! cond_ok "$k"; then
        continue
      fi
      if [ "${!k}" != "${S_DEF[$k]}" ] || [ "$k" = "PVE_HOST" ] || [ "$k" = "PVE_STORAGE" ]; then
        ENV_OVERRIDES+=("${env}=${!k}")
      fi
    fi
  done
}

# pfr_write_env CTID - patch ENV_OVERRIDES into the container's .env. Pushed
# as a KEY=VALUE file and applied with a small Python script (a guaranteed
# dependency of the app) rather than sed, so JSON values like
# RESTORE_DATA_NICS need no shell/sed delimiter escaping. Existing keys are
# replaced in place (comments and unrelated settings untouched); keys not in
# the file yet are appended.
pfr_write_env() {
  local tmp
  if [ "${#ENV_OVERRIDES[@]}" -eq 0 ]; then
    return 0
  fi
  pfr_mktemp tmp
  printf '%s\n' "${ENV_OVERRIDES[@]}" > "$tmp"
  pct push "$1" "$tmp" "$PFR_OVERRIDES_TMP"
  rm -f "$tmp"
  pct exec "$1" -- python3 -c '
import re, sys
overrides = {}
with open(sys.argv[1]) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        overrides[k] = v
path = sys.argv[2]
with open(path) as f:
    lines = f.readlines()
out = []
seen = set()
for line in lines:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
    if m and m.group(1) in overrides:
        out.append(f"{m.group(1)}={overrides[m.group(1)]}\n")
        seen.add(m.group(1))
    else:
        out.append(line)
for k, v in overrides.items():
    if k not in seen:
        out.append(f"{k}={v}\n")
with open(path, "w") as f:
    f.writelines(out)
' "$PFR_OVERRIDES_TMP" "$PFR_ENV_FILE"
  pct exec "$1" -- rm -f "$PFR_OVERRIDES_TMP"
}

# pfr_load_env FILE - set the .env-backed settings from a container's .env
# (everything else keeps the value it already has). RESTORE_DATA_NICS is
# mapped onto the Direct Network Transfer settings; a list this tool can't
# represent (several networks) reads as "custom" and is left alone.
pfr_load_env() {
  local line key val k nics=""
  local -A rev=()
  for k in "${ALL_KEYS[@]}"; do
    if [ -n "${S_ENV[$k]}" ]; then
      rev[${S_ENV[$k]}]="$k"
    fi
  done
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ""|"#"*) continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    if [[ "$val" == \"*\" && ${#val} -ge 2 ]] || [[ "$val" == \'*\' && ${#val} -ge 2 ]]; then
      val="${val:1:${#val}-2}"
    fi
    if [ "$key" = "RESTORE_DATA_NICS" ]; then
      nics="$val"
    elif [ -n "${rev[$key]:-}" ]; then
      printf -v "${rev[$key]}" '%s' "$val"
    fi
  done < "$1"
  PFR_ORIG_NICS="$nics"
  pfr_map_dnt "$nics"
}

# pfr_map_dnt JSON - RESTORE_DATA_NICS -> ENABLE_DNT / DNT_*.
pfr_map_dnt() {
  local out
  RESTORE_DATA_NICS_JSON=""
  DNT_CIDR=""; DNT_LOCAL_IP=""; DNT_HOSTNAME=""
  if [ -z "$1" ]; then
    ENABLE_DNT="no"
    return 0
  fi
  out=$(printf '%s' "$1" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("custom")
    sys.exit()
if not d:
    print("no")
elif len(d) == 1 and set(d[0]) <= {"cidr", "local_ip", "hostname"}:
    print("yes|%s|%s|%s" % (d[0].get("cidr", ""), d[0].get("local_ip", ""), d[0].get("hostname", "")))
else:
    print("custom")
' 2>/dev/null || echo custom)
  case "$out" in
    yes\|*)
      ENABLE_DNT="yes"
      IFS='|' read -r _ DNT_CIDR DNT_LOCAL_IP DNT_HOSTNAME <<< "$out"
      ;;
    no) ENABLE_DNT="no" ;;
    *) ENABLE_DNT="custom" ;;
  esac
}

# pfr_use_config_layout - the management tool shows only what can change
# after install: no container storage, resources, certificate or host-backup
# questions here.
pfr_use_config_layout() {
  PFR_MODE="config"
  TITLE="pve-flr-portal configuration"
  BASIC_KEYS=(CT_HOSTNAME BRIDGE VLAN_TAG IP IP6 DNS PVE_HOST PVE_STORAGE ENABLE_DNT)
  BASIC_LAYOUT=("H:Container" CT_HOSTNAME "H:Network" BRIDGE VLAN_TAG IP IP6 DNS "H:Portal" PVE_HOST PVE_STORAGE ENABLE_DNT)
  ADV_GROUPS=(portal access limits dnt)
  HUB_GO_BUTTON="Apply changes"
  HUB_TEXT="Highlight a setting and press Enter to change it. Changed settings are marked with *. Choose \"Apply changes\" when you're ready, or press Esc to quit."
  HUB_QUIT_TEXT="Quit without applying any changes?"
  WELCOME_TEXT="Welcome to the pve-flr-portal configurator.

This changes the settings of an installed portal: the portal's own settings and its container's network configuration.

Changed settings are marked with *. Nothing is applied until you choose \"Apply changes\", and you see what will change first.

Container resources and storage aren't changed here - use Proxmox for those."
}
