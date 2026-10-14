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
# Issue #52: certbot + a DNS-01 plugin, so an admin who wants a real
# (CA-issued) cert can run deploy/certbot-setup.sh once instead of
# hand-writing a renewal hook. Ready to go by default but skippable
# (INSTALL_CERTBOT=0) since the self-signed cert tls.py generates works
# fine without it. rfc2136 is the default plugin - DNS-01 is the right
# challenge type for an internal deployment with no port 80/443 exposed
# to the internet for http-01, and rfc2136 covers any DNS server that
# speaks RFC 2136 dynamic updates (most self-hosted DNS, many providers'
# BIND frontends) without committing to one commercial DNS API.
INSTALL_CERTBOT="${INSTALL_CERTBOT:-1}"
CERTBOT_DNS_PLUGIN="${CERTBOT_DNS_PLUGIN:-rfc2136}"

# Git's "dubious ownership" safety check (CVE-2022-24765) rejects git
# commands against a repo it doesn't consider safely owned - confirmed
# live 2026-09-01 under an unprivileged LXC container, where a `pct exec`
# root shell's git still tripped this against a root-owned clone. --system
# (not --global) so this holds regardless of which user runs git here -
# root today, but also $APP_USER after the chown below changes this
# directory's actual owner, and either way every future `deploy/update.sh`
# run (issue #89) would otherwise trip the exact same error on.
git config --system --add safe.directory "$APP_DIR"

echo "==> Installing OS packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

if [ "$INSTALL_CERTBOT" = "1" ]; then
  echo "==> Installing certbot + dns-${CERTBOT_DNS_PLUGIN} plugin"
  apt-get install -y -qq certbot
  # Separate call: a missing/renamed plugin package (Debian's certbot
  # DNS plugins occasionally don't exist for every provider on every
  # release) should be a warning, not abort the whole install over an
  # optional feature.
  apt-get install -y -qq "python3-certbot-dns-${CERTBOT_DNS_PLUGIN}" || \
    echo "    Warning: python3-certbot-dns-${CERTBOT_DNS_PLUGIN} not available - certbot installed, but deploy/certbot-setup.sh needs this plugin package too."
fi

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
echo
echo "To update later, run: bash $APP_DIR/deploy/update.sh"
