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

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
