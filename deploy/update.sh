#!/usr/bin/env bash
# Updates an existing pve-flr-portal install (installed via install.sh /
# lxc-create.sh) to a specific released version, or to the latest
# release. Run as root from inside the target container/host:
#
#   bash deploy/update.sh                # latest released version
#   bash deploy/update.sh v1.4.0         # a specific tagged release
#   bash deploy/update.sh 1.4.0          # "v" prefix is optional
#   bash deploy/update.sh main           # bleeding-edge, unreleased work
#
# Unlike `git pull` on main, this pins to a tagged GitHub Release by
# default (see docs/dev/versioning.md) so an update lands on a version
# that actually shipped and passed CI, not whatever commit happened to
# be on main at the time. See issue #89.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="pveflr"
SERVICE_NAME="pve-flr-portal"
TARGET="${1:-latest}"

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

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Restarting $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "==> Updated. Now running: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo "unknown")"
systemctl --no-pager status "$SERVICE_NAME" || true
