#!/usr/bin/env bash
# Removes the build tools (gcc, g++, make, ...) that installs before 2.0 pulled
# in by accident, and the system pip nothing uses. Run as root inside the
# container; `pve-flr-portal slim` / `pve-flr-portal update` do it from the host
# after asking.
#
#   bash deploy/container/slim-packages.sh check   # show what would be removed
#   bash deploy/container/slim-packages.sh apply   # remove it
#
# check exits 0 with the plan on stdout, 3 when there is nothing to remove, and
# 1 (with the reason on stderr) when removing would take away something the
# portal needs - in which case nothing is changed. apply re-runs the same
# check first.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

# The packages that brought the toolchain in (python3-pip recommends the
# first two) and the pip nothing uses: the portal runs from its own venv.
ROOTS=(build-essential python3-dev python3-pip)

# Never part of a removal: the interpreter and venv the portal runs on, git,
# TLS roots, certbot, and the base system. If a plan includes any of these,
# something other than the toolchain is involved and nothing is removed.
PROTECTED='^(python3|python3-minimal|python3-venv|python3-distutils|python3\.[0-9]+(-minimal|-venv)?|libpython3\.[0-9]+(-minimal|-stdlib)?|git|git-man|ca-certificates|openssl|certbot|python3-certbot.*|apt|dpkg|libc6|bash|coreutils|systemd.*|iproute2|sudo|openssh-server)$'

die() {
  echo "$*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "Must be run as root."

installed_roots() {
  local pkg
  for pkg in "${ROOTS[@]}"; do
    if [ "$(dpkg-query -W -f='${db:Status-Abbrev}' "$pkg" 2>/dev/null | tr -d ' ')" = "ii" ]; then
      echo "$pkg"
    fi
  done
}

# plan - fills PLAN (package names) and FREED (apt's own estimate), or exits.
plan() {
  local roots out
  roots="$(installed_roots)"
  [ -n "$roots" ] || exit 3
  # shellcheck disable=SC2086 # one argument per package
  out="$(apt-get -s purge --auto-remove $roots 2>/dev/null)" || die "apt could not work out what to remove."
  PLAN="$(printf '%s\n' "$out" | sed -n 's/^Remv \([^ ]*\).*/\1/p')"
  FREED="$(printf '%s\n' "$out" | sed -n 's/.*After this operation, \(.*\) disk space will be freed.*/\1/p')"
  [ -n "$PLAN" ] || exit 3
  local bad
  bad="$(printf '%s\n' "$PLAN" | grep -E "$PROTECTED" || true)"
  if [ -n "$bad" ]; then
    die "Not removing anything: the plan would also remove $(printf '%s' "$bad" | tr '\n' ' ')- that is not just build tools."
  fi
}

case "${1:-}" in
  check)
    plan
    echo "$(printf '%s\n' "$PLAN" | wc -l | tr -d ' ') packages${FREED:+, about $FREED}:"
    printf '%s\n' "$PLAN" | paste -sd' ' | fold -s -w 76 | sed 's/^/  /'
    ;;
  apply)
    plan
    # shellcheck disable=SC2086 # one argument per package
    apt-get purge -y -qq --auto-remove $(installed_roots)
    apt-get clean
    # the portal runs from its own venv; make sure it still can
    if [ -x "$APP_DIR/.venv/bin/python" ] && ! "$APP_DIR/.venv/bin/python" -c 'import fastapi, uvicorn' 2>/dev/null; then
      die "Removed the packages, but the portal's Python environment no longer imports - run: bash $APP_DIR/deploy/container/install.sh"
    fi
    echo "Removed $(printf '%s\n' "$PLAN" | wc -l | tr -d ' ') packages."
    ;;
  *)
    echo "Usage: bash deploy/container/slim-packages.sh check|apply" >&2
    exit 2
    ;;
esac
