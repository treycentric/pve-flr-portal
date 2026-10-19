#!/usr/bin/env bash
# Compatibility shim: update.sh moved to deploy/container/update.sh. This forwards there
# so existing instructions, scripts and certbot renewal hooks that use the old
# path keep working. Removed in 3.0.
exec bash "$(cd "$(dirname "$0")" && pwd)/container/update.sh" "$@"
