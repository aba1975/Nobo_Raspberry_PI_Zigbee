#!/usr/bin/env bash
set -euo pipefail

# Start the Nobo Web Control service

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

echo "Starting Nobo Web Control..."
systemctl start nobo-control
sleep 2
systemctl status nobo-control --no-pager
echo ""
echo "Web interface: http://$(hostname -I | awk '{print $1}'):8000"
