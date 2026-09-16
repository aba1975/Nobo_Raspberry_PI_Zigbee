# Installing on a new Raspberry Pi

This is the whole job, start to finish, for somebody who has not done it
before. Follow it in order. Nothing here needs a text editor, and nothing needs
to be understood to work.

**About an hour**, most of which is the Pi thinking to itself while you make
coffee.

---

## What you need

- A **Raspberry Pi 4** (or 5) with a power supply
- A **microSD card**, 16 GB or larger
- A computer with an SD card reader, to write the card
- Your **home network** — the Pi can use Wi-Fi or a cable
- *Optional:* a **Zigbee USB stick** and door/window sensors
- *Optional:* your **Nobø Eco Hub**. You do not need it to start

You do **not** need to know Linux. You will type four commands.

---

## Step 1 — Put the operating system on the card

1. On your computer, install **Raspberry Pi Imager** from
   <https://www.raspberrypi.com/software/>.
2. Put the microSD card in your computer.
3. Open Imager and choose:
   - **Device:** your Pi model
   - **Operating System:** *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your microSD card

   "Lite" has no desktop. That is correct — this Pi has no screen.

4. Click the **gear / Edit Settings** button before writing, and set:

   | | |
   | --- | --- |
   | Hostname | `nobohub` |
   | Enable SSH | yes, *use password authentication* |
   | Username | `nobo` |
   | Password | something you will remember |
   | Wi-Fi | your network name and password, if not using a cable |
   | Locale / time zone | your own country |

   **Set the time zone correctly.** The heating runs on a clock, and a Pi that
   thinks it is in London will heat your house an hour late.

5. Write the card, then put it in the Pi and power it on.

Give it **two or three minutes** the first time. There is nothing to watch.

---

## Step 2 — Connect to the Pi

From your computer, open a terminal (on Windows: PowerShell) and type:

```
ssh nobo@nobohub.local
```

Say **yes** when it asks about authenticity, then give the password from
Step 1.

<details>
<summary>If <code>nobohub.local</code> is not found</summary>

Some networks do not resolve `.local` names. Find the Pi's IP address in your
router's list of connected devices — it will be something like `192.168.1.42` —
and use that instead:

```
ssh nobo@192.168.1.42
```
</details>

You are now typing commands **on the Pi**.

---

## Step 3 — Plug in the Zigbee stick, if you have one

Do this **now**, before installing, and the installer will find it by itself.

Plug it into a **blue USB 3 port** only if you have no alternative — USB 3
ports emit interference in the same band Zigbee uses. A **black USB 2 port** is
better, and a short **USB extension cable** better still, as it gets the
antenna away from the Pi.

Skip this step entirely if you are not using sensors. You can add them later.

---

## Step 4 — Install

Copy this line, paste it, press Enter:

```
sudo apt update && sudo apt install -y git && sudo git clone https://github.com/aba1975/Nobo_Raspberry_PI_Zigbee.git /opt/nobo-control && sudo bash /opt/nobo-control/scripts/install.sh
```

<details>
<summary>If it says it could not download the software</summary>

Either this Pi cannot reach the internet — check with `ping -c1 github.com` —
or you are installing from a **private** fork of this project, in which case
GitHub needs a token.

Make one at <https://github.com/settings/personal-access-tokens>, *fine-grained*,
with **Contents: Read-only** on that one repository. Then:

```
sudo git clone https://YOUR_TOKEN@github.com/YOUR_NAME/YOUR_FORK.git /opt/nobo-control
sudo bash /opt/nobo-control/scripts/install.sh
```

The token is stored on the Pi so updates keep working. It is read-only and
limited to that one repository.
</details>

It will ask for your password, then ask you a few questions.

### The questions

**"Connect a real Nobo hub now?"** — answer **n** the first time.

> This starts in *demo mode*: the system invents a house with rooms and
> heaters so you can look around and learn the interface. Nothing it does can
> reach a real heater. Connecting your real hub later takes thirty seconds and
> does not need a reinstall.

**"Set up Zigbee sensors?"** — **y** if you plugged the stick in, otherwise it
will not ask.

> If it says *"No USB serial device is plugged in"*, the Pi cannot see your
> stick. Finish the install anyway, plug the stick in, and run Step 6 below.

**"New password"** — choose one, at least 8 characters, and type it twice.

> This is the password for the web page. It is not the same as the Pi's own
> password, and it is nobody's business but yours.

Then it builds. **This takes five to fifteen minutes on a Pi.** It looks like
it has stopped. It has not. Leave it alone.

When it finishes it prints the address to open.

---

## Step 5 — Open it

On your phone or laptop, on the same network, go to the address it printed:

```
http://nobohub.local:8000
```

Log in as **admin** with the password you chose.

You should see rooms, temperatures and schedules. They are invented — that is
demo mode doing its job. Press things. You cannot break anything.

> **Add it to your home screen.** On iPhone: Share → *Add to Home Screen*. It
> then opens like an app, full screen.

---

## Step 6 — Connect your real Nobø hub

When you are ready to control real heating:

1. Find the hub's **serial number** — 12 digits, on a sticker underneath.
2. Find its **IP address** in your router's device list.
3. In the web page: **Settings → Hub**, turn demo mode **off**, enter both,
   and save.

You will be signed out. Wait a few seconds and sign in again. The rooms are now
your real ones.

> The hub allows **two** connections at once, so the official Nobø app keeps
> working alongside this. You do not have to choose.

---

## Step 7 — Add sensors

Only if you have the Zigbee stick.

**If you skipped it at install time**, plug the stick in and run:

```
sudo bash /opt/nobo-control/scripts/install.sh --reconfigure
```

Answer the questions again — it will find the stick this time.

**To pair a sensor:**

1. **Settings → Sensors**, turn *Use contact sensors* on
2. Press **Add a sensor**
3. Take the sensor to **where it will actually live** — a window or door. A
   sensor paired next to the Pi may fail once you move it.
4. Press **Start pairing**
5. Hold the small button on the sensor about five seconds, until its light
   blinks
6. When it appears, give it a name and pick its room

Repeat for each sensor. Then open a room and choose what should happen when
something is left open — a warning, and optionally turning the heating down.

> **The battery reading will be blank at first.** That is normal and not a
> fault: these sensors report their battery on their own schedule, usually
> within an hour. It cannot be hurried.

---

## Looking after it

| I want to... | Type this |
| --- | --- |
| Get the latest version | `sudo bash /opt/nobo-control/scripts/update.sh` |
| Save settings and pairings | `sudo bash /opt/nobo-control/scripts/backup.sh` |
| See what it is doing | `sudo bash /opt/nobo-control/scripts/logs.sh` |
| Change the install answers | `sudo bash /opt/nobo-control/scripts/install.sh --reconfigure` |

**Back up before any change you are unsure about.** The backup includes your
rooms, schedules, accounts and — importantly — the Zigbee network. Without that
last part, moving to another Pi means re-pairing every sensor by hand, at every
window.

It starts itself after a power cut. Nothing to do.

---

## When something is wrong

**The page will not load.**

```
sudo systemctl status nobo-control
```

If it is not running:

```
sudo systemctl restart nobo-control
```

**It says the hub is offline.** Check the hub has power and a network light.
Check the IP address in **Settings → Hub** still matches your router — some
routers hand out a different one after a power cut. Give the hub a fixed
address in your router if this keeps happening.

**A sensor says "Offline".** Its battery may be dead, or it is too far from the
Pi. Zigbee reaches further with a mains-powered Zigbee device partway between
as a repeater — an IKEA smart plug works.

**I have forgotten the web password.**

```
cd /opt/nobo-control && sudo bash scripts/install.sh --reconfigure
```

Answer the questions again and set a new one. Your rooms and schedules are not
touched.

**Everything is broken and I want to start again.**

```
sudo systemctl stop nobo-control
sudo docker compose -f /opt/nobo-control/compose.yml down -v
sudo rm -rf /opt/nobo-control
```

Then start from Step 4. Take a backup first if there is anything worth keeping.

---

## Optional: a proper address instead of an IP

`http://nobohub.local:8000` works but is plain HTTP, and the password crosses
your network in the clear. On a home network that is usually accepted, but you
can have HTTPS with no certificate to buy and nothing opened to the internet.

See **"HTTPS on Your Own Network"** in the main `README.md`.

---

## What is running

Useful only if you are curious, or asking for help:

| | |
| --- | --- |
| The application | `/opt/nobo-control` |
| Your settings | `/opt/nobo-control/.env` |
| Rooms, schedules, accounts | a Docker volume, captured by `backup.sh` |
| Starts at boot via | `systemd`, service `nobo-control` |
| Sensors, when enabled | two extra containers, off unless asked for |
