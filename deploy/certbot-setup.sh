#!/usr/bin/env bash
# First-run helper (issue #52): issues a real (CA-issued) cert for
# pve-flr-portal via certbot's DNS-01 challenge, and wires up automatic
# renewal. Run ONCE, as root, inside the container/host running the
# portal:
#
#   bash deploy/certbot-setup.sh flr.example.com
#   bash deploy/certbot-setup.sh flr.example.com flr-data.example.com
#
# DNS-01 is used instead of http-01 because this app is often deployed
# internally, with no port 80/443 exposed to the internet for Let's
# Encrypt to reach - the challenge is proven by creating a DNS TXT
# record instead, which this host's DNS plugin does directly against
# the zone's nameserver.
#
# Prerequisites:
#   - certbot + python3-certbot-dns-<plugin> installed (install.sh does
#     this by default - INSTALL_CERTBOT=1, CERTBOT_DNS_PLUGIN=rfc2136).
#   - The plugin's credentials file in place and chmod 600 - see
#     deploy/rfc2136-credentials.ini.example for the rfc2136 default.
#
# Override via environment variables:
#   CERTBOT_DNS_PLUGIN         (default: rfc2136)
#   CERTBOT_CREDENTIALS_FILE   (default: /etc/letsencrypt/<plugin>-credentials.ini)
#   CERTBOT_PROPAGATION_SECONDS (default: 30)
#   CERTBOT_EMAIL              (default: unset -> --register-unsafely-without-email;
#                                set this if you want renewal-failure notices)
#   ACME_SERVER                (default: unset -> certbot's own default, Let's
#                                Encrypt's production directory. Point this at
#                                an internal ACME server instead - e.g. an
#                                acme2certifier instance fronting your own PKI
#                                - by setting it to that server's directory
#                                URL, e.g.
#                                https://acme.internal.example.com/directory.
#                                If that server's own TLS cert isn't from a
#                                publicly-trusted CA, its issuing CA needs to
#                                be trusted by this container first
#                                (update-ca-certificates) - certbot has no
#                                separate "skip TLS verification of the ACME
#                                server itself" flag.)
#   CERTBOT_KEY_TYPE           (default: unset -> certbot's own default,
#                                currently ecdsa on recent certbot versions.
#                                Set to "rsa" for a CA that doesn't support
#                                ECDSA yet - many internal ACME servers,
#                                including some acme2certifier setups.)
#   CERTBOT_RSA_KEY_SIZE       (default: unset -> certbot's own default
#                                (2048) when CERTBOT_KEY_TYPE=rsa. Ignored
#                                for ecdsa - use CERTBOT_ELLIPTIC_CURVE
#                                instead if that ever needs overriding.)
#   APP_DIR                    (default: /opt/pve-flr-portal)
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "Usage: bash deploy/certbot-setup.sh <domain> [more domains...]" >&2
  exit 1
fi

CERTBOT_DNS_PLUGIN="${CERTBOT_DNS_PLUGIN:-rfc2136}"
CERTBOT_CREDENTIALS_FILE="${CERTBOT_CREDENTIALS_FILE:-/etc/letsencrypt/${CERTBOT_DNS_PLUGIN}-credentials.ini}"
CERTBOT_PROPAGATION_SECONDS="${CERTBOT_PROPAGATION_SECONDS:-30}"
APP_DIR="${APP_DIR:-/opt/pve-flr-portal}"
CERT_NAME="$1"
HOOK="$APP_DIR/deploy/certbot-deploy-hook.sh"

if ! command -v certbot >/dev/null 2>&1; then
  echo "certbot is not installed - re-run deploy/install.sh with INSTALL_CERTBOT=1, or 'apt-get install certbot'." >&2
  exit 1
fi

if ! certbot plugins 2>/dev/null | grep -q "dns-${CERTBOT_DNS_PLUGIN}"; then
  echo "certbot's dns-${CERTBOT_DNS_PLUGIN} plugin isn't installed - 'apt-get install python3-certbot-dns-${CERTBOT_DNS_PLUGIN}'." >&2
  exit 1
fi

if [ ! -f "$CERTBOT_CREDENTIALS_FILE" ]; then
  echo "Credentials file not found: $CERTBOT_CREDENTIALS_FILE" >&2
  echo "See deploy/${CERTBOT_DNS_PLUGIN}-credentials.ini.example (if this plugin ships one) and copy/fill it in first." >&2
  exit 1
fi

PERMS=$(stat -c '%a' "$CERTBOT_CREDENTIALS_FILE")
if [ "$PERMS" != "600" ]; then
  echo "Warning: $CERTBOT_CREDENTIALS_FILE is mode $PERMS, not 600 - certbot's own DNS plugins refuse to run against a group/world-readable credentials file." >&2
fi

if [ ! -f "$HOOK" ]; then
  echo "Deploy hook not found: $HOOK" >&2
  exit 1
fi
if [ ! -x "$HOOK" ]; then
  # A file this project ships and fully controls - self-heal rather
  # than making the admin chmod it by hand (confirmed live: a fresh
  # git clone doesn't always carry the executable bit through).
  chmod +x "$HOOK"
fi

DOMAIN_ARGS=()
for d in "$@"; do
  DOMAIN_ARGS+=(-d "$d")
done

EMAIL_ARGS=(--register-unsafely-without-email)
if [ -n "${CERTBOT_EMAIL:-}" ]; then
  EMAIL_ARGS=(--email "$CERTBOT_EMAIL")
fi

SERVER_ARGS=()
if [ -n "${ACME_SERVER:-}" ]; then
  SERVER_ARGS=(--server "$ACME_SERVER")
fi

KEY_ARGS=()
if [ -n "${CERTBOT_KEY_TYPE:-}" ]; then
  case "$CERTBOT_KEY_TYPE" in
    rsa|ecdsa) ;;
    *)
      echo "CERTBOT_KEY_TYPE must be 'rsa' or 'ecdsa', got: $CERTBOT_KEY_TYPE" >&2
      exit 1
      ;;
  esac
  KEY_ARGS+=(--key-type "$CERTBOT_KEY_TYPE")
fi
if [ -n "${CERTBOT_RSA_KEY_SIZE:-}" ]; then
  KEY_ARGS+=(--rsa-key-size "$CERTBOT_RSA_KEY_SIZE")
fi

echo "==> Requesting a certificate for: $*"
certbot certonly --non-interactive --agree-tos "${EMAIL_ARGS[@]}" \
  --cert-name "$CERT_NAME" \
  --authenticator "dns-${CERTBOT_DNS_PLUGIN}" \
  "--dns-${CERTBOT_DNS_PLUGIN}-credentials" "$CERTBOT_CREDENTIALS_FILE" \
  "--dns-${CERTBOT_DNS_PLUGIN}-propagation-seconds" "$CERTBOT_PROPAGATION_SECONDS" \
  --deploy-hook "$HOOK" \
  "${SERVER_ARGS[@]}" \
  "${KEY_ARGS[@]}" \
  "${DOMAIN_ARGS[@]}"

# --deploy-hook is saved as the cert's renew_hook, so certbot.timer's
# twice-daily renew re-runs it automatically from here on. Run it once
# now too so the freshly-issued cert lands immediately rather than
# waiting for the next renewal cycle.
echo "==> Installing the freshly-issued cert now"
RENEWED_LINEAGE="/etc/letsencrypt/live/$CERT_NAME" "$HOOK"

echo
echo "==> Done. Certificate issued for: $*"
echo "    Renews automatically via certbot.timer; deploy/certbot-deploy-hook.sh reinstalls it each time."
