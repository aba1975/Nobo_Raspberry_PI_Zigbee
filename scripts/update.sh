#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — Update script
# Pulls latest code, rebuilds, and restarts the service

INSTALL_DIR="/opt/nobo-control"
SERVICE_NAME="nobo-control"

echo "Updating Nobo Web Control..."

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "ERROR: $INSTALL_DIR is not a git repository."
    echo "  Run scripts/install.sh first."
    exit 1
fi

cd "$INSTALL_DIR"

echo "[1/5] Pulling latest code..."
git pull

echo "[2/5] Rebuilding Docker image..."
docker compose build

echo "[3/5] Checking data directory ownership..."
# The container used to run as root and now runs as uid 1001. An existing
# data volume is still owned by root, and the application could not write
# users.json any more, so nobody could log in after upgrading.
VOLUME_NAME=$(docker inspect nobo-web-control \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' \
    2>/dev/null || true)
if [ -z "$VOLUME_NAME" ]; then
    MATCHES=$(docker volume ls -q 2>/dev/null | grep -E '(^|_)nobo-data$' || true)
    if [ "$(echo "$MATCHES" | grep -c .)" = "1" ]; then
        VOLUME_NAME="$MATCHES"
    fi
fi
if [ -n "$VOLUME_NAME" ]; then
    MOUNT=$(docker volume inspect "$VOLUME_NAME" --format '{{.Mountpoint}}' 2>/dev/null || true)
    if [ -n "$MOUNT" ] && [ -d "$MOUNT" ]; then
        chown -R 1001:1001 "$MOUNT"
        echo "  Data volume $VOLUME_NAME is writable by the application user."
    fi
fi

echo "[4/5] Restarting service..."
systemctl restart "$SERVICE_NAME"

echo "[5/5] Checking service status..."
sleep 3
systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Update complete. Web interface: http://$(hostname -I | awk '{print $1}'):8000"
