#!/usr/bin/env bash
# certbot --deploy-hook / renew_hook for pve-flr-portal (issue #52).
# Installed automatically as the renew_hook by certbot-setup.sh - not
# meant to be run by hand except to test it (see below).
#
# certbot exports $RENEWED_LINEAGE (-> /etc/letsencrypt/live/<name>)
# when it calls a deploy hook. This copies the freshly-issued
# fullchain.pem/privkey.pem to wherever the portal's .env says its own
# cert/key live, and restarts the service so it picks them up (the app
# reads its cert only at startup - confirmed live during #47 testing).
#
# tls.py never overwrites or deletes an admin-supplied cert - only a
# broken *self-signed* one - so pointing it at a certbot-managed path
# is safe: certbot renews in place, this hook re-copies, tls.py just
# sees a valid file and leaves it alone.
#
# Test it manually with: RENEWED_LINEAGE=/etc/letsencrypt/live/<name> \
#   bash deploy/certbot-deploy-hook.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/pve-flr-portal}"
APP_USER="${APP_USER:-pveflr}"
ENV_FILE="$APP_DIR/.env"
SERVICE_NAME="pve-flr-portal"

if [ -z "${RENEWED_LINEAGE:-}" ]; then
  echo "RENEWED_LINEAGE not set - this script is meant to run as a certbot deploy hook." >&2
  exit 1
fi

_env_get() {
  local key="$1" default="$2"
  local val
  val=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)
  echo "${val:-$default}"
}

_resolve() {
  # Relative paths in .env are relative to APP_DIR (systemd's
  # WorkingDirectory=), same as how the app itself resolves them.
  local path="$1"
  case "$path" in
    /*) echo "$path" ;;
    *) echo "$APP_DIR/$path" ;;
  esac
}

_install_cert() {
  local cert_key="$1" key_key="$2" label="$3"
  local cert_path key_path
  cert_path="$(_resolve "$(_env_get "$cert_key" "certs/portal.crt")")"
  key_path="$(_resolve "$(_env_get "$key_key" "certs/portal.key")")"
  mkdir -p "$(dirname "$cert_path")" "$(dirname "$key_path")"
  install -o "$APP_USER" -g "$APP_USER" -m 0640 "$RENEWED_LINEAGE/fullchain.pem" "$cert_path"
  install -o "$APP_USER" -g "$APP_USER" -m 0640 "$RENEWED_LINEAGE/privkey.pem" "$key_path"
  echo "==> Installed $label cert -> $cert_path"
}

_install_cert "TLS_CERT_FILE" "TLS_KEY_FILE" "main UI"

# Data-plane cert (issue #47 §7.6.1) opts in separately - it needs a
# hostname SAN matching a RESTORE_DATA_NICS entry, which not every
# certbot-managed domain necessarily is.
if [ "$(_env_get "RESTORE_DATA_NIC_TLS_PREFERRED" "verify")" != "plaintext" ] \
  && [ "$(_env_get "PFR_ACME_DATA_PLANE" "0")" = "1" ]; then
  _install_cert "RESTORE_DATA_NIC_TLS_CERT_FILE" "RESTORE_DATA_NIC_TLS_KEY_FILE" "data-plane"
fi

systemctl restart "$SERVICE_NAME"
echo "==> Restarted $SERVICE_NAME"
