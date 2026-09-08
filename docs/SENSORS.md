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

`sensor_provider.py` defines that contract. `sensor_simulated.py` is the only
implementation in this release. The API, warning aggregation, WebSocket
payloads, UI, persistence policy, and heating automation do not depend on its
storage format.

## Future Zigbee2MQTT provider

A future provider can subscribe to Zigbee2MQTT device and bridge topics,
translate `contact`, `availability`, and `battery` values into the normalized
snapshot, and translate pairing requests into Zigbee2MQTT permit-join
operations. Provider configuration and credentials must remain outside sensor
records and must not be returned by the API.

That integration is not implemented or verified here. In particular, this
release makes no claim about dongle discovery, Zigbee mesh reliability,
Zigbee2MQTT topic variants, retained MQTT messages, or physical sensor battery
reporting.

## Pairing and management

Settings contains only the feature switch, provider status, paired count, and
an **Add sensor** entry point. Pairing collects the physical type (door or
window), a useful name, and the zone assignment. Sensors are then managed in
their zone, beside the state they report, rather than growing one unbounded
list in Settings. The simulated provider completes pairing immediately; a
future Zigbee2MQTT provider will use the same flow while permit-join is active.

Existing schema-v1 simulated records predate the type field and migrate to
`window`, which preserves them without guessing from a user-editable name.
Schema-v1 and schema-v2 zone policies migrate with **Sensor override** off.

## Heating ownership

Each zone has an independent warning delay and an optional delayed action:
Away, Eco, Comfort, follow its schedule, or do nothing. Away, Eco, and Comfort
are applied only when the zone is connected, has heating equipment, and has no
pre-existing zone override. The resulting override is recorded with its exact
mode as automation-owned. After every assigned contact explicitly closes, the
automation releases only that owned override with Nobø `NORMAL`; it never
blindly sends Comfort. Normal lets the current global mode or weekly schedule
decide what happens.

By default a sensor rule can only lower the heating demand: Away is below Eco,
and Eco is below Comfort. An Eco rule therefore leaves an existing Away demand
alone, and a Comfort rule cannot raise an Away or Eco demand. The per-zone
**Sensor override** switch explicitly opts out of that guard and allows the
selected open action to outrank the active global mode or schedule. A later
global or zone command still counts as manual takeover and remains in control
for the rest of that open cycle.

“Follow schedule” is deliberately conservative. If the zone is already free of
a zone override, it is already following its schedule and no command is needed.
If somebody has manually overridden it, the sensor rule does not erase that
choice. A manual change made during any open cycle suppresses further sensor
actions until every contact has closed.

Any manual or external takeover ends ownership for the current open cycle. An
unknown or unavailable contact is not closed and therefore cannot release an
existing left-open warning or automation-owned override.

## Monitoring-only rooms

Sensors attach to the application's existing zone IDs. A zone may contain no
Nobø components: it still receives sensor state and warnings, while heating
actions are unavailable because there is no heater to control. This avoids a
second room model that would drift from the heating UI.
