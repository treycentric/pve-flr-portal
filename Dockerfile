# Primarily for local dev/testing (see README) - the LXC install under
# deploy/ is the recommended way to run this against a real PVE host.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8008

CMD ["python", "run.py"]
