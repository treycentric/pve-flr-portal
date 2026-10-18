#!/usr/bin/env bash
# Management tool for one installed pve-flr-portal container, run on the PVE
# host by the `pve-flr-portal` command (which copies this file and
# deploy/lib/settings.sh out of the container's own checkout, so the screens
# always match the release that is installed). Run as root on the node that
# hosts the container.
#
#   pfr-manage.sh config CTID    change the portal's settings and the
#                                container's network configuration
#
# `config` opens the same review screen the installer ends with, filled in
# from the container's current configuration. Settings that were changed are
# marked with *. "Apply changes" shows what will change, applies the network
# changes first (restarting the container, and restoring the previous network
# settings if it is not reachable afterwards), then writes the portal's
# .env and restarts the service.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/settings.sh
source "$HERE/lib/settings.sh"

PFR_CANCEL_RC=0

die() {
  msg_err "$*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: pfr-manage.sh config CTID

Change the settings of the pve-flr-portal container CTID: the portal's own
settings (PVE host, PBS storage, Direct Network Transfer, limits, ...) and
the container's network configuration (bridge, VLAN, IPv4, IPv6, DNS,
hostname). Normally started through `pve-flr-portal config`.
EOF
}

# --- Reading the container's current configuration ------------------------

CT_STATUS=""
OLD_NET0=""
OLD_ADDR=""

# load_current CTID - fill every setting from the container: its Proxmox
# configuration (hostname, network, DNS), the IPv6-off marker, and the
# portal's .env.
load_current() {
  local ctid="$1" cfg k tmp
  cfg=$(pct config "$ctid" 2>/dev/null) || die "Container $ctid was not found on this node."
  CT_STATUS=$(pct status "$ctid" | awk '{print $2}')
  if [ "$CT_STATUS" != "running" ]; then
    if ui_ask_yes "Container $ctid is not running. Start it so its settings can be read and changed?"; then
      msg_info "Starting container $ctid"
      pct start "$ctid"
      sleep 5
    else
      pfr_cancel
    fi
  fi
  pct exec "$ctid" -- test -f "$PFR_ENV_FILE" || die "Container $ctid has no pve-flr-portal installation at $PFR_APP_DIR."

  for k in "${ALL_KEYS[@]}"; do
    printf -v "$k" '%s' "${S_DEF[$k]}"
  done
  CT_HOSTNAME=$(sed -n 's/^hostname: *//p' <<< "$cfg")
  OLD_NET0=$(sed -n 's/^net0: *//p' <<< "$cfg")
  if [ -z "$OLD_NET0" ]; then
    die "Container $ctid has no net0 network interface."
  fi
  pfr_parse_net0 "$OLD_NET0"
  DNS_SERVERS=$(sed -n 's/^nameserver: *//p' <<< "$cfg")
  SEARCH_DOMAIN=$(sed -n 's/^searchdomain: *//p' <<< "$cfg")
  if [ "$IPV6_MODE" = "none" ] && pct exec "$ctid" -- test -f "$PFR_IPV6_SYSCTL" 2>/dev/null; then
    IPV6_MODE="disabled"
  fi

  tmp=$(mktemp)
  pfr_pull "$ctid" "$PFR_ENV_FILE" "$tmp" || die "Could not read $PFR_ENV_FILE from container $ctid."
  pfr_load_env "$tmp"
  rm -f "$tmp"

  OLD_ADDR="$(pfr_ct_address "$ctid")"
  pfr_snapshot
}

# --- What will change ---------------------------------------------------

# pending_summary - one "- Label: old -> new" line per changed setting.
pending_summary() {
  local k out=""
  for k in "${ALL_KEYS[@]}"; do
    if [ "${!k}" != "${ORIG[$k]-}" ]; then
      out="${out}- ${S_LABEL[$k]}: $(show_val "$k" "${ORIG[$k]-}") -> $(show_val "$k")\n"
    fi
  done
  printf '%b' "$out"
}

# --- Applying network changes -------------------------------------------

# normalise_dns "1.1.1.1, 9.9.9.9" -> "1.1.1.1 9.9.9.9"
normalise_dns() {
  printf '%s' "$1" | tr -s ', ' ' ' | sed 's/^ //; s/ $//'
}

restart_ct() {
  msg_info "Restarting container $1"
  if ! pct reboot "$1" >/dev/null 2>&1; then
    pct stop "$1" >/dev/null 2>&1 || true
    pct start "$1"
  fi
}

# wait_network CTID IPV4_MODE GATEWAY - up to ~90 seconds for the container
# to come back with an address (and, for a static address, an answering
# gateway when it has `ping`). Returns 1 if it never does.
wait_network() {
  local ctid="$1" mode="$2" gw="$3" i addr=""
  for i in $(seq 1 "${PFR_WAIT_TRIES:-30}"); do
    if pct exec "$ctid" -- true >/dev/null 2>&1; then
      if [ "$mode" = "manual" ]; then
        return 0
      fi
      addr="$(pfr_ct_address "$ctid")"
      if [ -n "$addr" ]; then
        break
      fi
    fi
    sleep "${PFR_WAIT_SLEEP:-3}"
  done
  if [ -z "$addr" ]; then
    return 1
  fi
  if [ "$mode" = "yes" ] && [ -n "$gw" ] \
    && pct exec "$ctid" -- sh -c 'command -v ping >/dev/null 2>&1'; then
    for i in 1 2 3 4 5; do
      if pct exec "$ctid" -- ping -c1 -W2 "$gw" >/dev/null 2>&1; then
        return 0
      fi
      sleep "${PFR_WAIT_SLEEP:-3}"
    done
    return 1
  fi
  return 0
}

# net_set_args CTID NET0 HOSTNAME DNS SEARCH ORIG_DNS ORIG_SEARCH - the
# `pct set` arguments for the container's own configuration; DNS entries
# that become blank are deleted so the host's settings apply again.
net_set_args() {
  NET_ARGS=(--net0 "$2")
  if [ "$3" != "${ORIG[CT_HOSTNAME]}" ] || [ "$1" = "rollback" ]; then
    NET_ARGS+=(--hostname "$3")
  fi
  if [ -n "$4" ]; then
    NET_ARGS+=(--nameserver "$(normalise_dns "$4")")
  elif [ -n "$6" ]; then
    NET_ARGS+=(--delete nameserver)
  fi
  if [ -n "$5" ]; then
    NET_ARGS+=(--searchdomain "$5")
  elif [ -n "$7" ]; then
    NET_ARGS+=(--delete searchdomain)
  fi
}

rollback_network() {
  local ctid="$1" attempted
  msg_info "Restoring the previous network settings"
  NET_ARGS=()
  net_set_args rollback "$OLD_NET0" "${ORIG[CT_HOSTNAME]}" "${ORIG[DNS_SERVERS]}" "${ORIG[SEARCH_DOMAIN]}" "$DNS_SERVERS" "$SEARCH_DOMAIN"
  pct set "$ctid" "${NET_ARGS[@]}" || msg_err "Could not restore the previous network settings - check container $ctid by hand (it was: $OLD_NET0)."
  attempted="$IPV6_MODE"
  IPV6_MODE="${ORIG[IPV6_MODE]}"
  pfr_apply_ipv6_sysctl "$ctid" || true
  IPV6_MODE="$attempted"
  restart_ct "$ctid"
}

apply_network() {
  local ctid="$1" new_net0
  IP_CONFIG=""
  new_net0="$(pfr_build_net0 "$OLD_NET0")"
  NET_ARGS=()
  net_set_args apply "$new_net0" "$CT_HOSTNAME" "$DNS_SERVERS" "$SEARCH_DOMAIN" "${ORIG[DNS_SERVERS]}" "${ORIG[SEARCH_DOMAIN]}"
  msg_info "Applying the network settings"
  if ! pct set "$ctid" "${NET_ARGS[@]}"; then
    msg_err "Proxmox rejected the network settings - nothing was changed."
    return 1
  fi
  if [ "$IPV6_MODE" != "${ORIG[IPV6_MODE]}" ]; then
    pfr_apply_ipv6_sysctl "$ctid"
  fi
  restart_ct "$ctid"
  if wait_network "$ctid" "$USE_STATIC_IP" "$STATIC_GW"; then
    msg_ok "The container is back on the network"
    return 0
  fi
  msg_err "The container could not be reached with the new network settings."
  rollback_network "$ctid"
  if wait_network "$ctid" "${ORIG[USE_STATIC_IP]}" "${ORIG[STATIC_GW]}"; then
    msg_ok "The previous network settings are back in place"
  else
    msg_err "The container still isn't reachable - check it in the Proxmox console."
  fi
  return 1
}

# --- Applying the portal's .env changes ---------------------------------

# dnt_touched - did anything that feeds RESTORE_DATA_NICS change? (So a
# hand-formatted value is never rewritten just because another setting was.)
dnt_touched() {
  local k
  if pfr_network_changed; then
    return 0
  fi
  for k in ENABLE_DNT DNT_CIDR DNT_LOCAL_IP DNT_HOSTNAME; do
    if [ "${!k}" != "${ORIG[$k]-}" ]; then
      return 0
    fi
  done
  return 1
}

apply_env() {
  local ctid="$1" before new_addr json=""
  # Direct Network Transfer is derived from the container's own address when
  # no subnet/address was entered by hand. If that address just changed,
  # re-detect it; an address set by hand is left alone (with a warning).
  if [ "$ENABLE_DNT" = "yes" ]; then
    if pfr_network_changed && [ "$DNT_CIDR $DNT_LOCAL_IP" = "$OLD_ADDR" ]; then
      DNT_CIDR=""
      DNT_LOCAL_IP=""
    fi
    RESTORE_DATA_NICS_JSON=""
    pfr_resolve_dnt "$ctid"
    json="$RESTORE_DATA_NICS_JSON"
    new_addr="$(pfr_ct_address "$ctid")"
    if [ -n "$DNT_LOCAL_IP" ] && [ -n "$new_addr" ] && [ "${new_addr#* }" != "$DNT_LOCAL_IP" ]; then
      msg_err "Direct Network Transfer uses ${DNT_LOCAL_IP}, which is not the container's address (${new_addr#* }); guests on that subnet will fall back to the slower path."
    fi
  elif [ "$ENABLE_DNT" = "no" ]; then
    json="[]"
  fi
  pfr_collect_env_overrides
  if [ -n "$json" ] && [ "$json" != "$PFR_ORIG_NICS" ] && dnt_touched; then
    ENV_OVERRIDES+=("RESTORE_DATA_NICS=${json}")
  fi
  if [ "${#ENV_OVERRIDES[@]}" -eq 0 ]; then
    return 0
  fi
  msg_info "Writing the portal's settings"
  pfr_write_env "$ctid"
  before=$(date +%s)
  pct exec "$ctid" -- systemctl restart pve-flr-portal
  for _ in $(seq 1 10); do
    if pct exec "$ctid" -- systemctl is-active --quiet pve-flr-portal; then
      msg_ok "The portal restarted ($(($(date +%s) - before))s)"
      return 0
    fi
    sleep "${PFR_WAIT_SLEEP:-2}"
  done
  msg_err "The portal did not come back after the restart - check: pct exec $ctid -- journalctl -u pve-flr-portal"
  return 1
}

# apply_changes CTID - 0 when changes were applied, 1 to go back to the
# review screen (nothing to apply, declined, or the network change failed).
apply_changes() {
  local ctid="$1" summary text net=0
  summary="$(pending_summary)"
  if [ -z "$summary" ]; then
    ui_msg "Nothing to apply - no settings were changed."
    return 1
  fi
  if pfr_network_changed; then
    net=1
  fi
  text="These changes will be applied to container ${ctid}:\n\n${summary}"
  if [ "$net" = "1" ]; then
    text="${text}\n\nChanging the container's network restarts it: the portal will be unavailable for a minute or so and everyone will be logged out. If the container can't be reached afterwards, the previous network settings are restored automatically."
  fi
  text="${text}\n\nApply these changes?"
  if ! ui_ask_yes "$text"; then
    return 1
  fi
  if [ "$net" = "1" ]; then
    if ! apply_network "$ctid"; then
      ui_msg "The network change did not work and was undone. Adjust the settings and try again, or choose Quit."
      return 1
    fi
  fi
  apply_env "$ctid" || true
  return 0
}

# --- Entry point --------------------------------------------------------

# set_welcome CTID - the welcome screen names the container being configured.
set_welcome() {
  WELCOME_TEXT="Welcome to the pve-flr-portal configurator.

This changes the settings of container ${1} (${CT_HOSTNAME}): the portal's own settings and the container's network configuration.

Changed settings are marked with *. Nothing is applied until you choose \"Apply changes\", and you see what will change first.

Container resources and storage aren't changed here - use Proxmox for those."
}

main() {
  local sub="${1:-}" ctid="${2:-}" ip
  case "$sub" in
    config) ;;
    ""|-h|--help|help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  if ! [[ "$ctid" =~ ^[0-9]+$ ]]; then
    usage >&2
    exit 2
  fi
  if [ "$(id -u)" -ne 0 ]; then
    die "This has to run as root on the PVE host."
  fi
  command -v pct >/dev/null 2>&1 || die "pct not found - run this on the PVE host."
  pfr_detect_ui
  if [ "$IS_TTY" != "1" ]; then
    die "Changing settings needs a terminal."
  fi
  load_current "$ctid"
  pfr_use_config_layout
  set_welcome "$ctid"
  show_welcome
  start_prefetch
  while :; do
    review_hub
    if apply_changes "$ctid"; then
      break
    fi
  done
  ip="$(pfr_ct_address "$ctid")"
  if [ -n "$ip" ]; then
    msg_ok "Done. The portal is at https://${ip#* }:${PORTAL_PORT}/"
  else
    msg_ok "Done."
  fi
}

# Run unless sourced (the tests source this file to exercise the functions).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
