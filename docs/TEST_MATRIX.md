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
ping nobopi.local               # works if the router publishes mDNS names
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
| 1.6 | Survives a **power cut** | 👤 | Pull the plug, wait 10s, plug in | Same as 1.5. This is the one that matters in a cabin. **Run 4 Sep 2026 — passed**, see below |
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
| 4.4 | ~~Off override~~ | — | **Not a test.** `off` is not a valid override: `POST /api/zones/{id}/override/off` returns 400, and the interface does not offer it. It is a *schedule* state only. Confirmed against the hub during commissioning. | — | — |
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

---

## What the first real run found

Run against a live hub (firmware 116, 7 zones, 11 heaters) in September 2026.
Every phase except 1.6 and parts of 5 was completed. **Ten defects were found,
and every one of them had passed the automated suite**, so they are worth
recording as a pattern rather than a list.

| Found | Why it was invisible before |
|---|---|
| Setting a temperature returned 500 | Demo stores set points as floats; the hub sends **strings**, and `'15' + 0.5` raised TypeError |
| A room excluded from Away never came home | Demo's Home blanket-assigns every zone; the hub cancels only the **global** override, leaving the zone override standing |
| Hub firmware showed "Unknown" | Read through `getattr(hub, 'hub_version', 'Unknown')` — pynobo has no such attribute, so the fallback was the only possible answer |
| Adding a device by serial always failed | Sent `X03` (pair over radio) for every model. Manual registration is `A01`, and an R80 RDC 700 has no pairing mode to enter |
| "Those controls are greyed out here" | A fixed sentence describing a restriction that had been removed |
| Week profile names came out as `Teknisk\xa0Rom` | Missing `decode_hub_name()` on one endpoint |
| Editing a hub built-in reported success | The hub accepts `U02` for its own schedules and silently ignores it |
| The temperature **minus** button did nothing | Both interfaces stepped 0.5; the hub stores whole degrees, so a half step down rounded back to where it started |
| The week editor could not be used on a phone | Every change rebuilt the row list, destroying the `<input>` mid-gesture — a time input fires `change` *while* a touch picker is being spun |
| **A zone set by hand ignored every global mode, for ever** | The suite asserted the opposite: a test named "a zone override outranks a global one" pinned the hub's ranking as the *app's* behaviour, so the stuck zone looked like the specification. Nobody had asked what should then release it |

Two lessons behind almost all of them:

**Demo mode was more forgiving than the hardware.** It stored floats where the
hub sends strings, and tidied up state the hub leaves alone. Both of the first
two defects passed their tests *because* of that. Demo mode has since been
changed to model the hub's actual behaviour, and the tests that had been passing
alongside those bugs now fail against the old code.

**A desktop browser is not the device.** The week editor bug was reachable only
by touch, and had been present for months. UI work now gets checked under touch
emulation, not just a desktop window.

**A passing test can be pinning down the wrong thing.** The stuck-zone defect had
a test sitting directly on top of it, asserting that a zone override survives a
global mode. That was a true statement about the hub and a dangerous one about
the app, and it made the bug look intended. When a test encodes hardware
behaviour, say what the *app* should do about that behaviour as well.

Three things were also established as facts rather than assumptions:

- A **zone override outranks the global override** on the hub. The away-exception
  feature depends on this and it had never been verified.
- **Copy-on-write for schedules works**, and 5.12's worst case — silently
  rescheduling the whole house — does not happen.
- **No temperature reaches this hub at all.** All 11 components report
  `tempsensor_for_zone_id = None` and `hub.temperatures` is empty. Of the 25
  models pynobo knows, only the SW4 has a thermometer. A blank room temperature
  is correct, not a fault.

---

## Test 1.6: the power cut

Run 4 September 2026 on the production Pi, with mains pulled for roughly two
minutes. This was the last item on the matrix and the one that matters most in a
cabin: an unattended box on a mountain will lose power, and it will lose it
without being asked politely first.

**It passed on every count.**

| Checked | Result |
|---|---|
| Boot | clean, 12:58:37 |
| Service started itself | `nobo-control` active at 12:59:13 — 36 s after boot, no login, no intervention |
| Containers | both `healthy` |
| Filesystem | root still `rw`, no ext4 recovery, orphan or corruption messages |
| Hub reconnected | yes, `connected: true` |
| Sockets on `:27779` | **exactly one** — no duplicate from the reconnect |
| Zones, set points, modes | identical to the snapshot taken before the cut |
| Devices | 11, same zone assignments |
| Week profiles | 6, same assignments |
| Away schedule and exceptions | identical |
| `intended_setpoints.json` | survived, and produced **no** false "changed outside app" flags |
| Follow-global flags | intact — they live on the hub, so the cut could not touch them |
| HTTPS from outside | the public hostname answered 200 |
| A write after the reboot | reached the hub and came back confirmed |

The set point guard deserves a specific mention. It persists what this app
intends, and a restart is exactly the moment a naive implementation would either
forget its intentions or, worse, wake up and decide that every zone had been
changed behind its back. Neither happened: every zone came back with
`setpoint_changed_outside: null`.

**One thing worth writing down for next time.** The baseline snapshots were
written to `/tmp`, which Ubuntu clears on boot, so the before-and-after diff had
to be done by eye against the printed output. Snapshots for a reboot test belong
somewhere that survives one — `~/nobo-baselines` on the Pi.

---

## Re-verifying that a heater can be removed and added back

4 September 2026, on the current build, because this is the test that decides
whether the application is sufficient on its own once the official Nobø app is
retired. If a user cannot get a receiver in and out from here, nothing else it
does matters very much.

The same heater as during commissioning: `160 001 145 133`, an R80 RDC 700 named
"Bunk Room by Bathroom" in zone 3, Downstairs Bedrooms.

| Step | Sent | Result |
|---|---|---|
| Remove it | `R01 160001145133` | 200; 11 devices → 10; gone from zone 3 |
| Add it back by serial, into zone 3, with its old name | `A01 160001145133 … zone_id=3` | 200 on the **first attempt** |
| Compare | — | Device record byte-identical to before: same name, model, zone |
| Whole system | — | Zones, devices, away settings, set points and hub identity all identical |
| Zone still works | `A03` Comfort, then Normal | Held Comfort, then released back to its schedule |

**`A01` alone was enough.** No `X03` pairing request, no fallback, no 504, and
nothing done at the heater — no pressing, no pairing mode, nobody standing in the
room. This is the behaviour the fix in "Register a device with A01, not a pairing
request" was aiming for, confirmed again on a later build.

That matters for every house of wall receivers, which is most of them. Nobø's
manual is explicit that the R80 RDC 700 and RXC 700 *"Require manual
registration"*, and the NTB-2R is likewise missing from the list of models that
answer an automatic search. For all three, typing the twelve digits is not a
workaround — it is the documented method, and the official app has to do the same
thing.

**Automatic search remains the one untested path** (5.5, 5.6). It is for battery
units that announce themselves over the radio — the Switch SW4, the TCU 700, Nobø
Sense — and no device in this house has a pairing mode to enter. Anyone who owns
one should try it while the official app is still available to fall back on.
