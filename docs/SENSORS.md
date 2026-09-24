# Contact sensor provider boundary

Contact sensors are optional and off by default. The first provider is a
persisted simulator for demo mode; it does not require a Zigbee dongle, MQTT
broker, Zigbee2MQTT, or any additional service.

The application consumes normalized contact snapshots rather than MQTT topics
or vendor payloads. A provider supplies:

- an opaque stable provider ID and application sensor ID;
- a display name, door/window type, and assigned Nobø zone ID;
- contact state (`open`, `closed`, or `unknown`);
- availability, battery percentage, signal strength, change time, and
  last-seen time;
- async lifecycle and CRUD/pairing operations;
- a callback whenever a snapshot changes.

`sensor_provider.py` defines that contract. `sensor_simulated.py` and
`sensor_zigbee2mqtt.py` implement it. The API, warning aggregation, WebSocket
payloads, UI, persistence policy, and heating automation do not depend on either
one's storage format.

## The real provider: Zigbee2MQTT out of process

**Decision: Zigbee2MQTT and a broker run as their own containers, and the
application is a pure MQTT consumer. The radio stack does not run inside
`server.py`.**

`sensor_zigbee2mqtt.py` implements this against the `ContactSensorProvider`
contract; `sensor_mqtt.py` carries the transport. The MQTT client library is
imported lazily, so a Nobø-only installation neither needs it nor loads it, and
selecting the provider without it gives a clear instruction rather than an
import traceback.

Select it with `provider: "zigbee2mqtt"` in the sensor settings. The broker
address comes from `NOBO_MQTT_URL` (default `mqtt://127.0.0.1:1883`) and the
topic root from `NOBO_MQTT_BASE_TOPIC` (default `zigbee2mqtt`). Neither is
stored in sensor records, and neither is returned by the API.

The alternative — zigpy in process, no broker, one less moving part — was
rejected on the strength of this repository's own history. `server.py` already
owns a dedicated event loop for the hub socket, and all four rules in "Talking
to a Real Hub" exist because that one connection was got wrong. A second radio
stack sharing that process can take the heating down with it, and the heating is
the part that matters when the building is empty in winter. Out of process, a
Zigbee failure is contained: sensors report `available: false`, which the UI
already states honestly, and the Nobø control keeps running.

Zigbee2MQTT also owns the device handling. Aqara contact sensors are notoriously
non-standard, and its converters are a large body of work that would otherwise
have to be reimplemented against hardware, blind.

### What the provider translates

| Zigbee2MQTT | Snapshot field |
| --- | --- |
| `zigbee2mqtt/<name>` → `contact` | `state` — **`contact: true` means CLOSED.** Inverting this is the single easiest way to make the whole feature backwards while looking plausible, so it is asserted by test |
| `zigbee2mqtt/<name>/availability` → `state` | `available` |
| `zigbee2mqtt/<name>` → `battery` | `battery` |
| `zigbee2mqtt/<name>` → `linkquality` | `link_quality` — Zigbee LQI, 0 to 255 |
| `zigbee2mqtt/bridge/devices` | the device list, and `ieee_address` as the identity |

Identity is the **IEEE address**, not a generated UUID and not the friendly
name. It survives renaming, re-pairing and a Zigbee2MQTT restart, so a sensor
that is removed and re-paired returns to the room it was already assigned to.
Name, door/window type and zone assignment stay in this application — the mesh
has no concept of a room, and no device reports whether it is on a door or a
window. Those remain a choice made at pairing.

Retained messages mean a restart gets current state immediately rather than
waiting for something to move.

### One contract change was needed

`pair()` returning a `ContactSnapshot` is achievable for a simulator and not for
a radio: a real join takes anywhere from seconds to never. Pairing is therefore
two steps — `begin_pairing()` opens the permit-join window, and the device
arrives as a `CREATED` event through `subscribe()`. The UI already updates over
the WebSocket, so it can show a "searching" state and then the device. Naming,
type and room are collected *after* the device joins, which is also the better
sequence, because until it joins there is nothing to name. Calling `pair()` or
`create()` on the Zigbee provider raises rather than inventing a device.

A sensor that has joined but has not yet reported is `unknown`, not `closed`.
Claiming closed would mean the left-open warning and the heating rule both
trusted a fact nobody reported.

Removal asks Zigbee2MQTT and waits for its `device_leave` event rather than
treating the request as the outcome, which would hide a sensor that is still on
the mesh.

### Verified against real hardware, 15 September 2026

An Aqara MCCGQ11LM joined a SONOFF ZBDongle-P (Z-Stack coordinator build
20250321) on channel 15 and sent exactly these payloads:

```
zigbee2mqtt/0x00158d008c8bc4f2/availability {"state":"online"}
zigbee2mqtt/0x00158d008c8bc4f2 {"contact":true,"linkquality":98}    magnet on
zigbee2mqtt/0x00158d008c8bc4f2 {"contact":false,"linkquality":105}  magnet off
zigbee2mqtt/0x00158d008c8bc4f2 {"contact":true,"linkquality":98}    back on
```

That settles the inversion from the hardware rather than from the
documentation: the device's own definition reads *"Indicates if the contact is
closed (= true) or open (= false)"*, with `value_on: false` and
`value_off: true`. `tests/fake_zigbee2mqtt.py` now carries that definition
verbatim, and `test_the_payloads_a_real_aqara_sent` replays the captured
payloads.

Two things worth knowing from it:

**A report carries no battery, and it cannot be asked for.** Zigbee2MQTT's
definition says battery "can take up to 24 hours before reported", and
requesting it explicitly is refused outright:

```
zigbee2mqtt/<device>/get {"battery":""}
  -> error: No converter available for 'battery' on '0x00158d008c8bc4f2'
```

On the MCCGQ11LM it is report-only, so a newly paired sensor simply has no
level until the device volunteers one. Measured on this hardware: both sensors
were interviewed at 09:11 and first reported battery at 10:02 — **51 minutes**.

The provider therefore leaves it `null`, and the interface names the gap rather
than rendering an empty space, which reads as a broken sensor next to one
showing a percentage. Substituting the last value, or showing a warning, would
invent a reading or cry wolf on every new sensor.

**A re-pairing sensor emits `device_leave` immediately before `device_joined`.**
Metadata is therefore kept when a device leaves, so it returns to its room and
name rather than arriving anonymous.

### Signal strength, and what it is not

Every report carries `linkquality`, so unlike the battery it is known from the
device's very first message. It is Zigbee **LQI**, an integer from 0 to 255
scored by the receiver of the *last hop* — a sensor reporting through a
repeater is being graded on the short leg to that repeater, not on its distance
from the Pi.

The raw number means nothing to a person, and its one practical use is
answering "does this spot need a repeater?", so the interface shows a verdict
and keeps the number in the tooltip: **100 or more good, 50 to 99 fair, under
50 weak**, following the usual Zigbee2MQTT reading of the scale. Only *weak* is
coloured as a problem; a fair link is a hint.

**It is not the dBm figure on the box.** Aqara and Xiaomi publish numbers such
as ≤ 9.99 dBm and ≤ 10.5 dBm; those are each device's maximum *transmit power*,
a fixed hardware specification identical for every unit of that model. They say
nothing about a particular sensor in a particular cupboard, and displaying one
would be a constant dressed up as a measurement. LQI is the reading that
actually varies with where the sensor is put.

Neither LQI nor RSSI is available for a device the coordinator cannot currently
hear, which is why the value is remembered rather than blanked — see below.

**Readings are carried across a restart; the contact state is not.** Battery
and signal are the last measurements taken, and a sleeping contact sensor may
not speak again for hours, so discarding them left both blank for most of a day
after every update. They are persisted in the Zigbee metadata file alongside
`last_seen`, and the interface already says how long ago the sensor was heard
from, so a stale reading is never presented as a live one. Whether a window is
open *now* is a different kind of fact — a safety question — and it still
starts `unknown` until the hardware says otherwise.

Still unverified: mesh range and reliability over distance, Aqara re-parenting
onto a repeater, battery-reporting accuracy, and behaviour over days rather than
minutes. Link quality was 98–105 of 255 with the sensor near the coordinator,
which says nothing about the far end of the building.

`tests/fake_zigbee2mqtt.py` fakes Zigbee2MQTT's *topic* contract, which is the
part this application can get wrong; the MQTT wire protocol underneath is the
client library's responsibility and is deliberately not reimplemented. The same
caveat applies as to `fake_hub.py`: it proves a message was understood, not that
a radio delivered it.

### Extending range

Three settings and one purchase, in the order they are worth doing.

**Only mains-powered devices repeat.** Aqara and Xiaomi contact sensors are
Zigbee *end devices*: they sleep between reports and route nothing, ever. A
network of a coordinator and nothing but battery sensors has no mesh in it —
it is a hub and spokes, and every distant sensor is weak for the same reason.
Adding a mains-powered router (a smart plug or an in-wall relay) roughly
*halves* the distance any one hop has to cross, which buys far more than any
amount of adjusting the coordinator. Place it between the dongle and the weak
area, not in the weak area.

**A repeater needs no code here, and gets none.** It joins Zigbee2MQTT and
starts relaying immediately; nothing in this application has to understand it
for the range to improve. What the application does is narrower, and bounded
deliberately:

- pairing reports it as **"Repeater added"** rather than "that is not a contact
  sensor", because somebody who bought a plug to extend the mesh has succeeded
  at exactly what they set out to do, and a rejection message invites them to
  take it back to the shop;
- Settings lists what is repeating, and says plainly when nothing is;
- it is never counted as a sensor, never given a room, and **not switched on or
  off from here**. Controlling a plug — tying it to Away, say — is a different
  feature with its own ownership and restart questions, and pretending
  otherwise in the interface would promise a switch that does not exist.

The "no repeaters" note is shown only once at least one device is paired. On an
empty installation it would be advice to go shopping for nothing.

#### A spare dongle makes the strongest repeater

A second ZBDongle-P flashed with *router* firmware is the best relay this
project has a use for: the same CC2652P radio and the same amplifier as the
coordinator, rather than the modest radio inside a smart plug.

**It needs no computer once flashed.** USB supplies power and nothing else —
no host, no data, no drivers. A phone charger, a spare port on a television, a
USB socket in the wall: anything that stays on. It is a self-contained Zigbee
router the moment it has 5 V.

Flashing it does need a machine once, and the demo Pi will do. There is a
script, because the two ways this goes wrong are both unrecoverable by ordinary
means and neither announces itself:

```bash
sudo bash scripts/zigbee-flash-router.sh
```

It finds the spare, **refuses to touch the adapter this system is using as its
coordinator** — that would destroy the running network, since every sensor is
paired to it and the network key lives on it — fetches the right image for this
exact adapter, checks it against the digest GitHub publishes, and asks you to
type the word `flash` before writing anything.

There is deliberately **no flag to override the coordinator refusal**, and the
firmware is deliberately **not a parameter**. Both are the decisions that go
wrong, and a flag would eventually be pasted from a forum by somebody in a
hurry. If the spare really is the only stick present, unplug the working one.

| | |
| --- | --- |
| Firmware | `CC1352P2_CC2652P_launchpad_router_*.zip` |
| Not | the `_coordinator_` image, and not the `..._other_...` build |
| Auto-BSL | **yes** on this adapter — no button-holding needed to enter the bootloader |
| Tool | `cc2538-bsl`, fetched at a pinned commit rather than a branch tip |
| Needs | `python3-serial` and `python3-intelhex` |
| Pairing | automatic after reflashing, with the network open to join |
| Factory reset | a single press of the button on the stick |

The firmware choice is from Koenkk's own adapter table, which lists exactly one
image for the "SONOFF Zigbee 3.0 USB Dongle Plus by ITead" and notes the RF
switch pin that drives the 20 dBm amplifier. Flashing the wrong image can lock
the bootloader, so it is worth reading the row rather than guessing.

Reversible: write the coordinator image back and it is a coordinator again.

Worth knowing if the spares are also earmarked for building more installations:
a dongle **ships as a coordinator**, so one straight out of the bag needs no
flashing at all to run a Pi. Only a stick that has already been converted to a
router has to be put back, and that is the same procedure with the
`_coordinator_` image.

Two caveats. **A router starts at 9 dBm, not 20.** `NOBO_ZIGBEE_TRANSMIT_POWER`
configures the coordinator and nothing else. The router firmware (from its
20221102 build) takes its own setting over the air instead: Zigbee2MQTT knows a
flashed ZBDongle-P as `ti.router` and exposes `transmit_power`, -20 to 20 dBm,
which the firmware writes to its own non-volatile memory, so it survives a power
cut. Once it has joined, from the Pi:

```bash
docker exec nobo-mosquitto mosquitto_pub -h 127.0.0.1 \
  -t 'zigbee2mqtt/0x<its address>/set' -m '{"transmit_power": 20}'
```

That comes from Zigbee2MQTT's device definition and the firmware's source, and
has not yet been run here. Read it back with `/get` and `{"transmit_power": ""}`.

And **use a decent USB supply**: a cheap charger is a noisy thing to sit a
2.4 GHz receiver on top of, which is the same reasoning that put the
coordinator on an extension lead in the first place.

`scripts/zigbee-map.sh` answers whether there is a router at all, and which
parent each sensor actually chose. Link quality in the interface cannot: it
grades the last hop, so a sensor reporting through a repeater looks healthy no
matter how far it is from the Pi.

Run against the demo Pi's real bridge on 17 September 2026, twice. First on an
empty network — every sensor having just been unpaired — which it reported as
such. Then with two Aqara sensors paired, where it named both as children of
the coordinator and scored them 82 and 121.

That second run is what proves the parent-and-quality half, and the cross-check
is worth recording: the interface, reading the LQI stamped on each *report*,
independently gave 65 and 116 for the same two devices. Same ordering, same
bands, from two sources that share no code — the map reads each router's
neighbour table, the interface reads what arrives over MQTT. The neighbour
table runs a little high and a little stale, so treat the two as agreeing
rather than as one contradicting the other.

What it still has not seen is **a network with a router in it**. Every device
here is a battery end device, so "which parent did it choose" has so far only
ever been answered "the coordinator".

**Transmit power** is set to 20 dBm by `NOBO_ZIGBEE_TRANSMIT_POWER`. The
ZBDongle-P is a CC2652P with an amplifier, and its firmware default is 5, so
this is free range that was previously left on the table. It makes the
*coordinator* louder and not the sensors, which still answer at around 10 dBm —
so it rescues a link that was marginal in one direction only, and cannot make a
sensor audible that the coordinator simply cannot hear.

**Do not expect the link quality numbers to move.** LQI is measured by whichever
radio *received* the frame, and every number this application displays came from
a sensor's report, so all of them describe the sensor→coordinator direction.
Raising what the coordinator transmits cannot change any of them, and somebody
comparing before and after will reasonably conclude the setting did nothing.
What it actually buys is the other direction: acknowledgements the sensor has to
hear or it retransmits and drains its battery, the interview during pairing, and
any command sent *to* a device. Judge it by devices staying joined, not by a
bigger number.

Two things follow from the same fact. **Antenna gain is worth more than
transmit power**, because an antenna is reciprocal — it improves receive as
well as transmit, and receive is the side that is actually limited here. And
**a high-gain omni is not automatically better**: gain is bought by flattening
the radiation pattern into a disc, which is the wrong shape for a building with
a floor above the dongle. In a single-storey spread, more gain helps; across
floors, a modest antenna standing vertically usually beats a tall thin one.

There is nothing else on the dongle to adjust. Koenkk's coordinator firmware
exposes no antenna selection — the ZBDongle-P has only the external SMA — and
the transmit power above is the whole of the radio configuration. Check the
firmware is current (`bridge/info` → `coordinator.meta.revision`, against
<https://github.com/Koenkk/Z-Stack-firmware/releases>) and leave it alone
otherwise; flashing carries real risk and buys nothing on its own.

**What cannot be read back**, and should not be claimed: neither the achieved
transmit power nor which firmware variant is flashed is exposed by anything.
`CC1352P2_CC2652P_launchpad_*` is the build for the ZBDongle-P and drives the
amplifier; `..._other_*` is for CC2652P boards wired differently. Both report
an identical version string, so the only evidence that 20 dBm took effect is
that the adapter did not complain.

Which one *should* be there is not in doubt, at least: Koenkk's adapter table
lists exactly one image for the "SONOFF Zigbee 3.0 USB Dongle Plus by ITead",
and names the pin it uses to drive the 20 dBm amplifier. Only a deliberate
mistake would have put the other build on it.

**Channel** is `NOBO_ZIGBEE_CHANNEL`, and is deliberately empty by default: an
empty `ZIGBEE2MQTT_CONFIG_*` variable is ignored, so an existing network keeps
the channel it was formed on. Choose it before pairing anything. See the survey
under "Test rig" below for how, and treat the answer as permanent.

**These variables reach Zigbee2MQTT only when it writes its configuration
file** — on first run, and whenever a setting is persisted afterwards. They are
not re-read from the environment on an ordinary restart. On an installation
that already has a `configuration.yaml` in its volume, check rather than assume:

```bash
sudo docker exec nobo-zigbee2mqtt \
    grep -E 'channel|transmit_power' /app/data/configuration.yaml
```

If the value has not landed, edit that file in place and restart the container.

**Verified on the demo Pi, 17 September 2026.** An ordinary
`sudo bash scripts/update.sh` against Zigbee2MQTT 2.14.1 did rewrite
`configuration.yaml` — the file's mtime moved to the moment the container came
back — and `transmit_power: 20` was in it afterwards, with no complaint from
the adapter (a CC2652P running Z-Stack 20250321). So on this version the
variable does arrive without intervention. Check anyway: the code path that
writes it is not one this project controls, and the failure is silent.

The same check produced the argument for `NOBO_ZIGBEE_CHANNEL` better than any
reasoning did. That Pi was found already on **channel 15** — correct, matching
the survey below, and present only because somebody had once edited the file
inside the volume by hand. Nothing in the repository knew, so a second
installation built from this source would have formed its network on 11 while
the first sat on 15, and the only record of the decision was a paragraph of
prose. That is exactly the gap the variable closes.

**Placement beats all of it.** The dongle wants a short, *shielded* extension
(0.5–1 m is plenty) on a **USB 2** port, as far as the cable allows from the
Pi's USB 3 sockets, any attached SSD, and the Wi-Fi router. USB 3 radiates
broadband noise straight through 2.4 GHz, and a dongle sitting against a Pi's
own Wi-Fi radio is being jammed by the machine it is plugged into. This costs
nothing to try and is frequently the whole problem.

### Test rig

A rig lives on the demo Pi at `/opt/zigbee-test`, deliberately **outside** the
application's `compose.yml` so that nothing about the heating stack changes
until the radio is proven. It runs Mosquitto bound to loopback only and
Zigbee2MQTT with its frontend behind a token. Tear it down with
`cd /opt/zigbee-test && sudo docker compose down`.

The Zigbee channel is **15**, chosen against a measured survey rather than the
default: the site has 16 access points on Wi-Fi 1/6/11 with seven on channel 11,
including the Pi's own radio, which sits centimetres from the dongle. Zigbee 15
(2425 MHz) occupies the designed gap between Wi-Fi 1 and 6 and is furthest from
that dominant adjacent cluster. The default, channel 11, would have sat inside
Wi-Fi 1. **This choice is effectively permanent** — changing it later means
re-pairing every sleepy device.

That survey is the reason `NOBO_ZIGBEE_CHANNEL` exists. Until it did, the
decision was recorded here but enforced nowhere, and a fresh installation of
the application stack would silently form its network on the channel this
paragraph rejects.

When Zigbee2MQTT moves into the application stack it must go behind a Compose
profile, as `tls` already is, so a Nobø-only installation starts nothing extra.
Its data directory holds the network key and device database; losing it means
re-pairing everything, so it belongs in `scripts/backup.sh`. Note also that the
application container runs as uid 1001 and `nobo` is not in `dialout`, so device
passthrough and group handling need attention at that point.

## Running it

Both extra containers sit behind a Compose profile, as `tls` does, so a
Nobø-only installation starts nothing and needs no dongle:

```
COMPOSE_PROFILES=zigbee
NOBO_ZIGBEE_ADAPTER=/dev/serial/by-id/usb-...-if00-port0
```

Always the by-id path, never `/dev/ttyUSB0`: USB numbering is not stable across
reboots, and pointing a coordinator at the wrong adapter is not a failure that
announces itself. `ZIGBEE2MQTT_TAG` is pinned so a rebuild cannot change the
Zigbee stack under a working mesh.

Then choose the provider in Settings. Both are allowed beside either hub.
`zigbee2mqtt` beside a demo hub is what lets real sensors be tested without
touching a building's heating; `simulated` beside a real hub is how the feature
is tried before a stick arrives, and there the automation stands down
(`BlockReason.DEMO_SENSORS`) so invented contacts warn but never change a real
heater. Choosing `zigbee2mqtt` is refused with a 503 and the reason if the
Zigbee stack is not answering, and nothing changes.

**Choosing `zigbee2mqtt` is checked before it is accepted.** This process
cannot see the USB stick — the adapter is passed to the *zigbee2mqtt*
container, not to this one — so what is checked instead is whether the stack
that owns the radio is alive, which is the same question in every way that
matters: a stick in a Pi with Zigbee2MQTT stopped is exactly as useless as no
stick. `probe_zigbee2mqtt()` subscribes to the retained `bridge/state` and
distinguishes three answers: no broker at all, a broker with nothing behind it,
and a Zigbee2MQTT that is there.

Without it, switching to real sensors on a Pi with no dongle simply *succeeded*
— it persisted, Settings said On, and the MQTT client then retried a refused
connection every five seconds for ever, across reboots, with nothing on screen
to explain why.

The refusal is a **503, not a 501**: Zigbee2MQTT starting after this
application is ordinary boot ordering, not a permanent incapability, and the
same request a minute later should work. It runs **only when the provider is
being taken up** — never on an ordinary save, so editing a zone's rule while
the radio restarts is not refused, and never on the way *off*, so a missing
radio cannot trap somebody with the feature enabled.

| | |
| --- | --- |
| `GET /api/sensors/zigbee-check` | Whether real sensors could be switched on right now: `usable`, `broker_reachable`, `bridge_online`, `detail`. Admin only. |

That check is its own request rather than a field on the settings response,
because it talks to the broker and waits, and the settings are read on every
page load. The interface asks it when somebody opens the choice, so Settings
can grey out the real option and say why, rather than offering it and then
refusing. Turning sensors on therefore asks *which kind*: demo and real are two
different systems rather than two settings of one, and they do not share a
sensor list.

`probe_zigbee_stack()` in `server.py` is a module-level seam beside
`create_provider` for the same reason that one is. **A test that installs a
fake provider must patch both or neither** — patching only the factory leaves
the pre-flight check asking a real broker that is not there.

### Moving the dongle to another installation

Sensors are paired to the *coordinator*, not to the Pi, but the network key and
device database live in the `zigbee2mqtt-data` volume, which `scripts/backup.sh`
now captures. Move the dongle without that volume and every sensor has to be
paired again by hand, at the door or window it is stuck to.

Names, door/window types and rooms are keyed by IEEE address in
`data/zigbee_sensor_metadata.json`. Carry that file across and re-paired sensors
return to the rooms and names they already had instead of arriving anonymous —
which is the difference between re-pairing ten sensors and re-pairing *and*
re-describing them. Zone ids differ between installations, so map each
sensor's `zone_id` to the room of the same name on the new Pi.

In order, for a Pi that is already running:

1. **Copy the `zigbee2mqtt-data` volume across before the stick is plugged in.**
   Create it on the new Pi as `nobo-control_zigbee2mqtt-data`, with the labels
   `com.docker.compose.project=nobo-control` and
   `com.docker.compose.volume=zigbee2mqtt-data` so Compose treats it as its
   own. A Zigbee2MQTT that starts on an empty volume forms a *new* network on
   the stick, and the old one is gone.

   Copy it **on the day of the move, with the old Zigbee2MQTT stopped**, not
   weeks ahead. The database is only true as of the moment it was copied:
   anything paired afterwards — a sensor, or a repeater — still holds the
   network key and will keep talking, but the new Pi's Zigbee2MQTT has never
   heard of it and it has to be paired again. A copy staged early is a rehearsal,
   and should be replaced before the stick is plugged in.
2. **Set `NOBO_ZIGBEE_ADAPTER`** in `.env`. The by-id name carries the stick's
   own serial number, so it is the same on every Pi.
3. **Plug the stick in, then add `zigbee` to `COMPOSE_PROFILES`** — beside
   `tls`, not instead of it — and `sudo systemctl restart nobo-control`.
   Not before: with the profile on and no stick, starting Zigbee2MQTT fails
   with "error gathering device information". The heating carries on — checked
   on the demo Pi on 24 September 2026, from a cold start — but the service
   unit retries every ten seconds until the stick is back.
4. **Choose Zigbee** under Settings → Door and Window Sensor Configuration.
5. **Open and close each sensor once.** A sleeping contact sensor reports
   nothing until it changes, so until then it shows as not heard from.
6. **Retire the old volume** once the new Pi is working. Two coordinators with
   the same network key and PAN id — the old volume restored onto a second
   stick — would fight over the same mesh.

`install.sh --reconfigure` also does step 3, but it asks every question again,
the hub and the admin password included. On a Pi with a real hub and a
password somebody knows, the steps above are the shorter way.

## Pairing and management

Settings contains only the feature switch, provider status, paired count, and
an **Add sensor** entry point. Pairing collects the physical type (door or
window), a useful name, and the zone assignment. Sensors are then managed in
their zone, beside the state they report, rather than growing one unbounded
list in Settings. The simulated provider completes pairing immediately. A
Zigbee2MQTT provider cannot, because a real join is not instantaneous; see the
contract change noted above.

### A flat battery looks exactly like a quiet one

Zigbee2MQTT will not call a battery device offline until it has been silent for
**25 hours** (`availability.passive.timeout`, 1500 minutes). That default is
right and should not be shortened casually: a contact sensor speaks only when
something changes, and the ones here have gone **two and a half hours between
reports with perfectly good batteries**. Anything much tighter cries wolf.

The consequence is worth stating plainly. For most of a day after a battery
dies, the sensor reads as present and whatever it last said is still believed —
including `open`, which will hold that room's heating action for the whole
period. Tested by pulling a battery: two hours later the system still reported
the sensor available and the window open, which is correct behaviour and
unhelpful news.

So the interface names the silence long before anything is willing to call it
offline: past six hours a sensor says *"nothing heard since ..."* rather than
sitting there looking healthy. Six hours is chosen against measured behaviour,
not taste.

The same six hours is what the **A sensor stops reporting** alert uses, so the
screen and the email cannot disagree about when silence becomes news — and the
same 20% threshold backs the low-battery badge and the low-battery alert. Two
places grading one number differently would be worse than either grading alone.

Three points about those alerts belong here rather than in the README, because
they are consequences of how the hardware behaves:

**All of them quiet at once is one fault.** Nineteen silent sensors is not
nineteen flat batteries; it is Zigbee2MQTT or the broker having stopped, or the
stick having been unplugged. So the individual alerts are held back and a single
*Every sensor stops reporting* is sent instead, which also says the heating is
unaffected — it runs over a separate connection to the hub, and somebody reading
that subject line at midnight should not fear for the pipes.

**The time-based alerts have to schedule their own wake-up.** The automation
loop sleeps until something *reports*, and a window left open in an empty cabin
reports nothing at all — which is exactly the case the 24-hour escalation exists
for. Each condition therefore returns the earliest moment it could next become
true and the loop merges that into its sleep, which keeps the property that an
idle house still does not poll.

**A left-open escalation cannot distinguish a real open window from a sensor
knocked off its frame.** Both read open for ever. The email says so, rather than
sending somebody to the cabin certain of what they will find.

**Open-while-away is the one alert that ignores quiet hours**, because the delay
is the damage: you are leaving now, and by morning you are hours away with the
anti-frost temperature holding an open room. It is also the one that needed a
grace period, for a reason that only appears once it is installed — *the front
door is open while you are walking out of it*, so firing the instant Away is set
would cry wolf on every single departure, and a rule that cries wolf is a rule
that gets switched off. Five minutes covers leaving.

The grace is measured from when the contact opened rather than from when Away
was set, which is what gives the right answer in both directions: a window open
since breakfast alerts the moment you leave, while a door opened as you go gets
its five minutes. Coming home clears the alert without sending anything — "you
are back" is not news to somebody who has just walked in — but somebody going
and shutting it does send the recovery.

Two things make that age trustworthy:

**It is the device's timestamp, not ours.** With `last_seen: ISO_8601` set in
Zigbee2MQTT, each report carries when the device actually spoke. A broker
replays retained messages on every reconnect, and counting a replay as a fresh
sighting reset "last heard" for every sensor each time the application
started — including the flat ones, which is exactly the case it exists for.

**It survives a restart.** The age is kept in
`data/zigbee_sensor_metadata.json` beside the name and room. Taking "now" at
registration made a sensor whose battery died days ago claim it had just been
heard from, every time the application started — the one moment somebody is
most likely to be looking at it.

### When the sensor stack goes away

Two different failures, and both must end with sensors reported as
`available: false` rather than their last known state being presented as
current:

**Zigbee2MQTT stops.** Its last will publishes `bridge/state: offline`, which
the provider treats as every sensor being unreachable.

**The broker itself dies, or the network goes.** No will is delivered, because
there is nobody left to deliver it. The transport therefore reports its own
disconnection, and that is handled identically. Without it the front page would
go on asserting that every window is shut, and the away-and-open warning would
quietly stop working, with nothing on screen to say why.

Neither changes the heating: a zone only settles when every one of its contacts
is *available and closed*, so an override is held either way. What is lost
without the second path is the operator's only cue to go and look.

`MqttUnavailable` is a subclass of `ProviderUnavailable` so that a handler
answering 503 cannot catch only half of them, and `SensorNotFound` is defined
once beside the contract for the same reason — a provider with its own
same-named class is not caught, and the handler answers 500.

### What the pairing window says

Somebody pairing a sensor is standing at a door holding a button on a battery
device, so the interface has to distinguish four things, and in particular has
to distinguish *nothing has happened yet* from *nothing is going to*:

| State | Shown as |
| --- | --- |
| window open | "Listening for a sensor", with the seconds remaining counting down |
| a contact sensor joined | "Sensor found", then the name/type/room form |
| something joined that is not a contact sensor | "That is not a contact sensor", naming what it was |
| the interview did not finish | "Pairing failed" |
| the window closed with nothing | "Nothing joined in time" |

The last is the one worth having: a join window that simply goes quiet leaves a
person pressing a button at a radio that stopped listening minutes ago. It is a
state of its own, not a return to the beginning.

The window is polled once a second while that sheet is open, rather than pushed,
because a countdown has to tick every second and the WebSocket only speaks when
something changes. The poll stops when the sheet closes.

Dismissing the sheet — by the scrim or by Escape, not only by its buttons —
closes the join window, as the hub's own device search already did. Otherwise
the radio stays in permit-join for the rest of its four minutes with nothing on
screen saying so, and anything that joins in that time is accepted silently.

Zigbee caps a join window at 254 seconds. Offering longer would promise
something the radio silently clamps, so the API refuses it.

Existing schema-v1 simulated records predate the type field and migrate to
`window`, which preserves them without guessing from a user-editable name.
Schema-v1 and schema-v2 zone policies migrate with **Sensor override** off.

## What a rule may do to the heating

Each zone chooses a warning delay and, separately, one thing to do while a
contact of its own is open:

| Action | What it does |
| --- | --- |
| Do nothing | Warn only. The heating is untouched. |
| Set to Away | Hold the fixed 7 °C anti-frost temperature. |
| Set to Eco | Hold the zone's eco temperature. |
| Set to Comfort | Hold the zone's comfort temperature. |
| Return to schedule | Let go of any hold on the room so its schedule decides. |

The first four either hold an override or hold nothing. **Return to schedule**
is the odd one out: it cancels a zone override rather than creating one, so
there is nothing to give back when the contact closes. It is one-shot and
self-limiting — once the hold is gone there is nothing left to release.

### The rule

`off` is colder than `away`, which is colder than `eco`, which is colder than
`comfort`.

> **While a contact is open, the room runs whichever is colder: what the rule
> asks for, or what the room would be doing anyway.**

That is the whole policy, and it is worked out afresh on every pass rather than
remembered. What "the room would be doing anyway" means depends on who is
holding it: the global override or week profile when this automation holds the
zone, and whatever mode is actually running when somebody else does.

| While a window is open with an **Eco** rule | The room runs |
| --- | --- |
| Home, week profile says Comfort | Eco — the rule is colder |
| Home, week profile says Away | Away — the house is colder |
| Away chosen for the house | Away |
| Comfort chosen for the house *while it is still open* | Eco |
| Somebody sets this room to Comfort by hand | Eco |
| Somebody sets this room to Away by hand | Away |

Deciding from scratch each time is what makes it predictable. An earlier
version remembered that somebody had "taken over" and stood the rule down for
the rest of the open cycle, which meant a room's temperature depended on the
order things had happened in rather than on what was true now — and choosing
Home after Away left the window rule switched off without anything saying so.

**Override colder modes** is the per-zone way out. With it on, the comparison
is skipped and the rule's mode holds until the contact closes, whatever anyone
else asks for in the meantime.

**Return to schedule** cancels a hold rather than taking one, so nothing is
owned afterwards. Letting go warms a room whenever the schedule is warmer than
the hold, so it answers to the same ordering as everything else.

### Ownership

An override this automation applies is written down with its exact mode. When
every assigned contact reports closed, exactly that override is cancelled with
a Nobø `NORMAL`; the current global mode or week profile then decides what the
room does. Comfort is never sent to "restore" a room, because there is no
record that Comfort is where it came from.

A hub applies an override asynchronously and only shows it once it echoes back,
so for a few seconds after writing to a zone this automation does not treat
disagreement as somebody overruling it — otherwise every evaluation in that gap
would send the same command again.

An unknown or unavailable contact is not closed, so it can neither clear a
left-open warning nor release a hold. A room says why its rule is standing
down: already colder, no heater, or no hub.

### Leaving with something open

The per-room warning answers "has this been open a while?". It does not answer
"did I shut up the place before I drove off?", which is a different question
with a different urgency, so it gets its own warning at the top of the front
page whenever the house is on a global Away — set by hand or by a scheduled
away period — and any contact is open. It carries no delay: being away is what
changes the stakes.

Whether the house is away is read from `global_override_mode` on
`/api/status`, not inferred from the zones. A house on Away with one room kept
on Eco by an away exception does not read as "away" if you only look at what
each zone is running.

### Simulated hub state

Demo mode has to answer "what would this room fall back to?", which means it
has to model both kinds of override the hub keeps. The active global mode and
the set of zones under their own override live in `data/server_state.json`, so
a restart knows what the simulated hub is holding — exactly as a real hub
remembers its overrides across a power cut.

## Monitoring-only rooms

Sensors attach to the application's existing zone IDs. A zone may contain no
Nobø components: it still receives sensor state and warnings, while heating
actions are unavailable because there is no heater to control. This avoids a
second room model that would drift from the heating UI.
