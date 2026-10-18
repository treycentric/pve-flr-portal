#!/usr/bin/env bash
# Manage the Proxmox roles the portal's users need. Run as root on a PVE node
# (roles are cluster-wide); normally started through `pve-flr-portal roles`,
# and by the installer for the roles chosen there.
#
#   pfr-roles.sh status
#   pfr-roles.sh create [NAME... | --all] [--unrestricted] [-y]
#   pfr-roles.sh delete [NAME... | --all] [--remove-acls] [-y]
#
# Creating a role grants nothing - it only defines the privileges. Giving a
# user access is still the per-user `pveum acl modify` step in the README.
#
# The roles (see the README's "Provisioning Access" for why each privilege):
#   FileRestoreReader      browse and download backups
#   FileRestoreReaderHost  browse host backups (Datastore.Allocate is full admin
#                          on that storage - see the README before using it)
#   FileRestoreOperator    restore into running guests (PVE 9 or newer)
PFR_ROLES_SCRIPT=1
set -euo pipefail

ROLE_ORDER=(FileRestoreReader FileRestoreOperator FileRestoreReaderHost)
declare -A ROLE_PRIVS=(
  [FileRestoreReader]="Datastore.AllocateSpace,VM.Backup,VM.Audit"
  [FileRestoreOperator]="VM.GuestAgent.Audit,VM.GuestAgent.FileWrite"
  [FileRestoreReaderHost]="Datastore.Allocate,Datastore.Audit"
)
# Added to FileRestoreOperator only on request: the privilege that lets the
# portal run commands inside a guest, a much larger grant.
UNRESTRICTED_PRIV="VM.GuestAgent.Unrestricted"

ASSUME_YES=0
UNRESTRICTED=0
REMOVE_ACLS=0
NAMES=()
ALL=0

die() {
  echo "pfr-roles: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: pfr-roles.sh <command> [NAME... | --all] [options]

Commands:
  status                    Show each role: missing, ok, or different from the
                            documented privileges, and how many grants use it.
  create [NAME...|--all]    Create the roles (existing ones are left alone; one
                            with different privileges is shown and only changed
                            after you confirm, or with -y).
  delete [NAME...|--all]    Delete roles - refused while grants still use them,
                            unless --remove-acls.

Roles: FileRestoreReader, FileRestoreOperator (PVE 9+), FileRestoreReaderHost.

Options:
  --unrestricted   FileRestoreOperator also gets VM.GuestAgent.Unrestricted.
  --remove-acls    With delete: remove the grants that use the role first.
  -y, --yes        Don't ask (needed without a terminal to change or delete).
EOF
}

have_tty() {
  [ -t 0 ] && [ -t 1 ]
}

# ask TEXT - [y/N]; without a terminal the answer is no unless -y was given.
ask() {
  local answer
  if [ "$ASSUME_YES" = "1" ]; then
    return 0
  fi
  have_tty || return 1
  read -rp "$1 [y/N] " answer || answer="n"
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
  esac
  return 1
}

# --- What PVE has ------------------------------------------------------------

# pve_major -> the PVE major version (0 if it can't be told).
pve_major() {
  local v
  v=$(pveversion 2>/dev/null | sed -n 's|^pve-manager/\([0-9][0-9]*\).*|\1|p' | head -1 || true)
  echo "${v:-0}"
}

# supported NAME - can this PVE have the role? (The guest-agent privileges
# are PVE 9's; PVE 8 only has the coarse VM.Monitor.)
supported() {
  if [ "$1" = "FileRestoreOperator" ] && [ "$(pve_major)" -lt 9 ]; then
    return 1
  fi
  return 0
}

# wanted_privs NAME -> the comma-separated privileges it should have.
wanted_privs() {
  local p="${ROLE_PRIVS[$1]}"
  if [ "$1" = "FileRestoreOperator" ] && [ "$UNRESTRICTED" = "1" ]; then
    p="$p,$UNRESTRICTED_PRIV"
  fi
  echo "$p"
}

# current_privs NAME -> its privileges, sorted and comma-separated; empty and
# status 1 if the role doesn't exist.
current_privs() {
  pveum role list --output-format json 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
for role in json.load(sys.stdin):
    if role.get("roleid") == name:
        print(",".join(sorted(p for p in (role.get("privs") or "").split(",") if p)))
        sys.exit(0)
sys.exit(1)
' "$1" | tr -d '\r'
}

sorted() {
  printf '%s' "$1" | tr ',' '\n' | sort | paste -sd, -
}

# grants NAME -> "path<TAB>type<TAB>ugid" for each ACL entry using the role.
grants() {
  pveum acl list --output-format json 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
for acl in json.load(sys.stdin):
    if acl.get("roleid") == name:
        print("%s\t%s\t%s" % (acl.get("path", ""), acl.get("type", "user"), acl.get("ugid", "")))
' "$1" | tr -d '\r'
}

# --- Commands -----------------------------------------------------------------

cmd_status() {
  local name current want state n
  printf '%-24s %-10s %-7s %s\n' "ROLE" "STATE" "GRANTS" "PRIVILEGES"
  for name in "${ROLE_ORDER[@]}"; do
    if ! supported "$name"; then
      printf '%-24s %-10s %-7s %s\n' "$name" "n/a" "-" "needs PVE 9 or newer"
      continue
    fi
    want="$(sorted "$(wanted_privs "$name")")"
    if current="$(current_privs "$name")"; then
      n=$(grants "$name" | grep -c . || true)
      state="ok"
      # The Unrestricted privilege is optional, so a role with it is still ok.
      if [ "$current" != "$want" ] && [ "$current" != "$(sorted "$want,$UNRESTRICTED_PRIV")" ]; then
        state="differs"
      fi
      printf '%-24s %-10s %-7s %s\n' "$name" "$state" "$n" "$current"
    else
      printf '%-24s %-10s %-7s %s\n' "$name" "missing" "-" "(would have: $want)"
    fi
  done
}

# create_one NAME -> 0 in the wanted state, 1 not (declined / unsupported).
create_one() {
  local name="$1" want current
  if ! supported "$name"; then
    echo "Skipping $name: it needs PVE 9 or newer (the guest-agent privileges don't exist on this version)."
    return 0
  fi
  want="$(wanted_privs "$name")"
  if current="$(current_privs "$name")"; then
    if [ "$current" = "$(sorted "$want")" ] \
      || { [ "$name" = "FileRestoreOperator" ] && [ "$UNRESTRICTED" != "1" ] \
           && [ "$current" = "$(sorted "$want,$UNRESTRICTED_PRIV")" ]; }; then
      echo "$name already exists."
      return 0
    fi
    echo "$name exists with different privileges:"
    echo "  has:    $current"
    echo "  wanted: $(sorted "$want")"
    if ask "Change $name to the wanted privileges?"; then
      pveum role modify "$name" -privs "$want" || { echo "Could not change $name." >&2; return 1; }
      echo "Changed $name."
      return 0
    fi
    echo "Left $name as it is." >&2
    return 1
  fi
  pveum role add "$name" -privs "$want" || { echo "Could not create $name." >&2; return 1; }
  echo "Created $name ($want)."
  if [ "$name" = "FileRestoreReaderHost" ]; then
    echo "  Note: Datastore.Allocate is full admin rights on a storage. Grant it only on a"
    echo "  storage dedicated to host backups (see the README)."
  fi
}

cmd_create() {
  local name rc=0
  for name in "${NAMES[@]}"; do
    create_one "$name" || rc=1
  done
  echo
  echo "Creating a role grants nothing; grant it to users with pveum acl modify (see the README)."
  return "$rc"
}

# delete_one NAME -> 0 gone (or never there), 1 refused / declined.
delete_one() {
  local name="$1" list line path type ugid flag
  if ! current_privs "$name" >/dev/null; then
    echo "$name doesn't exist."
    return 0
  fi
  list="$(grants "$name")"
  if [ -n "$list" ]; then
    echo "$name is used by these grants:"
    printf '%s\n' "$list" | awk -F'\t' '{printf "  %s  %s %s\n", $1, $2, $3}'
    if [ "$REMOVE_ACLS" != "1" ]; then
      echo "Not deleting $name. Remove the grants first, or pass --remove-acls." >&2
      return 1
    fi
    ask "Remove those grants and delete $name?" || { echo "Left $name as it is." >&2; return 1; }
    while IFS=$'\t' read -r path type ugid; do
      [ -n "$path" ] || continue
      case "$type" in
        group) flag="--groups" ;;
        token) flag="--tokens" ;;
        *) flag="--users" ;;
      esac
      pveum acl delete "$path" --roles "$name" "$flag" "$ugid" \
        || { echo "Could not remove the grant of $name to $ugid on $path - $name is not deleted." >&2; return 1; }
      echo "Removed the grant of $name to $ugid on $path."
    done <<< "$list"
  else
    ask "Delete the role $name?" || { echo "Left $name as it is." >&2; return 1; }
  fi
  pveum role delete "$name" || { echo "Could not delete $name." >&2; return 1; }
  echo "Deleted $name."
}

cmd_delete() {
  local name rc=0
  for name in "${NAMES[@]}"; do
    delete_one "$name" || rc=1
  done
  return "$rc"
}

# --- Arguments ---------------------------------------------------------------

main() {
  local cmd="${1:-}" arg name
  case "$cmd" in
    status|create|delete) shift ;;
    ""|-h|--help|help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  for arg in "$@"; do
    case "$arg" in
      --all) ALL=1 ;;
      --unrestricted) UNRESTRICTED=1 ;;
      --remove-acls) REMOVE_ACLS=1 ;;
      -y|--yes) ASSUME_YES=1 ;;
      -h|--help) usage; exit 0 ;;
      -*) die "Unknown option $arg (see --help)." ;;
      *)
        [ -n "${ROLE_PRIVS[$arg]:-}" ] || die "Unknown role '$arg' (roles: ${ROLE_ORDER[*]})."
        NAMES+=("$arg")
        ;;
    esac
  done
  command -v pveum >/dev/null 2>&1 || die "pveum not found - run this on a PVE node."
  if [ "$ALL" = "1" ]; then
    NAMES=("${ROLE_ORDER[@]}")
  fi
  case "$cmd" in
    status) cmd_status ;;
    create)
      [ "${#NAMES[@]}" -gt 0 ] || die "Say which roles (names, or --all) - see --help."
      cmd_create
      ;;
    delete)
      [ "${#NAMES[@]}" -gt 0 ] || die "Say which roles (names, or --all) - see --help."
      cmd_delete
      ;;
  esac
}

# Run unless sourced (the tests source this file to exercise the functions).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
