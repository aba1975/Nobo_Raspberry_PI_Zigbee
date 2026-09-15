# Contact sensor provider boundary

Contact sensors are optional and off by default. The first provider is a
persisted simulator for demo mode; it does not require a Zigbee dongle, MQTT
broker, Zigbee2MQTT, or any additional service.

The application consumes normalized contact snapshots rather than MQTT topics
or vendor payloads. A provider supplies:

- an opaque stable provider ID and application sensor ID;
- a display name, door/window type, and assigned Nobø zone ID;
- contact state (`open`, `closed`, or `unknown`);
- availability, battery percentage, change time, and last-seen time;
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

**A report carries no battery.** Zigbee2MQTT's definition says battery "can take
up to 24 hours before reported", so every newly paired sensor legitimately has
none. The provider leaves it `null` and the UI renders nothing rather than a
fault; substituting the last value or showing a warning would cry wolf on every
new sensor.

**A re-pairing sensor emits `device_leave` immediately before `device_joined`.**
Metadata is therefore kept when a device leaves, so it returns to its room and
name rather than arriving anonymous.

Still unverified: mesh range and reliability over distance, Aqara re-parenting
onto a repeater, battery-reporting accuracy, and behaviour over days rather than
minutes. Link quality was 98–105 of 255 with the sensor near the coordinator,
which says nothing about the far end of the building.

`tests/fake_zigbee2mqtt.py` fakes Zigbee2MQTT's *topic* contract, which is the
part this application can get wrong; the MQTT wire protocol underneath is the
client library's responsibility and is deliberately not reimplemented. The same
caveat applies as to `fake_hub.py`: it proves a message was understood, not that
a radio delivered it.

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

When Zigbee2MQTT moves into the application stack it must go behind a Compose
profile, as `tls` already is, so a Nobø-only installation starts nothing extra.
Its data directory holds the network key and device database; losing it means
re-pairing everything, so it belongs in `scripts/backup.sh`. Note also that the
application container runs as uid 1001 and `nobo` is not in `dialout`, so device
passthrough and group handling need attention at that point.

## Pairing and management

Settings contains only the feature switch, provider status, paired count, and
an **Add sensor** entry point. Pairing collects the physical type (door or
window), a useful name, and the zone assignment. Sensors are then managed in
their zone, beside the state they report, rather than growing one unbounded
list in Settings. The simulated provider completes pairing immediately. A
Zigbee2MQTT provider cannot, because a real join is not instantaneous; see the
contract change noted above.

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
