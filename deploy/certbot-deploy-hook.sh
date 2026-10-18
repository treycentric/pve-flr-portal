#!/usr/bin/env bash
# Compatibility shim: certbot-deploy-hook.sh moved to deploy/container/certbot-deploy-hook.sh. This forwards there
# so existing instructions, scripts and certbot renewal hooks that use the old
# path keep working. Removed in the next major release.
exec bash "$(cd "$(dirname "$0")" && pwd)/container/certbot-deploy-hook.sh" "$@"
