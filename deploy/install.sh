#!/usr/bin/env bash
# Installs pve-flr-portal on a plain Debian 12 (or compatible) host/LXC
# that already has this repo checked out. Run as root from inside the
# target container/host:
#
#   bash deploy/install.sh
#
# lxc-create.sh calls this automatically after creating the container;
# run it by hand if you provisioned the container/machine yourself.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="pveflr"
SERVICE_NAME="pve-flr-portal"

# Git's "dubious ownership" safety check (CVE-2022-24765) rejects git
# commands against a repo it doesn't consider safely owned - confirmed
# live 2026-09-01 under an unprivileged LXC container, where a `pct exec`
# root shell's git still tripped this against a root-owned clone. --system
# (not --global) so this holds regardless of which user runs git here -
# root today, but also $APP_USER after the chown below changes this
# directory's actual owner, and either way for every future `git pull`
# a redeploy (`cd $APP_DIR && git pull && systemctl restart
# pve-flr-portal`) would otherwise trip the exact same error on.
git config --system --add safe.directory "$APP_DIR"

echo "==> Installing OS packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "==> Creating service user $APP_USER"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Creating virtualenv and installing dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "==> Creating .env from .env.example - EDIT THIS before it'll work"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Installing systemd unit"
# The unit's StateDirectory=pve-flr-portal makes systemd create + own
# /var/lib/pve-flr-portal (PFR_DATA_DIR, issue #30) on first start - no
# mkdir/chown needed here, and `systemctl enable --now` below triggers it.
sed "s#__APP_DIR__#${APP_DIR}#g; s#__APP_USER__#${APP_USER}#g" \
  "$APP_DIR/deploy/pve-flr-portal.service.template" > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "==> Installed. Service status:"
systemctl --no-pager status "$SERVICE_NAME" || true
echo
echo "Edit $APP_DIR/.env (PVE_HOST, PVE_STORAGE) then:"
echo "  systemctl restart $SERVICE_NAME"
