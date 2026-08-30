"""Entrypoint that serves the portal over HTTPS by default (docs/plan.md
§7.3). Cert bootstrap has to happen before uvicorn binds its SSL
context, which is too late to do from a FastAPI startup event - hence a
small script instead of `uvicorn backend.main:app` directly.
"""
from pathlib import Path

import uvicorn

from backend.config import settings
from backend.tls import ensure_self_signed_cert

if __name__ == "__main__":
    cert_path = Path(settings.tls_cert_file)
    key_path = Path(settings.tls_key_file)
    ensure_self_signed_cert(cert_path, key_path, common_name=settings.pve_host)

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        reload=True,
    )
