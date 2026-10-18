#!/usr/bin/env bash
# Updates an existing pve-flr-portal install (installed via install.sh /
# lxc-create.sh) to a specific released version, or to the latest
# release. Run as root from inside the target container/host:
#
#   bash deploy/container/update.sh                # latest released version
#   bash deploy/container/update.sh v1.4.0         # a specific tagged release
#   bash deploy/container/update.sh 1.4.0          # "v" prefix is optional
#   bash deploy/container/update.sh main           # bleeding-edge, unreleased work
#
# Unlike `git pull` on main, this pins to a tagged GitHub Release by
# default (see docs/dev/versioning.md) so an update lands on a version
# that actually shipped and passed CI, not whatever commit happened to
# be on main at the time.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_USER="pveflr"
SERVICE_NAME="pve-flr-portal"
TARGET="${1:-latest}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root (chown and the systemd unit need it)." >&2
  exit 1
fi

cd "$APP_DIR"

# Same "dubious ownership" issue install.sh works around - harmless to
# repeat here since --system + --add is idempotent.
git config --system --add safe.directory "$APP_DIR"

if [ -n "$(git status --porcelain)" ]; then
  echo "Refusing to update: $APP_DIR has local/uncommitted changes." >&2
  echo "Check 'git status' and commit, stash, or discard them first." >&2
  exit 1
fi

echo "==> Fetching tags and branches"
git fetch --quiet --tags origin
git fetch --quiet origin main

if [ "$TARGET" = "main" ]; then
  echo "==> Switching to main (unreleased/bleeding-edge)"
  git checkout --quiet main
  git merge --quiet --ff-only origin/main
elif [ "$TARGET" = "latest" ]; then
  TAG=$(git tag -l 'v*.*.*' --sort=-v:refname | head -1)
  if [ -z "$TAG" ]; then
    echo "No vX.Y.Z release tags found - has a release ever been cut?" >&2
    exit 1
  fi
  echo "==> Checking out latest release: $TAG"
  git checkout --quiet "$TAG"
else
  TAG="$TARGET"
  case "$TAG" in
    v*) ;;
    *) TAG="v$TAG" ;;
  esac
  if ! git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
    echo "No such release tag: $TAG" >&2
    echo "Available: $(git tag -l 'v*.*.*' --sort=-v:refname | tr '\n' ' ')" >&2
    exit 1
  fi
  echo "==> Checking out release: $TAG"
  git checkout --quiet "$TAG"
fi

echo "==> Installing dependencies"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Re-applies install.sh's root-owned-app-dir/certs-only layout,
# migrating an existing install. Note: an install from before this layout needed
# this script run twice - the checkout above updates the file on disk,
# but the process already running this update is still the old version
# that doesn't do any of this. Or just run install.sh once instead.
chown -R root:root "$APP_DIR"
mkdir -p "$APP_DIR/certs"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/certs"
chmod 750 "$APP_DIR/certs"
chown root:"$APP_USER" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

check_owner() { [ "$(stat -c '%U:%G' "$1")" = "$2" ] || { echo "Expected $1 to be owned by $2, got $(stat -c '%U:%G' "$1")" >&2; exit 1; }; }
check_mode() { [ "$(stat -c '%a' "$1")" = "$2" ] || { echo "Expected $1 to be mode $2, got $(stat -c '%a' "$1")" >&2; exit 1; }; }
check_owner "$APP_DIR/requirements.txt" "root:root"
check_owner "$APP_DIR/certs" "$APP_USER:$APP_USER"
check_mode "$APP_DIR/certs" "750"
check_owner "$APP_DIR/.env" "root:$APP_USER"
check_mode "$APP_DIR/.env" "640"

echo "==> Refreshing systemd unit"
# Previously only install.sh wrote this file, so a unit-template change
# (like narrowing ReadWritePaths) could leave an update's unit stale.
sed "s#__APP_DIR__#${APP_DIR}#g; s#__APP_USER__#${APP_USER}#g" \
  "$APP_DIR/deploy/container/pve-flr-portal.service.template" > "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

echo "==> Restarting $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "==> Updated. Now running: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo "unknown")"
systemctl --no-pager status "$SERVICE_NAME" || true
