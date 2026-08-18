#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — First-time installation script for Raspberry Pi / Ubuntu Server
# Run as root or with sudo

INSTALL_DIR="/opt/nobo-control"
REPO_URL="https://github.com/aba1975/Nobo_Raspberry_PI.git"
SERVICE_NAME="nobo-control"

echo "============================================"
echo "  Nobo Web Control — Installation Script"
echo "============================================"
echo ""

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    echo "  sudo bash scripts/install.sh"
    exit 1
fi

# Step 1: Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "[1/6] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "  Docker installed successfully."
else
    echo "[1/6] Docker is already installed."
fi

# Step 2: Verify Docker Compose is available
if docker compose version &> /dev/null; then
    echo "[2/6] Docker Compose plugin is available."
else
    echo "[2/6] ERROR: Docker Compose plugin not found."
    echo "  Try: sudo apt-get install docker-compose-plugin"
    exit 1
fi

# Step 3: Clone or update the repository
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[3/6] Repository already exists at $INSTALL_DIR — pulling latest..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "[3/6] Cloning repository to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Step 4: Set up environment file
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[4/6] Creating .env from template..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "  IMPORTANT: Edit /opt/nobo-control/.env with your hub details:"
    echo "    sudo nano /opt/nobo-control/.env"
    echo ""
    echo "  Set NOBO_SERIAL to your hub's 12-digit serial number"
    echo "  Set NOBO_IP to your hub's IP address on the LAN"
    echo ""
else
    echo "[4/6] .env file already exists — keeping current configuration."
fi

# Step 5: Install systemd service
echo "[5/6] Installing systemd service..."
cp "$INSTALL_DIR/deploy/systemd/nobo-control.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "  Service installed and enabled for automatic startup."

# Step 6: Build the Docker image
echo "[6/6] Building Docker image (this may take a few minutes on first run)..."
cd "$INSTALL_DIR"
docker compose build

echo ""
echo "============================================"
echo "  Installation complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit your configuration:"
echo "     sudo nano /opt/nobo-control/.env"
echo ""
echo "  2. Start the service:"
echo "     sudo systemctl start nobo-control"
echo ""
echo "  3. Check it is running:"
echo "     sudo systemctl status nobo-control"
echo ""
echo "  4. Open the web interface:"
echo "     http://$(hostname -I | awk '{print $1}'):8000"
echo ""
echo "  Default login: admin / nobohub"
echo "  (Change the password after first login!)"
echo ""
