# Nobo Web Control for Raspberry Pi

A containerized deployment of [nobo-web-control](https://github.com/aba1975/nobo-web-control) for Raspberry Pi 4B running Ubuntu Server. Provides local web-based control of your Nobo Energy Hub heating system.

This is a port of the original Windows-based project. The application code is identical; only the deployment method has changed (Docker + systemd instead of Windows Service/Task Scheduler).

## What This Project Does

- Controls your Nobo heating system through a web interface on your local network
- Shows real-time temperatures and zone status via WebSocket
- Supports all Nobo device types (NTB-2R, R80 RDC 700, and 20+ others)
- Provides comfort, eco, and away modes per zone or globally
- Includes weekly schedule editing and scheduled away mode
- Runs 24/7 on your Raspberry Pi as an always-on home server
- Works entirely on your local network — no cloud required

> **Important:** The Nobo Eco Hub only allows one TCP connection at a time. While this web control system is connected, the official Nobo app cannot connect simultaneously.

## Prerequisites

### Hardware

- Raspberry Pi 4B (4 GB or 8 GB RAM recommended)
- microSD card (16 GB or larger, Class 10 or better)
- Power supply (official USB-C 5V/3A recommended)
- Ethernet cable (recommended) or Wi-Fi connection
- Nobo Energy Hub on the same local network

### Software

- Ubuntu Server 24.04 LTS (ARM64) — installed on the Raspberry Pi
- Docker and Docker Compose (installed by the setup script, or manually)

### Information You Need

- **Hub serial number:** 12-digit number on the back of your Nobo Eco Hub (e.g., `123456789012`)
- **Hub IP address:** Found in your router's device list (e.g., `192.168.1.100`)

## Step 1: Prepare the Raspberry Pi

### Install Ubuntu Server on the microSD Card

1. Download the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer
2. Insert your microSD card
3. Open Raspberry Pi Imager:
   - **Choose Device:** Raspberry Pi 4
   - **Choose OS:** Other general-purpose OS → Ubuntu → Ubuntu Server 24.04 LTS (64-bit)
   - **Choose Storage:** Your microSD card
4. Click the gear icon (settings) before writing:
   - Set a hostname (e.g., `nobo-pi`)
   - Enable SSH (use password authentication for now)
   - Set a username and password (e.g., `nobo` / choose a strong password)
   - Configure Wi-Fi if not using Ethernet
   - Set your locale and timezone
5. Click **Write** and wait for it to finish
6. Insert the microSD card into your Raspberry Pi and power it on
7. Wait 2-3 minutes for first boot to complete

## Step 2: Enable and Use SSH

SSH should already be enabled if you configured it in the Raspberry Pi Imager (Step 1). If not:

### Enable SSH (if needed)

Connect a keyboard and monitor to your Pi, log in, then:

```bash
sudo systemctl enable ssh
sudo systemctl start ssh
```

### Verify SSH Is Running

```bash
sudo systemctl status ssh
```

You should see `Active: active (running)`.

### Connect from Another Computer

Find your Pi's IP address (on the Pi itself):

```bash
hostname -I
```

From your computer (replace `192.168.1.50` with your Pi's actual IP):

**Linux / macOS Terminal:**
```bash
ssh nobo@192.168.1.50
```

**Windows (PowerShell or Command Prompt):**
```bash
ssh nobo@192.168.1.50
```

**Windows (PuTTY):** Enter the IP address, port 22, click Open, and log in.

Accept the fingerprint prompt the first time (`yes`).

### Optional: Use SSH Keys Instead of Passwords

On your computer, generate a key pair (if you don't have one):

```bash
ssh-keygen -t ed25519
```

Copy the public key to your Pi:

```bash
ssh-copy-id nobo@192.168.1.50
```

Now you can connect without typing a password.

### Optional: Harden SSH

Edit the SSH config on the Pi:

```bash
sudo nano /etc/ssh/sshd_config
```

Recommended changes (after setting up SSH keys):

```
PasswordAuthentication no
PermitRootLogin no
```

Apply changes:

```bash
sudo systemctl restart ssh
```

## Step 3: Install Docker and Docker Compose

SSH into your Raspberry Pi, then run:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

Add your user to the docker group (so you don't need `sudo` for docker commands):

```bash
sudo usermod -aG docker $USER
```

Log out and back in for the group change to take effect:

```bash
exit
```

Then SSH in again and verify Docker works:

```bash
docker --version
docker compose version
```

Both commands should print version information.

## Step 4: Clone the Repository

```bash
sudo git clone https://github.com/aba1975/Nobo_Raspberry_PI.git /opt/nobo-control
sudo chown -R $USER:$USER /opt/nobo-control
cd /opt/nobo-control
```

## Step 5: Configure Environment Variables

```bash
cp .env.example .env
nano .env
```

Edit the file with your hub details:

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=false
```

- `NOBO_SERIAL`: The 12-digit serial number from the back of your Nobo Eco Hub
- `NOBO_IP`: The IP address of your hub on your local network
- `NOBO_DEMO`: Set to `true` to test without a real hub (uses simulated data)

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

> **Tip:** To find your hub's IP address, check your router's admin page for connected devices. Look for a device named "Nobo Hub" or similar.

## Step 6: Start the System

### Option A: Quick Start (manual)

```bash
cd /opt/nobo-control
docker compose up --build -d
```

### Option B: Using the Install Script (recommended)

The install script handles everything (Docker installation, image build, systemd setup):

```bash
cd /opt/nobo-control
sudo bash scripts/install.sh
```

Then start the service:

```bash
sudo systemctl start nobo-control
```

## Step 7: Make It Start on Reboot

If you used the install script (Option B above), the service is already enabled. Otherwise:

```bash
sudo cp /opt/nobo-control/deploy/systemd/nobo-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nobo-control
sudo systemctl start nobo-control
```

Verify the service is running:

```bash
sudo systemctl status nobo-control
```

You should see `Active: active (running)`.

These commands explained:
- `systemctl enable` — tells the system to start this service automatically on every boot
- `systemctl start` — starts it right now
- `systemctl status` — shows whether it is running

## Step 8: Verify It Is Working

### Check the service status

```bash
sudo systemctl status nobo-control
```

### Check the container is healthy

```bash
docker ps
```

Look for the `nobo-web-control` container with status `healthy`.

### Open the web interface

From any device on your local network, open a browser and go to:

```
http://<YOUR_PI_IP>:8000
```

Replace `<YOUR_PI_IP>` with your Raspberry Pi's IP address (e.g., `http://192.168.1.50:8000`).

### Default login credentials

| Username | Password |
|----------|----------|
| `admin`  | `nobohub` |

**Change the default password immediately** after first login by clicking the user icon in the top-right corner.

### Check logs

```bash
sudo bash /opt/nobo-control/scripts/logs.sh
```

Or directly:

```bash
cd /opt/nobo-control && docker compose logs --tail 50 -f
```

Press `Ctrl+C` to stop following logs.

## Updating the Software

To update to the latest version:

```bash
cd /opt/nobo-control
sudo bash scripts/update.sh
```

This pulls the latest code, rebuilds the Docker image, and restarts the service. Your configuration (`.env`) and data (user accounts, schedules) are preserved.

Manual update steps if preferred:

```bash
cd /opt/nobo-control
git pull
docker compose build
sudo systemctl restart nobo-control
```

## Troubleshooting

### Service won't start

```bash
# Check service logs
sudo journalctl -u nobo-control -n 50 --no-pager

# Check Docker logs
cd /opt/nobo-control && docker compose logs --tail 50
```

### Can't connect to the Nobo Hub

- Verify the serial number and IP in `.env` are correct
- Check that the Pi and Hub are on the same network/subnet
- Make sure no other app (like the official Nobo app) is connected to the hub
- Try restarting the Nobo Hub (power cycle)
- Check the logs for connection error messages

### Web interface not loading

- Check the container is running: `docker ps`
- Check nothing else is using port 8000: `sudo ss -tlnp | grep 8000`
- Try accessing from the Pi itself: `curl http://localhost:8000/api/health`
- Check firewall: `sudo ufw status` (if active, run `sudo ufw allow 8000`)

### Docker build fails

- Ensure you have internet access: `ping -c 1 google.com`
- Check disk space: `df -h` (Docker images need ~500 MB)
- Try rebuilding without cache: `docker compose build --no-cache`

### Container keeps restarting

```bash
docker logs nobo-web-control --tail 100
```

Common causes:
- Invalid `NOBO_SERIAL` or `NOBO_IP` in `.env`
- Hub not reachable on the network
- Port 8000 already in use

### Pi runs out of memory

If using a 2 GB Pi and experiencing issues:
```bash
# Check memory usage
free -h

# The app typically uses ~100-150 MB
docker stats --no-stream
```

### Permission denied errors

```bash
# Fix ownership of the project directory
sudo chown -R $USER:$USER /opt/nobo-control
```

## Backing Up Configuration and Data

### Create a backup

```bash
sudo bash /opt/nobo-control/scripts/backup.sh
```

Backups are saved to `~/nobo-backups/` by default. You can specify a different directory:

```bash
sudo bash /opt/nobo-control/scripts/backup.sh /path/to/backup/dir
```

### What is backed up

- `.env` — your hub configuration (serial, IP)
- `data/` volume — user accounts, away schedules, demo zone state, server state

### Restore from backup

```bash
tar -xzf ~/nobo-backups/nobo-backup-YYYYMMDD-HHMMSS.tar.gz
sudo cp backup/.env /opt/nobo-control/
sudo docker cp backup/data/. nobo-web-control:/app/data/
sudo systemctl restart nobo-control
```

## Differences from the Windows Version

| Feature | Windows Version | Raspberry Pi Version |
|---------|----------------|---------------------|
| Runtime | Python installed directly | Docker container |
| Auto-start | Windows Service (NSSM) or Task Scheduler | systemd + Docker |
| Configuration | Environment variables or edit `server.py` | `.env` file |
| Data storage | `data/` folder on disk | Docker named volume |
| Updates | Manual download | `git pull` + rebuild |
| Network | Runs directly on host network | Docker host networking mode |

The application code is identical. All features — zones, schedules, device management, WebSocket updates, authentication, away scheduling — work the same way.

## Project Structure

```
Nobo_Raspberry_PI/
├── app/                        # Application code (from nobo-web-control)
│   ├── server.py               # FastAPI backend
│   ├── auth.py                 # Authentication module
│   ├── away_schedule.py        # Away schedule persistence
│   ├── config_persistence.py   # Config/state persistence
│   └── static/                 # Web UI (HTML, CSS, JS, images)
├── tests/                      # Test suite (pytest)
├── deploy/
│   └── systemd/
│       └── nobo-control.service  # systemd unit file
├── scripts/
│   ├── install.sh              # First-time setup
│   ├── update.sh               # Update to latest version
│   ├── backup.sh               # Backup config/data
│   ├── start.sh                # Start the service
│   ├── stop.sh                 # Stop the service
│   └── logs.sh                 # View logs
├── Dockerfile                  # Container build instructions
├── compose.yml                 # Docker Compose configuration
├── .env.example                # Configuration template
├── requirements.txt            # Python runtime dependencies
├── requirements-dev.txt        # Development/testing dependencies
└── README.md                   # This file
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8000 | TCP/HTTP | Web interface and API |

The container runs with `network_mode: host`, meaning it shares the Pi's network stack directly. This is required so the application can discover and communicate with the Nobo Eco Hub on your LAN.

## API

The server provides a REST API for integration with other systems (e.g., Home Assistant):

- `GET /api/health` — Health check
- `GET /api/status` — Connection status and away schedule
- `GET /api/zones` — All zones with current status
- `POST /api/zones/{zone_id}/override/{mode}` — Set zone mode (comfort/eco/away/normal)
- `POST /api/zones/{zone_id}/temperature` — Set zone temperatures
- `POST /api/global/override/{mode}` — Set global mode for all zones
- `GET /api/zones/{zone_id}/schedule` — Get zone weekly schedule
- `POST /api/zones/{zone_id}/schedule` — Update zone weekly schedule
- `GET /api/devices` — List all devices
- `WS /ws` — WebSocket for real-time updates

API endpoints (`/api/*` and `/ws`) do not require authentication, so local integrations work without credentials.

## Security Notes

- This system is designed for use on a trusted local network only
- Do not expose port 8000 to the internet without a reverse proxy and TLS
- Change the default admin password immediately after first login
- The web UI requires login; API endpoints are open for local integrations
- User passwords are stored with bcrypt hashing

## License

This project is provided as-is for personal use. See the original project at [nobo-web-control](https://github.com/aba1975/nobo-web-control).

## Acknowledgments

- Application code from [nobo-web-control](https://github.com/aba1975/nobo-web-control)
- Built with [FastAPI](https://fastapi.tiangolo.com/) and [pynobo](https://github.com/echoromeo/pynobo)
- Containerized for Raspberry Pi with Docker
