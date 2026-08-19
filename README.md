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

## Features

Everything below is reached from the web interface at `http://<pi-ip>:8000`.

### Heating control

| Feature | What it does |
| --- | --- |
| **Zone overview** | Every zone with its current temperature, comfort and eco set points, and the mode it is in right now. Updates by itself — you never need to refresh. |
| **Per-zone override** | Put a single zone into Comfort, Eco or Away, or return it to Normal so it follows its weekly schedule again. |
| **Global mode** | Put the whole house into Comfort, Eco, Away or Home in one click. |
| **Temperature set points** | Set the comfort and eco temperature per zone, between 7 °C and 30 °C. The eco temperature must be lower than the comfort temperature, and values are rounded to whole degrees because that is all the hub stores. |
| **Weekly schedule** | A per-zone plan of which mode applies at which time on each day of the week (see [Weekly schedule rules](#weekly-schedule-rules)). |
| **Scheduled away** | Set a holiday period. The house goes to Away when it starts and back to Home when it ends. |
| **Command log** | A running list of what was sent to the hub and what came back, which is the first place to look when something behaves unexpectedly. |

Some devices — plain on/off receivers such as the R80 RSC 700 — have no
adjustable set point. Their temperature is set on the device itself, and the
interface says so rather than pretending the change worked.

### Accounts

| Feature | What it does |
| --- | --- |
| **Login** | The whole interface and the entire API require a login. Nothing is readable without one. |
| **Change password** | Under the 👤 icon. |
| **Rename your account** | Under the 👤 icon. |
| **Manage users** | Admins can add, remove and change the role of other users. |
| **Lockout** | Repeated failed logins from the same address are temporarily blocked. |

### Administration

| Feature | What it does |
| --- | --- |
| **Hub settings in the browser** | Switch between demo mode and your real hub, and set the hub serial and IP, without editing files or using SSH. See [Changing Hub Settings From the Web Interface](#changing-hub-settings-from-the-web-interface). |
| **Demo mode** | A full simulated house with eight zones, so you can try everything before a hub is connected. |
| **Automatic start** | Starts on boot and restarts by itself if it stops. |
| **Backup and restore** | A script that captures your settings and data. |

### What works with a real hub, and what does not

This project talks to the hub through
[pynobo](https://github.com/echoromeo/pynobo), which exposes what the hub's own
protocol offers. Some things the hub simply does not allow a third party to do.

| Feature | Demo mode | Real hub |
| --- | --- | --- |
| View zones, temperatures and modes | ✅ | ✅ |
| Per-zone and global overrides | ✅ | ✅ |
| Change comfort / eco temperatures | ✅ | ✅ |
| Rename a zone | ✅ | ✅ |
| Scheduled away | ✅ | ✅ |
| View weekly schedules | ✅ | ✅ |
| **Edit weekly schedules** | ✅ | ❌ |
| **Add or delete a zone** | ✅ | ❌ |
| **Add, remove, move, rename or replace a device** | ✅ | ❌ |
| **Discover or pair a new device** | ❌ | ❌ |

Use the official Nobø app for anything in the "real hub ❌" rows. Pairing in
particular is done entirely by the hub during pairing mode; there is no
discovery in this project, and none is planned.

You do not have to remember this table. When the application is connected to a
real hub, the controls it cannot honour are greyed out with an explanation, so
nothing you can click will fail with a "not implemented" error.

### Weekly schedule rules

When you edit a schedule, the whole week is sent at once and it must describe
every minute of every day:

- All seven days must be present.
- Each day's blocks must run from `00:00` to `24:00` with no gaps and no overlaps.
- Blocks must be in order, and each must be at least one minute long.
- Times are in the Raspberry Pi's own timezone, not UTC (see [Timezone](#timezone)).

A partial update is rejected rather than merged, so that a saved schedule is
never half old and half new. The editor in the web interface builds a valid
week for you; these rules matter if you call the API yourself.

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

First you need your Pi's IP address. Pick whichever method is easiest:

**Method 1 — try the hostname (no monitor needed).** If you set the hostname to `nobo-pi` in Step 1, try this from your computer:

```bash
ssh nobo@nobo-pi.local
```

If that works, you can skip the rest of this section.

**Method 2 — check your router.** Open your router's admin page in a browser and look at the list of connected devices. Find the one named after your hostname (e.g., `nobo-pi`) and note its IP address (e.g., `192.168.1.50`).

**Method 3 — read it on the Pi.** If you have a keyboard and monitor connected, log in and run:

```bash
hostname -I
```

The first number shown is your Pi's IP address.

Once you have the IP, connect from your computer (replace `192.168.1.50` with your Pi's actual IP):

**Linux / macOS Terminal:**
```bash
ssh nobo@192.168.1.50
```

**Windows (PowerShell or Command Prompt):**
```bash
ssh nobo@192.168.1.50
```

**Windows (PuTTY):** Enter the IP address, port 22, click Open, and log in.

Accept the fingerprint prompt the first time (`yes`), then enter the password you set in Step 1.

> **Tip:** Give your Pi a fixed (static) IP address in your router settings. Otherwise the IP may change after a reboot and your bookmark to the web interface will stop working.

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

> **Shortcut:** The install script in Step 6 (Option B) does all of Step 3 and Step 4 for you. If you plan to use it, you can skip straight to [Step 6](#step-6-start-the-system). The manual steps below are here so you know what the script does.

SSH into your Raspberry Pi, then run:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

This takes a few minutes. Add your user to the docker group (so you don't need `sudo` for docker commands):

```bash
sudo usermod -aG docker $USER
```

**You must log out and back in for the group change to take effect:**

```bash
exit
```

Then SSH in again and verify Docker works:

```bash
docker --version
docker compose version
```

Both commands should print version information. If you instead see `permission denied while trying to connect to the Docker daemon socket`, you did not log out and back in — run `exit` and reconnect.

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
- `NOBO_ALLOW_ANON_API`: Leave this alone. It is explained under [Security Notes](#security-notes).

**Two things that catch people out:**

1. `NOBO_DEMO` accepts `true`, `1` and `yes` (any capitalisation) as "on".
   Anything else, including an empty value, means off.
2. **The serial `111111111111` switches demo mode on by itself**, whatever
   `NOBO_DEMO` says. That value is also the built-in default, so if you skip
   creating `.env` entirely the system starts in demo mode and shows a
   simulated house rather than reporting an error. If you are seeing eight
   zones called "Large Bathroom", "Kitchen" and so on, this is why.

If the defaults are used because no `.env` exists, the hub IP defaults to
`10.0.0.100`. That is only a placeholder; it is not where your hub lives.

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

> **Tip:** To find your hub's IP address, check your router's admin page for connected devices. Look for a device named "Nobo Hub" or similar.

### Not ready to connect a real hub yet?

You can run the whole system in **demo mode** with simulated zones and temperatures. This is a good way to check the installation worked before touching your real heating system. Use these settings:

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=true
```

In demo mode the serial number and IP are ignored, so the placeholder values above are fine.

### Switching between demo mode and your real hub

You can switch at any time after installation. There are two ways to do it:

- **From the web interface (recommended)** — no SSH, no restart. See [Changing hub settings from the web interface](#changing-hub-settings-from-the-web-interface).
- **By editing `.env`** — the original method, described below.

#### Method 2: Editing the `.env` file

**1. Open the configuration file:**

```bash
sudo nano /opt/nobo-control/.env
```

**2. Change the values.**

To use **demo mode** (simulated zones, no hub needed):

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=true
```

To use your **real hub**:

```
NOBO_SERIAL=<your 12-digit serial>
NOBO_IP=<your hub's IP address>
NOBO_DEMO=false
```

**3. Save and exit** (`Ctrl+O`, `Enter`, `Ctrl+X`).

**4. Apply the change** — the setting is only read when the container starts, so you must restart:

```bash
sudo systemctl restart nobo-control
```

**5. Confirm which mode you are in:**

```bash
curl http://localhost:8000/api/status
```

Look at the `demo_mode` value in the response:

```json
{"connected":true,"demo_mode":true,"hub_serial":"123456789012",...}
```

- `"demo_mode":true` — running on simulated data
- `"demo_mode":false` and `"connected":true` — talking to your real hub
- `"connected":false` — check the serial number and IP, and make sure the official Nobo app is not connected to the hub

> **Note:** After switching modes, do a hard refresh in your browser (`Ctrl+Shift+R`, or `Cmd+Shift+R` on macOS) so it does not show cached data from the previous mode.

## Step 6: Start the System

### Option B: Using the Install Script (recommended)

The install script does everything for you: installs Docker, adds you to the docker group, clones or updates the code in `/opt/nobo-control`, creates `.env` if it is missing, installs the systemd service, and builds the Docker image.

```bash
sudo bash /opt/nobo-control/scripts/install.sh
```

The first run takes roughly 5-10 minutes, most of it building the Docker image.

> The script **enables** the service, meaning it will come up on every boot, but
> it deliberately does not start it there and then — you have not had a chance
> to put your hub details in `.env` yet. Nothing is running until you do this:

```bash
sudo systemctl start nobo-control
```

> **Note:** If the script installed Docker for the first time, log out (`exit`) and SSH back in before running `docker` commands yourself. Otherwise you will get `permission denied` on the Docker socket.

### Option A: Quick Start (manual)

Only use this if you skipped the install script. It starts the container but does **not** set it to start automatically on boot — you still need Step 7.

```bash
cd /opt/nobo-control
docker compose up --build -d
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

#### Why there are two "restart automatically" settings

`compose.yml` says `restart: always` and the systemd unit says
`Restart=always`. That looks like a conflict but is not, and you do not need to
change either:

- **Docker** brings the container back if the application inside it crashes.
- **systemd** brings the whole thing back if Docker itself, or the machine,
  went away.

Stopping the service runs `docker compose down`, which removes the container
altogether, and Docker never resurrects a container that no longer exists. So
`sudo systemctl stop nobo-control` really does stop it and it stays stopped.

The unit also runs `docker compose pull` before starting. Since the image is
built on the Pi rather than downloaded, this normally logs "Skipped - No image
to be pulled" and moves on. It is prefixed with `-` so a failure (for example
with no internet connection) cannot stop the service from starting.

### Confirm it really survives a reboot

It is worth testing this once, so you are not surprised after a power cut:

```bash
sudo reboot
```

Your SSH session will disconnect. Wait about 1-2 minutes, then SSH back in and check:

```bash
systemctl is-active nobo-control
docker ps
```

You should see `active`, and a `nobo-web-control` container with status `Up ... (healthy)`. The container normally becomes healthy within about 30 seconds of boot.

## Step 8: Verify It Is Working

### Check the service status

```bash
sudo systemctl status nobo-control
```

### Check the container is healthy

```bash
docker ps
```

Look for the `nobo-web-control` container with status `healthy`. It can take up to 30 seconds after starting before it changes from `health: starting` to `healthy`.

### Check the API responds

Run this on the Pi itself:

```bash
curl http://localhost:8000/api/health
```

You should get a response like:

```json
{"status":"ok","connected":true,"demo_mode":true,"timestamp":"..."}
```

`connected: true` means the app is talking to your hub (or to the simulated hub if `demo_mode` is `true`).

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

**Change the default password immediately** after first login — see [Managing User Accounts](#managing-user-accounts) below.

### Check logs

```bash
bash /opt/nobo-control/scripts/logs.sh
```

No `sudo` is needed here, as long as your user is in the `docker` group (the
install script puts you there). You can also ask for a different number of
past lines — the default is 50:

```bash
bash /opt/nobo-control/scripts/logs.sh 200
```

Or directly:

```bash
cd /opt/nobo-control && docker compose logs --tail 50 -f
```

Press `Ctrl+C` to stop following logs.

## Managing User Accounts

Everything to do with accounts lives behind the **user icon (👤) in the top-right corner** of the web interface. Click it to open the **User Settings** panel. Close it with the ✕ button, the `Esc` key, or by clicking outside the panel.

The panel has these sections:

### 🔑 Change Password

Available to every user, for their own account. Enter your current password, then the new one twice. Passwords must be at least 8 characters.

**Do this first**, to replace the default `admin` / `nobohub` credentials.

### ✏️ Rename Account

Change your own username. You stay logged in afterwards.

### 🛠️ Manage Users (admins only)

This section is only shown to users with the `admin` role. It lists every account and lets you:

- **Add a user** — enter a username and password (minimum 8 characters) and pick a role:
  - **`user`** — can view and control heating.
  - **`admin`** — can additionally manage accounts and change the hub connection settings.
- **Delete a user** — click the 🗑️ button next to them. You cannot delete your own account, so there is always at least one admin left.

### 🚪 Logout

Ends your session and returns you to the login page.

> **Note:** User accounts are stored in the Docker data volume (`data/users.json`), with passwords hashed using bcrypt. They survive restarts, software updates and reboots, and are included in `scripts/backup.sh`.

## Changing Hub Settings From the Web Interface

Once the system is running you no longer need SSH to switch between demo mode and your real Nobø Hub. You can do it from the browser.

### How to do it

1. Log in to the web interface at `http://<your-pi-ip>:8000` as an **admin** user.
2. Click the **cogwheel (⚙) / Devices** icon in the top bar.
3. At the top of the page you will see the **Hub Connection** card, showing which mode you are currently in.
4. Choose one of the two options:
   - **Demo mode** — simulated zones and temperatures, no hub required. Useful for testing.
   - **Connect to a real Nobø Hub** — then fill in:
     - **Hub serial number** — the 12 digits from the sticker on the bottom of the hub. Spaces are allowed, so `210 000 016 247` and `210000016247` both work.
     - **Hub IP address** — the hub's address on your network, e.g. `192.168.1.42`.
5. Click **Save Hub Settings**.
6. A confirmation box appears summarising exactly what will change. Click **Apply Change** to go ahead, or **Cancel** to go back.
7. **You are signed out and returned to the login page.** Log in again — the app reloads with the new settings.

The change is applied **immediately** on the server. **You do not need to restart the container or reboot the Pi.**

### Why does it log me out?

Switching between demo mode and a real hub replaces every zone, device and schedule in the system at once. Rather than leave half-updated pages open in your browser, the app deliberately ends your session so the next login starts from a clean, consistent state. This is expected behaviour, not an error.

### Good to know

- **Admin only.** Regular (non-admin) users cannot change the hub settings, and the card is not shown to them.
- **The setting is remembered.** It is written to `data/hub_config.json` inside the Docker data volume, so it survives container restarts, `docker compose down/up`, software updates, and a full Pi reboot. After a reboot the systemd service starts automatically in whichever mode you last selected.
- **It overrides `.env`.** If you have set the hub from the web interface, that value wins over `NOBO_DEMO`, `NOBO_SERIAL` and `NOBO_IP` in `.env`. The `.env` values are only used until the first time you save settings from the web interface. The Hub Connection card tells you which one is currently in effect (`environment` or `web interface`).
- **If the hub cannot be reached**, the settings are still saved and you get a warning message before being signed out. The app keeps retrying in the background, so once the hub becomes reachable it will connect on its own. Check that:
  - the serial number and IP address are correct,
  - the hub is powered on and on the same network,
  - the official Nobø app is **not** connected to the hub at the same time (the hub only accepts one connection).
- **Switching back to demo mode** is always safe and always works, even if the real hub is unreachable. This is a good way to confirm the web interface itself is healthy.

### Reading the status indicator

The coloured dot in the top-right corner tells you the real state of the system:

| Indicator | Meaning |
|-----------|---------|
| `Connecting...` | The browser is still opening its connection to the Pi. |
| `Disconnected` / `Connection Error` | The browser cannot reach the Pi at all. Check that the Pi is powered on and the service is running. |
| `🟡 Demo Mode` | Working normally on simulated data. No hub is being contacted. |
| `⚠️ Hub Unreachable` | The Pi is fine, but it cannot talk to your Nobø Hub. Zones will not load. Check the serial number and IP address under **Devices**. |
| `Connected` | Connected to your real hub and receiving live data. |

`⚠️ Hub Unreachable` clears by itself, without a page refresh, as soon as the hub becomes reachable again.

### Reverting to the `.env` file

If you want the `.env` file to control the mode again, delete the saved override and restart:

```bash
cd /opt/nobo-control
docker compose exec nobo-web-control rm -f /app/data/hub_config.json
sudo systemctl restart nobo-control
```

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

### Web page loads but no zones appear ("connecting" forever)

If the interface loads and you can log in, but the zone tiles never appear, first check whether the backend is actually fine:

```bash
curl http://localhost:8000/api/status
curl http://localhost:8000/api/zones
```

If those return data (in demo mode you should see 8 example zones), the backend is healthy and the problem is in the browser:

1. **Hard refresh the page** — `Ctrl+Shift+R` (`Cmd+Shift+R` on macOS). An old cached copy of `app.js` is the most common cause.
2. **Open the browser console** — press `F12` and look at the Console tab. A red `SyntaxError` or similar means the page script failed to load, which stops the zone list and the live updates from ever starting.
3. **Check for a newer version** — `sudo bash /opt/nobo-control/scripts/update.sh`.

If `/api/zones` returns an empty list while `NOBO_DEMO=false`, the app is connected to a real hub that has no zones configured — set up your zones in the official Nobo app first.

### "⚠️ Hub Unreachable" and "Cannot reach the Nobø hub"

The Pi and the web interface are working, but the app cannot talk to your Nobø Eco Hub, so there are no zones to show. Check, in order:

1. **The IP address is correct.** Hubs often get a new address from the router after a power cut. Find the current one in your router's device list, then update it under **Devices → Hub Connection**. Consider giving the hub a static/reserved IP in your router so this cannot happen again.
2. **The serial number is correct** — all 12 digits from the sticker on the hub.
3. **Nothing else is connected to the hub.** The hub accepts **one** connection at a time. Close the official Nobo app on every phone and tablet.
4. **The hub is on the same network** as the Pi and is powered on.

You can confirm what the server thinks is going on with:

```bash
curl http://localhost:8000/api/hub/config
```

To rule out a network problem entirely, switch to demo mode from **Devices → Hub Connection**. If the 8 example zones appear, the Pi and the app are healthy and the issue is purely with reaching the hub.

You do **not** need to restart anything after correcting the address — the app retries in the background and the indicator changes to `Connected` on its own.

### Clicking the user icon (👤) does nothing

The User Settings panel should open when you click the person icon in the top-right corner. If nothing happens:

1. **Hard refresh the page** — `Ctrl+Shift+R` (`Cmd+Shift+R` on macOS). An old cached copy of `auth.js` is the most likely cause.
2. **Make sure you are on the current version** — this was a genuine bug in earlier builds, where the script opened the panel with one CSS class while the stylesheet styled another, so the panel was technically open but invisible. Update with:

   ```bash
   sudo bash /opt/nobo-control/scripts/update.sh
   ```

3. **Check the browser console** — press `F12` and look at the Console tab for errors from `auth.js`.

Note that the **🛠️ Manage Users** section only appears for accounts with the `admin` role. If you can open the panel and change your own password but see no user management, you are signed in as a regular user.

### Some buttons are greyed out, or I get "not available when connected to a real hub"

That is deliberate. A few things only work on the demo data kept on the Pi —
adding and deleting zones, moving devices between zones, and similar
housekeeping. The Nobø hub does not accept those changes over the network, so
when you are connected to a real hub the buttons are disabled and the API
answers `501`. Use the official Nobø app for them. Everything to do with actual
heating — temperatures, modes, schedules, holiday periods — works in both
modes. The full list is in
[What works with a real hub](#what-works-with-a-real-hub-and-what-does-not).

### "permission denied while trying to connect to the Docker daemon socket"

Your user is not in the `docker` group yet, or you have not logged out since being added. Fix it:

```bash
sudo usermod -aG docker $USER
exit
```

Then SSH back in and try again. Group membership only applies to new login sessions.

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

Backups are saved to the `nobo-backups` folder in your home directory (e.g., `/home/nobo/nobo-backups/`). You can specify a different directory:

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
# docker cp writes the files as root, but the application runs as a normal
# user. Without this it cannot save changes and logins start failing.
sudo docker exec -u root nobo-web-control chown -R nobo:nobo /app/data
sudo systemctl restart nobo-control
```

The restart is not optional: the running application keeps its own copy of this
data in memory and would overwrite what you just restored.

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
├── tests/                      # Test suite (pytest) — see Testing below
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
├── pytest.ini                  # Test configuration (lets pytest find app/ and tests/)
├── .dockerignore               # What stays out of the image (tests, .git, .env)
├── .env.example                # Configuration template
├── requirements.txt            # Python runtime dependencies
├── requirements-dev.txt        # Development/testing dependencies
├── CLAUDE.md                   # Notes for AI coding assistants working on this repo
└── README.md                   # This file
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8000 | TCP/HTTP | Web interface and API |

The container runs with `network_mode: host`, meaning it shares the Pi's network stack directly. This is required so the application can discover and communicate with the Nobo Eco Hub on your LAN.

## API

The server provides a REST API for integration with other systems (for example
Home Assistant).

**Everything requires a login.** Send the `session_id` cookie you get back from
`POST /auth/login`. The only exception is `GET /api/health`, which is left open
so monitoring tools can check the service is alive without credentials.

```bash
# Log in and keep the session cookie in a file
curl -c cookies.txt -X POST http://<pi-ip>:8000/auth/login \
     -d 'username=admin&password=yourpassword'

# Then use it
curl -b cookies.txt http://<pi-ip>:8000/api/zones
```

Note that `/auth/login` takes form fields, not JSON.

### Reading

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Health check. The only endpoint that does not need a login. |
| `GET /api/status` | Connection status, demo mode, away schedule and the timezone in use |
| `GET /api/capabilities` | Which features work in the current mode (see [What works with a real hub](#what-works-with-a-real-hub-and-what-does-not)) |
| `GET /api/zones` | All zones with current status |
| `GET /api/zones/{zone_id}/schedule` | One zone's weekly schedule |
| `GET /api/week_profiles` | Week profiles as the hub stores them |
| `GET /api/devices` | All devices, with their friendly names and zone assignment |
| `GET /api/hub/config` | Current hub connection settings |
| `GET /api/log` | Recent commands sent to and received from the hub |
| `GET /api/global-mode/away-schedule` | The current holiday period, if any |
| `WS /ws` | Live updates. Pushes the current zones on connect, then again whenever anything changes. |

### Controlling

| Endpoint | Purpose |
| --- | --- |
| `POST /api/zones/{zone_id}/override/{mode}` | Set one zone to `comfort`, `eco`, `away` or `normal` |
| `POST /api/global/override/{mode}` | Set every zone at once. Here `home` is accepted as another word for `normal`; on the per-zone endpoint above it is not. |
| `POST /api/zones/{zone_id}/temperature` | Set `comfort` and/or `eco` for a zone |
| `PUT /api/zones/{zone_id}` | Rename a zone or change its icon |
| `PUT /api/global-mode/away-schedule` | Set the holiday period |
| `DELETE /api/global-mode/away-schedule` | Clear the holiday period |
| `POST /api/zones/{zone_id}/schedule` | Replace a zone's whole week (see [Weekly schedule rules](#weekly-schedule-rules)) |

### Administration

| Endpoint | Purpose |
| --- | --- |
| `POST /auth/login` / `POST /auth/logout` | Start and end a session |
| `GET /auth/me` | Who you are logged in as |
| `POST /auth/change-password`, `POST /auth/rename` | Your own account |
| `GET/POST /auth/admin/users`, `PATCH/DELETE /auth/admin/users/{username}` | Manage users (admins only) |
| `POST /api/hub/config` | Switch between demo mode and a real hub (admins only; ends your session on success) |
| `POST/PUT/PATCH/DELETE /api/zones`, `/api/devices/...` | Zone and device management — **demo mode only**, see the capability table above |

### If a request fails

The reason is always in the `detail` field, and the status code tells you what
kind of problem it is:

| Code | Meaning |
| --- | --- |
| `400` | The request itself was wrong — for example an eco temperature above the comfort temperature |
| `401` | Not logged in, or the session expired |
| `403` | Logged in, but this needs an admin |
| `404` | No such zone or device |
| `501` | The feature is not available against a real hub — use the official Nobø app |
| `503` | The hub is not reachable right now |

### Letting other systems in without a login

If you want Home Assistant or a script to read and control the heating without
handling a password, set `NOBO_ALLOW_ANON_API=true` in `.env` and restart. That
opens every `/api/...` address and the live-update connection to anyone who can
reach the Pi, so only do it on a network where you trust every device. Logging
in still works as normal, and the admin-only endpoints stay admin-only.

## Testing

The tests run in demo mode and need no hub, no network and no Raspberry Pi.

```bash
cd /opt/nobo-control          # or wherever you cloned it
pip install -r requirements-dev.txt
python -m pytest
```

Run one file, or one test, while working on something:

```bash
python -m pytest tests/test_capabilities.py
python -m pytest tests/test_temperature_validation.py -k rounding
```

Run them from the repository root. `pytest.ini` there tells pytest where the
application code and the tests live, so the command above works regardless of
which subdirectory you cloned into.

One message in the output is expected and harmless: a
`PynoboConnectionError: Failed to connect to Nobø Ecohub at 192.0.2.10`
traceback. A test deliberately points the application at an address that cannot
answer, to check it survives an unreachable hub.

To run them the same way the application does, in the container image:

```bash
cd /opt/nobo-control
docker compose build
docker run --rm --user root -v "$PWD":/src -w /src \
  nobo-control-nobo-web-control:latest \
  sh -c 'pip install -q -r requirements-dev.txt && python -m pytest'
```

`--user root` is needed because the image runs as an unprivileged user that
does not own your checkout.

## Timezone

Week schedules and away periods are wall-clock times: "07:00" means seven in
the morning where the Pi is, not in UTC. Containers default to UTC, so the
container is given the Pi's own clock settings (`/etc/localtime` and
`/etc/timezone` are mounted read-only in `compose.yml`).

Check which timezone is actually in use:

```bash
curl -b cookies.txt http://localhost:8000/api/status
```

Look at the `timezone` field. It is also written to the log at startup. If it
says `UTC` when it should not, fix the Pi's own timezone and restart:

```bash
sudo timedatectl set-timezone Europe/Oslo
sudo systemctl restart nobo-control
```

Do not set a `TZ` variable in `.env`. An empty `TZ` is treated as UTC and
overrides the mounted files, which is exactly the bug this avoids.

## Security Notes

- **This system is for a trusted local network only.** There is no HTTPS: the
  login password and session cookie travel across your network in plain text.
  Anyone able to watch that traffic can read them. On a home network behind a
  router this is a normal trade-off; on a shared or public network it is not.
- **Do not forward port 8000 from your router.** If you need access from
  outside the house, use a VPN back into your network, or put a reverse proxy
  such as Caddy or nginx in front of it to terminate TLS and reach the
  application over `http://localhost:8000`. Exposing it directly puts your
  heating, and a plain-text password, on the public internet.
- **Change the default `admin` / `nobohub` password immediately.** It is
  published here and in every copy of this repository.
- Every API address and the live-update connection require a login, unless you
  deliberately turn that off with `NOBO_ALLOW_ANON_API` (see above).
- Repeated failed logins from the same address are temporarily locked out.
- Passwords are stored as bcrypt hashes, never in plain text.
- The application runs as an unprivileged user inside the container, so a flaw
  reachable from a web request does not get root on the Pi. The container does
  share the Pi's network (`network_mode: host`), which is required to reach the
  hub, so it is not isolated from your LAN.
- The data volume holds your user accounts. Treat a backup of it like a
  password file.

## License

This project is provided as-is for personal use. See the original project at [nobo-web-control](https://github.com/aba1975/nobo-web-control).

## Acknowledgments

- Application code from [nobo-web-control](https://github.com/aba1975/nobo-web-control)
- Built with [FastAPI](https://fastapi.tiangolo.com/) and [pynobo](https://github.com/echoromeo/pynobo)
- Containerized for Raspberry Pi with Docker
