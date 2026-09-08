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

A data listener is an optional enhancement: if its IP isn't a local
address (a wrong `local_ip`, or the interface isn't attached yet) it is
skipped with a clear log line and the main portal runs normally - DNT
just isn't offered for that subnet (guests there fall back to the
chunked write path).

Data-plane TLS (issue #47, docs/plan.md §7.6.1): those data listeners
are plain HTTP when RESTORE_DATA_NIC_TLS_PREFERRED is `plaintext`
(unchanged from before #47) and HTTPS otherwise, using a dedicated
self-signed data-plane cert (IP SANs for the configured NICs) unless the
admin drops their own at RESTORE_DATA_NIC_TLS_CERT_FILE/_KEY_FILE.
"""
import asyncio
import errno
import logging
import socket
import ssl
from pathlib import Path

import uvicorn

from backend.config import ensure_data_dir, settings
from backend.restore_network_pull import parse_data_nics
from backend.tls import ensure_data_plane_cert, ensure_self_signed_cert

_log = logging.getLogger("pve_flr_portal.run")

_MIN_TLS = {"1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}


def _data_plane_tls_enabled() -> bool:
    return settings.restore_data_nic_tls_preferred != "plaintext"


def _bind_error(ip: str, port: int) -> str | None:
    """None if this host can bind `(ip, port)`, else a short reason -
    used to skip a misconfigured data NIC before it takes the whole
    process down with a startup bind error. Distinguishes "not a local
    address" (wrong local_ip) from "port already in use" (usually a
    collision with the main 0.0.0.0 listener - set RESTORE_DATA_NIC_PORT)."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        return str(exc)
    try:
        probe.bind((ip, port))
        return None
    except OSError as exc:
        if exc.errno == errno.EADDRNOTAVAIL:
            return f"{ip} is not a local address on this host - fix local_ip or attach that interface"
        if exc.errno == errno.EADDRINUSE:
            return f"{ip}:{port} is already in use - set RESTORE_DATA_NIC_PORT to a free port"
        return str(exc)
    finally:
        probe.close()


def _prepare_data_plane_cert(data_nics) -> tuple[Path, Path]:
    cert_path = Path(settings.restore_data_nic_tls_cert_file)
    key_path = Path(settings.restore_data_nic_tls_key_file)
    # The cert only needs to cover what the download URL will actually
    # present: a NIC's `hostname` when it has one, otherwise its IP. A
    # NIC with a hostname doesn't need an IP SAN (the URL uses the name),
    # so an admin's DNS-only cert (Let's Encrypt / step-ca) isn't flagged
    # as "does not cover".
    ip_sans = tuple(dict.fromkeys(nic.local_ip for nic in data_nics if not nic.hostname))
    dns_sans = tuple(dict.fromkeys(nic.hostname for nic in data_nics if nic.hostname))
    ensure_data_plane_cert(cert_path, key_path, ip_sans, dns_sans)
    return cert_path, key_path


async def _run_data_listener(server: uvicorn.Server, ip: str, port: int) -> None:
    try:
        await server.serve()
    except SystemExit:  # uvicorn's bind-failure path raises this
        _log.error(
            "Direct Network Transfer data listener on %s:%s failed to start. The portal is "
            "running normally; DNT is unavailable for that subnet until this is fixed.",
            ip,
            port,
        )
    except OSError as exc:
        _log.error("Direct Network Transfer data listener on %s:%s: %s", ip, port, exc)


async def _serve_with_data_nics(cert_path: Path, key_path: Path, data_nics) -> None:
    main_config = uvicorn.Config(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )

    # A specific-IP listener on the same port as the main 0.0.0.0 bind
    # collides (EADDRINUSE) on most kernels, so default the data plane to
    # PORT+1 rather than PORT. An admin can still pin it with
    # RESTORE_DATA_NIC_PORT if their setup allows sharing.
    data_port = settings.restore_data_nic_port or (settings.port + 1)
    tls = _data_plane_tls_enabled()
    dp_cert = dp_key = dp_ca = None
    if tls:
        dp_cert, dp_key = _prepare_data_plane_cert(data_nics)
        dp_ca = settings.restore_data_nic_tls_ca_file or None

    # The main listener's lifetime governs the process; a data listener
    # that can't start is logged and dropped, never fatal.
    coros = [uvicorn.Server(main_config).serve()]

    # One data-plane listener per distinct configured IP, bound to that
    # specific interface only - never 0.0.0.0, which would defeat the
    # whole point of keeping the data plane separate from the
    # UI/PVE-management listener above.
    for ip in sorted({nic.local_ip for nic in data_nics}):
        reason = _bind_error(ip, data_port)
        if reason is not None:
            _log.error(
                "Skipping the Direct Network Transfer data listener for %s: %s. The portal is "
                "running normally; DNT stays unavailable for that subnet.",
                ip,
                reason,
            )
            continue
        if tls:
            data_config = uvicorn.Config(
                "backend.main:app",
                host=ip,
                port=data_port,
                ssl_certfile=str(dp_cert),
                ssl_keyfile=str(dp_key),
                ssl_ca_certs=dp_ca,
            )
            try:
                data_config.load()  # builds the SSL context - raises on a bad cert/key
                data_config.ssl.minimum_version = _MIN_TLS[settings.restore_data_nic_tls_min_version]
            except (ssl.SSLError, OSError, ValueError) as exc:
                _log.error(
                    "Skipping the Direct Network Transfer data listener for %s: bad data-plane "
                    "cert/key (%s). The portal is running normally; DNT stays unavailable for that subnet.",
                    ip,
                    exc,
                )
                continue
            scheme = "https"
        else:
            data_config = uvicorn.Config("backend.main:app", host=ip, port=data_port)
            scheme = "http"
        print(f"Direct Network Transfer: also serving the download route on {scheme}://{ip}:{data_port}")
        coros.append(_run_data_listener(uvicorn.Server(data_config), ip, data_port))

    await asyncio.gather(*coros)


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
