FROM python:3.12-slim

WORKDIR /app

# Install build dependencies for bcrypt (needed on ARM64)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Create data directory for persistent storage
RUN mkdir -p /app/data

# Run as an unprivileged user. The application never needs root, and the
# container shares the Pi's network namespace (network_mode: host), so a flaw
# in a web request should not get root on the device.
#
# The data directory must be owned by that user, otherwise the first write of
# users.json fails and nobody can log in.
RUN useradd --system --create-home --uid 1001 nobo && \
    chown -R nobo:nobo /app
USER nobo

EXPOSE 8000

# The listen address and port are read at start-up rather than baked in, so one
# image can run either on its own (0.0.0.0:8000, the default and what everyone
# gets) or behind the TLS proxy (127.0.0.1:8000, where the plain-HTTP port is
# not reachable from the network at all).
#
# The health check always uses the loopback address, which is correct either
# way, and reads the port from the environment so the two cannot drift apart.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('NOBO_PORT', '8000') + '/api/health')" || exit 1

# exec, so uvicorn replaces the shell and becomes PID 1. Without it the signal
# on "docker compose down" reaches the shell instead, and the container is
# killed after the timeout rather than shutting down cleanly.
#
# --proxy-headers with --forwarded-allow-ips=127.0.0.1 means X-Forwarded-Proto
# is honoured from the local reverse proxy and ignored from anywhere else, so
# request.url.scheme is right behind TLS without letting the network forge it.
CMD ["sh", "-c", "exec uvicorn server:app --host ${NOBO_BIND:-0.0.0.0} --port ${NOBO_PORT:-8000} --proxy-headers --forwarded-allow-ips=127.0.0.1 --log-level info"]
