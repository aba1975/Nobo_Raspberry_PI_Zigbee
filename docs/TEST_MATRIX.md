# Test Matrix — Checking a Real Installation

This is the checklist for the things automated tests cannot reach: a real hub, a
real phone, a real browser, a real power cut.

The unit and integration suite already covers the application's own logic
against a fake hub. What it cannot cover is whether the hub behaves as
documented, whether the radio actually switches a heater on, whether the Pi
survives its own power supply, and how many TCP connections are open. That is
what this document is for.

## How to use it

Each test says who does it. Most need both of us:

| Symbol | Meaning |
|---|---|
| 👤 | You — needs a phone, a physical heater, or eyes on the room |
| 🤖 | The assistant — SSH, API calls, log and socket inspection |
| 👥 | Both, at the same time |

The 🤖 rows assume the assistant can reach the Pi. If it cannot, run them
yourself and paste the output — every one is a shell command or a `curl`, and
nothing here needs a tool you do not already have.

Work top to bottom. The order is deliberate: everything in phase 1 and 2 is
read-only, so if something is wrong we find out before anything has been
changed. Phase 3 onwards writes to the hub, and every step says how to undo it.

**Before starting anything that writes, take a snapshot** of the hub's full
state, and diff it at the end to prove the house was left as it was found. It
takes seconds and it is the difference between "I think we put it back" and
knowing.

Record results in the table at the bottom. A test that has never been run is
more useful marked "not run" than quietly assumed to pass.

> **If you are the assistant and starting cold:** read `CLAUDE.md` first,
> particularly "Where This Has Got To". It says which paths have been proven
> against real hardware and which have never run outside a fake — Phase 5 below
> is entirely in the second category, and should not be described as verified
> until it has been done.

---

## Before you start — finding the Pi

The Pi under test is usually not the one you were last working on, and at a
cabin its address is whatever the router handed out. Work this out first, before
anything else, because everything below needs it.

**On the Pi itself**, if you have a keyboard and screen on it:

```bash
hostname -I          # its addresses, the first is normally the one you want
ip route get 1.1.1.1 # the address it uses to reach the world, and on which interface
```

**From another machine on the same network**, if the Pi is headless:

```bash
ping nobohub.local              # works if the router publishes mDNS names
nmap -sn 192.168.1.0/24         # or whatever your subnet is
arp -a | findstr /i "b8:27:eb dc:a6:32 e4:5f:01"   # Windows; Raspberry Pi MAC prefixes
```

Confirm you have the right machine before trusting it:

```bash
curl -s http://<ip>:8000/api/health
# {"status":"ok", ...}  — the only endpoint that answers without a login
```

If HTTPS is set up on that Pi, port 8000 will be closed on purpose and the
address is `https://<name>/api/health` instead.

Then note these down, because every later phase refers to them:

```
Pi address:        ______________
SSH user:          ______________
Web login:         ______________
Hub IP:            ______________
Hub serial:        ______________
Which Pi is this?  test / production
```

## Then — make sure it is running the current code

Easy to skip, and it invalidates everything below. A Pi left on an old branch
pulls that branch and reports success, so "I updated it" is not evidence.

```bash
cd /opt/nobo-control
git rev-parse --abbrev-ref HEAD     # should be main
git log --oneline -1                # and match the newest commit on GitHub
```

If it is on something else, move it across — your accounts, schedules, system
name and certificates are all in Docker volumes, so only the code changes:

```bash
sudo git fetch origin
sudo git checkout -B main origin/main
sudo git branch --set-upstream-to=origin/main main
sudo bash scripts/update.sh
```

Then confirm the suite passes on that machine before testing hardware with it.
The command is in the README under Testing; include `nodejs` or fifteen tests
skip, which reads like a pass:

```
654 passed, 3 skipped
```

Anything else and stop — a failing suite makes every result below ambiguous.

> **Make sure you are testing the machine you think you are.** If you run more
> than one Pi, they serve identical-looking pages, and a test that "passed" on
> the wrong one is worse than a test not run. The name at the top of the screen
> is the quickest way to tell them apart — see
> [Naming Your System](../README.md#naming-your-system). Give them different
> names before you start.

**A static address for the hub is worth setting up** while you are at the
router. The hub re-runs DHCP after its roughly-18-hourly reboot, and if its
address moves, the Pi cannot find it again until someone updates the setting.
Test 6.6 covers this.

## Phase 0 — Ground rules

- **The heating is real.** These tests change the temperature of a house that
  someone may be living in. Do not run phase 4 in January on an occupied cabin.
- **Pick a low-stakes zone.** A hallway, a technical room, a spare bedroom.
  Avoid a bathroom with underfloor heating (slow to recover) and any room with
  a frost risk if you leave it Off by mistake.
- **Know how to undo it from the phone.** If we lose the Pi mid-test, the
  official app is the fallback for putting the house back. Have it installed and
  logged in before starting.
- **One writer at a time.** If we are both changing things, we cannot tell whose
  change caused what.

---

## Phase 1 — The Pi itself (read-only, safe any time)

| # | Test | Who | How | Pass looks like |
|---|---|---|---|---|
| 1.1 | Service is enabled and running | 🤖 | `systemctl is-enabled nobo-control; systemctl is-active nobo-control` | `enabled` and `active` |
| 1.2 | Container is healthy, not restarting | 🤖 | `docker ps` | `Up … (healthy)`, restart count 0 |
| 1.3 | Health endpoint answers | 🤖 | `curl -s localhost:8000/api/health` | `{"status":"ok", …}` |
| 1.4 | Correct branch and version deployed | 🤖 | `git log --oneline -1` | Matches what we intended to deploy |
| 1.5 | Survives a reboot unattended | 👥 | `sudo reboot`, then wait | App answers again within ~60s, no login needed, data intact |
| 1.6 | Survives a **power cut** | 👤 | Pull the plug, wait 10s, plug in | Same as 1.5. This is the one that matters in a cabin |
| 1.7 | Disk is not filling up | 🤖 | `df -h /` | Comfortably under 80% |
| 1.8 | Clock is correct and in the right timezone | 🤖 | `timedatectl` | Correct local time — schedules are wall-clock, so a wrong clock heats at the wrong hour |

> **1.6 is the one people skip and regret.** A graceful `reboot` flushes the
> filesystem; a power cut does not. It is the honest test for a cabin, where the
> power will eventually go out while nobody is there.

---

## Phase 2 — Talking to the hub (read-only, safe any time)

| # | Test | Who | How | Pass looks like |
|---|---|---|---|---|
| 2.1 | Hub is reachable on the network | 🤖 | `ping`, `nc -vz <hub-ip> 27779` | Port open |
| 2.2 | App reports connected | 🤖 | `GET /api/status` | `connected: true`, `demo_mode: false` |
| 2.3 | Zones match the real house | 👥 | Open the web UI | Room names and count are the real ones, not the demo house |
| 2.4 | Devices match the real house | 👥 | Each room in the UI | Every heater you own appears, with the right serial and model |
| 2.5 | Non-ASCII names are correct | 👤 | Look at the room list | `æ ø å` render properly — no `Ã¸`, no `\xa0` |
| 2.6 | Setpoints match the app | 👥 | Compare UI with the phone app, room by room | Same comfort and eco values |
| 2.7 | Exactly **one** connection to the hub | 🤖 | `ss -tn \| grep 27779` | Exactly one `ESTAB`. Two means a leak |
| 2.8 | Connection survives idle | 🤖 | Leave it 15 min, re-check 2.2 and 2.7 | Still connected, still one socket. Proves the keep-alive works — the hub drops silent clients after 30s |
| 2.9 | Nothing is being written | 🤖 | Activity log, Settings → Activity log | All hub entries say `received`. No `sent` we did not cause |

---

## Phase 3 — Sync between the Pi and the phone (reversible, low risk)

This proves the two-connection behaviour on your own hub. Each test is a change
and an immediate change back.

| # | Test | Who | How | Pass looks like |
|---|---|---|---|---|
| 3.1 | Phone → Pi | 👥 | You change global mode in the app. I watch. | Pi shows it within ~10s, **without refreshing** |
| 3.2 | Pi → phone | 👥 | I set a room override. You watch the app. | App shows it within ~10s |
| 3.3 | Pi → browser, live | 👤 | Open the UI on two devices, change something on one | The other updates on its own (WebSocket) |
| 3.4 | Both connected at once | 👥 | Do 3.1 with the app open and the Pi connected | Neither is kicked off. This is the claim in the README |
| 3.5 | Physical switch → both | 👤 | Press a Nobø Switch, if you have one | Change appears on the Pi and the app |
| 3.6 | Phone rejoins after the Pi | 👤 | Close the app, reopen it | Reconnects normally, shows current state |

---

## Phase 4 — Control (writes to the hub — pick a low-stakes room)

Do these one at a time and confirm each before moving on.

| # | Test | Who | How | Pass looks like | Undo |
|---|---|---|---|---|---|
| 4.1 | Comfort override | 👥 | Set the test room to Comfort | Mode changes in UI and app | Set back to Schedule |
| 4.2 | The heater actually responds | 👤 | Stand at the heater during 4.1 | It clicks / warms. **This is the only test that proves the radio works end to end** | — |
| 4.3 | Eco override | 👥 | Set Eco | Mode and setpoint change | Back to Schedule |
| 4.4 | Off override | 👥 | Set Off | Heater stops | Back to Schedule |
| 4.5 | Back to Schedule | 👥 | Clear the override | Returns to whatever the week profile says now | — |
| 4.6 | Change comfort temperature | 👥 | Set the test room's comfort to a distinct value like 23 | Shows 23 in UI and app | Set it back to the original |
| 4.7 | Global Away | 👥 | Set the whole house Away | Every room follows | Set back to Home |
| 4.8 | Global Home | 👥 | Back to Home | Every room returns | — |
| 4.9 | Rooms excluded from Away | 👤 | Mark a room as excluded, then set Away | That room keeps its own mode | Restore |
| 4.10 | Override survives a restart | 🤖 | Set an override, restart the service, re-read | Still there — the hub holds it, not the Pi | Clear it |

> **4.2 is the most important test in this document.** Everything else confirms
> that a message reached the hub. Only standing next to the heater confirms the
> hub reached the heater. It is the one step no amount of software testing can
> replace.

---

## Phase 5 — Zones, devices, schedules (changes hub configuration)

**This is the part that has never been run against real hardware.** Discovery,
pairing and week profile edits are implemented from the protocol document, not
from observed behaviour. Expect to find something.

Do these when you have time to undo them, not five minutes before leaving.

| # | Test | Who | How | Pass looks like | Undo |
|---|---|---|---|---|---|
| 5.1 | Create a zone | 👥 | Add "Test Zone" | Appears, gets a hub-assigned id | Delete it |
| 5.2 | Rename a zone | 👥 | Rename it, with an `ø` in the name | Name correct in UI **and phone app** | Rename back |
| 5.3 | Manual device registration | 👤 | Add a heater by serial number | Right model and image recognised | Remove it |
| 5.4 | Move a device between zones | 👥 | Move one heater to Test Zone | Moves in both UI and app | Move it back |
| 5.5 | **Automatic discovery** | 👤 | Put a device in pairing mode, run Search | It is found | Do not pair yet |
| 5.6 | **Pair a discovered device** | 👤 | Pair it | Added to the chosen zone | Remove it |
| 5.7 | Rename a device | 👥 | Rename a heater | Correct in both | Rename back |
| 5.8 | Delete an empty zone | 🤖 | Delete Test Zone once empty | Gone | — |
| 5.9 | Delete a zone with devices is refused | 🤖 | Try it | Clear error, nothing deleted | — |
| 5.10 | **Read a week profile** | 👥 | Open a room's schedule | Matches the app's schedule exactly | — |
| 5.11 | **Edit a week profile** | 👥 | Change one time block on the test room | Correct in the app too | Restore |
| 5.12 | Schedule copy-on-write | 👥 | Edit a schedule shared by several rooms | **Only that room changes.** Others keep theirs | Restore |
| 5.13 | Scheduled away | 👥 | Set an away period a few minutes out | Applies at the right wall-clock time | Cancel it |

> **5.12 is the one with the worst failure mode.** Week profiles are shared
> objects on the hub, and every zone starts on the same factory profile. If the
> copy-on-write logic is wrong, editing one room silently reschedules the whole
> house — and you would not notice until rooms started heating at the wrong time
> days later. Check the other rooms explicitly, in the app, not just in the UI.

---

## Phase 6 — Failure and recovery

| # | Test | Who | How | Pass looks like |
|---|---|---|---|---|
| 6.1 | Hub unplugged | 👤 | Unplug the hub for 2 min | Pi shows disconnected, retries with backoff, does not crash |
| 6.2 | Hub back | 👤 | Plug it in | Reconnects on its own within ~1 min |
| 6.3 | **No connection leak after reconnecting** | 🤖 | `ss -tn \| grep 27779` after 6.2 | Still exactly one socket |
| 6.4 | Wi-Fi drops | 👤 | Disable Wi-Fi briefly | Recovers on its own |
| 6.5 | Hub's nightly reboot | 🤖 | Check logs after 24h | Reconnects each time, one socket after |
| 6.6 | Hub IP changed by DHCP | 👥 | Reboot the router, or change the lease | Either still works, or gives a clear error. **A static lease for the hub is worth setting up** |
| 6.7 | Wrong serial rejected cleanly | 🤖 | Enter a wrong serial | Clear error, no crash, easy to correct |
| 6.8 | Switch to demo and back | 🤖 | Toggle twice | Works both ways, **one socket** at the end, demo data intact |

---

## Phase 7 — The interface

| # | Test | Who | How | Pass looks like |
|---|---|---|---|---|
| 7.1 | Works on your phone | 👤 | Open the UI on a phone browser | Readable, tappable, no sideways scrolling |
| 7.2 | Works on a computer | 👤 | Desktop browser | Laid out sensibly |
| 7.3 | Add to home screen | 👤 | Install as a web app | Correct name and icon, opens full screen |
| 7.4 | Every button does something | 👤 | Press all of them | No dead buttons. *This has caught real bugs* |
| 7.5 | Mode colours match | 👤 | Compare a room's buttons with its current mode | Colours agree |
| 7.6 | Activity log | 👤 | Settings → Activity log | Shows recent changes. Empty after a restart is normal — it is in memory |
| 7.7 | Rooms without sensors | 👤 | Look at an R80 / NTB-2R room | Setpoint shown, no invented room temperature |
| 7.8 | Rooms with sensors | 👤 | If you own one | Measured temperature shown |
| 7.9 | Login required | 🤖 | Open the API logged out | Redirected to login |
| 7.10 | Old page after an update | 👤 | Update, then reload | New version loads — no stale buttons that do nothing |

---

## Result sheet

Copy this and fill it in. Date each run — a pass from six months and four
updates ago is not a pass.

```
Date:            ______________
Version tested:  ______________  (git log --oneline -1)
Pi address:      ______________
Hub serial:      ______________
Tester(s):       ______________

Phase 1  Pi              ___ / 8    notes:
Phase 2  Hub read        ___ / 9    notes:
Phase 3  Sync            ___ / 6    notes:
Phase 4  Control         ___ / 10   notes:
Phase 5  Configuration   ___ / 13   notes:
Phase 6  Recovery        ___ / 8    notes:
Phase 7  Interface       ___ / 10   notes:

Hub state restored to starting point?   yes / no
Anything left changed:                  ______________
```

## The short version

If you only have twenty minutes: **1.3, 2.2, 2.7, 3.1, 4.1, 4.2, 6.3.**

That is: the app is up, the hub is connected, no connections are leaking, sync
works both ways, a room responds, **a heater physically responds**, and nothing
leaks after a reconnect. Those seven cover the failure modes that actually bite.
