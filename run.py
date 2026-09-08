"""Entrypoint that serves the portal over HTTPS by default (docs/plan.md
§7.3). Cert bootstrap has to happen before uvicorn binds its SSL
context, which is too late to do from a FastAPI startup event - hence a
small script instead of `uvicorn backend.main:app` directly.

Direct Network Transfer (docs/plan.md §7.6, issue #22, step 5): when
RESTORE_DATA_NICS is configured, this also binds one additional listener
per distinct configured data-NIC IP, for the network-pull download route
(`GET /api/restore-downloads/{token}`). Those listeners have to run in
the *same process* as the main one - `restore_download`'s token store
and `restore_jobs.manager` are both in-memory and process-local
(CLAUDE.md - no extra services), so a guest's fetch has to land in the
same process that minted its token. uvicorn's own `--reload` supervisor
only wraps the single-server `uvicorn.run()` entrypoint, so the default
(no data NICs configured) keeps using `uvicorn.run(..., reload=True)`;
only the opt-in multi-listener path below gives that up.

Data-plane TLS (issue #47, docs/plan.md §7.6.1): those data listeners
are plain HTTP when RESTORE_DATA_NIC_TLS_PREFERRED is `plaintext`
(unchanged from before #47) and HTTPS otherwise, using a dedicated
self-signed data-plane cert (IP SANs for the configured NICs) unless the
admin drops their own at RESTORE_DATA_NIC_TLS_CERT_FILE/_KEY_FILE.
"""
import asyncio
import ssl
from pathlib import Path

import uvicorn

from backend.config import ensure_data_dir, settings
from backend.restore_network_pull import parse_data_nics
from backend.tls import ensure_data_plane_cert, ensure_self_signed_cert

_MIN_TLS = {"1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}


def _data_plane_tls_enabled() -> bool:
    return settings.restore_data_nic_tls_preferred != "plaintext"


def _prepare_data_plane_cert(data_nics) -> tuple[Path, Path]:
    cert_path = Path(settings.restore_data_nic_tls_cert_file)
    key_path = Path(settings.restore_data_nic_tls_key_file)
    ip_sans = tuple(dict.fromkeys(nic.local_ip for nic in data_nics))
    dns_sans = tuple(dict.fromkeys(nic.hostname for nic in data_nics if nic.hostname))
    ensure_data_plane_cert(cert_path, key_path, ip_sans, dns_sans)
    return cert_path, key_path


async def _serve_with_data_nics(cert_path: Path, key_path: Path, data_nics) -> None:
    main_config = uvicorn.Config(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )
    servers = [uvicorn.Server(main_config)]

    data_port = settings.restore_data_nic_port or settings.port
    tls = _data_plane_tls_enabled()
    dp_cert = dp_key = dp_ca = None
    if tls:
        dp_cert, dp_key = _prepare_data_plane_cert(data_nics)
        dp_ca = settings.restore_data_nic_tls_ca_file or None

    # One data-plane listener per distinct configured IP, bound to that
    # specific interface only - never 0.0.0.0, which would defeat the
    # whole point of keeping the data plane separate from the
    # UI/PVE-management listener above.
    for ip in sorted({nic.local_ip for nic in data_nics}):
        if tls:
            data_config = uvicorn.Config(
                "backend.main:app",
                host=ip,
                port=data_port,
                ssl_certfile=str(dp_cert),
                ssl_keyfile=str(dp_key),
                ssl_ca_certs=dp_ca,
            )
            data_config.load()
            data_config.ssl.minimum_version = _MIN_TLS[settings.restore_data_nic_tls_min_version]
            scheme = "https"
        else:
            data_config = uvicorn.Config("backend.main:app", host=ip, port=data_port)
            scheme = "http"
        servers.append(uvicorn.Server(data_config))
        print(f"Direct Network Transfer: also serving the download route on {scheme}://{ip}:{data_port}")

    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    ensure_data_dir()  # issue #30 - provision the app-state dir before serving
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
