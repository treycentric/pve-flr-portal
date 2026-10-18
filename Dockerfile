# The container image .github/workflows/image.yml publishes to GHCR, and
# what docker-compose.yml builds for local dev/testing. The LXC install
# under deploy/ remains the recommended way to run this on a PVE host.
FROM python:3.11-slim

# PFR_RELOAD=false: the code never changes inside the image, so uvicorn's
# file-watching reload supervisor (run.py's default for a source checkout)
# only costs a process. PFR_DATA_DIR is absolute so it doesn't depend on
# the working directory.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PFR_RELOAD=false \
    PFR_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# An unprivileged user. At runtime the app writes only certs/ (the
# self-signed cert it generates unless one is mounted there), the data dir,
# and the temp dir while it builds a download bundle.
RUN useradd --system --uid 10001 --user-group --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin pfr \
    && mkdir -p /app/certs /app/data \
    && chown pfr:pfr /app/certs /app/data
USER 10001:10001

EXPOSE 8008

# /static/style.css needs no login and doesn't call PVE (/login does, with
# retries), so it tests the web server alone. The portal's cert is
# self-signed by default, hence no verification on this loopback call.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:%s/static/style.css' % os.environ.get('PORT', '8008'), context=ssl._create_unverified_context(), timeout=4)"]

CMD ["python", "run.py"]
