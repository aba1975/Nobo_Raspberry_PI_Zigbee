#!/usr/bin/env bash
set -euo pipefail

# Show logs from the Nobo Web Control container
# Usage: sudo bash scripts/logs.sh         (follow mode)
#        sudo bash scripts/logs.sh 100     (last 100 lines, then follow)

LINES="${1:-50}"

cd /opt/nobo-control
docker compose logs --tail "$LINES" -f
