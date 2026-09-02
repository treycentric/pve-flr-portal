"""Entrypoint that serves the portal over HTTPS by default (docs/plan.md
§7.3). Cert bootstrap has to happen before uvicorn binds its SSL
context, which is too late to do from a FastAPI startup event - hence a
small script instead of `uvicorn backend.main:app` directly.

Design C (docs/plan.md §7.6, issue #22, step 5): when RESTORE_DATA_NICS
is configured, this also binds one additional plain-HTTP listener per
distinct configured data-NIC IP, for the network-pull download route
(`GET /api/restore-downloads/{token}`) alone. That listener has to run
in the *same process* as the main one, not a second `run.py` invocation
- `restore_download`'s token store and `restore_jobs.manager` are both
in-memory and process-local (CLAUDE.md - no extra services), so a
guest's fetch has to land in the same process that minted its token.
uvicorn's own `--reload` supervisor only wraps the single-server
`uvicorn.run()` entrypoint, not multiple `Server` instances sharing one
event loop, so the default (no data NICs configured - unchanged from
before this) keeps using `uvicorn.run(..., reload=True)` for the
familiar auto-reload dev loop; only the opt-in multi-listener path below
gives that up.
"""
import asyncio
from pathlib import Path

import uvicorn

from backend.config import settings
from backend.restore_network_pull import parse_data_nics
from backend.tls import ensure_self_signed_cert


async def _serve_with_data_nics(cert_path: Path, key_path: Path, data_nics) -> None:
    main_config = uvicorn.Config(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )
    servers = [uvicorn.Server(main_config)]

    # One data-plane listener per distinct configured IP, bound to that
    # specific interface only - never 0.0.0.0, which would defeat the
    # whole point of keeping the data plane separate from the
    # UI/PVE-management listener above. Deliberately plain HTTP, no TLS
    # - see restore_runner._try_design_c()'s docstring for why.
    data_port = settings.restore_data_nic_port or settings.port
    for ip in sorted({nic.local_ip for nic in data_nics}):
        data_config = uvicorn.Config("backend.main:app", host=ip, port=data_port)
        servers.append(uvicorn.Server(data_config))
        print(f"Design C: also serving the network-pull download route on http://{ip}:{data_port}")

    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    cert_path = Path(settings.tls_cert_file)
    key_path = Path(settings.tls_key_file)
    ensure_self_signed_cert(cert_path, key_path, common_name=settings.pve_host)

    data_nics = parse_data_nics(settings.restore_data_nics_json)
    if data_nics:
        asyncio.run(_serve_with_data_nics(cert_path, key_path, data_nics))
    else:
        uvicorn.run(
            "backend.main:app",
            host="0.0.0.0",
            port=settings.port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            reload=True,
        )
