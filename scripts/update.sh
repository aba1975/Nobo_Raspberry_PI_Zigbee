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

echo "[1/4] Pulling latest code..."
git pull

echo "[2/4] Rebuilding Docker image..."
docker compose build

echo "[3/4] Restarting service..."
systemctl restart "$SERVICE_NAME"

echo "[4/4] Checking service status..."
sleep 3
systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Update complete. Web interface: http://$(hostname -I | awk '{print $1}'):8000"
