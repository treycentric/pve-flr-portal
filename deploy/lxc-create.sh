#!/usr/bin/env bash
# Compatibility shim: lxc-create.sh moved to deploy/host/lxc-create.sh. This
# keeps the old curl one-liner and `bash deploy/lxc-create.sh` working: it runs
# the sibling host/lxc-create.sh from a checkout, else downloads that script
# (from PFR_VERSION, default main) and runs it. Removed in the next major
# release.
set -euo pipefail

here="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/host/lxc-create.sh" ]; then
  exec bash "$here/host/lxc-create.sh" "$@"
fi

ref="${PFR_VERSION:-main}"
slug="${REPO_URL:-https://github.com/treycentric/pve-flr-portal.git}"
slug="${slug#https://github.com/}"
slug="${slug%.git}"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if ! curl -fsSL "https://raw.githubusercontent.com/${slug}/${ref}/deploy/host/lxc-create.sh" -o "$tmp" \
  || ! grep -q '^PFR_LIB_API_WANTED=' "$tmp"; then
  echo "Could not download the installer (deploy/host/lxc-create.sh at ${ref}) from GitHub." >&2
  exit 1
fi
bash "$tmp" "$@"
