# Zigbee sensor support

Off unless asked for. See `docs/SENSORS.md` for the design and
`README.md` for the user-facing description.

    # .env
    COMPOSE_PROFILES=zigbee
    NOBO_ZIGBEE_ADAPTER=/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_<serial>-if00-port0

Find the adapter path with `ls -l /dev/serial/by-id/`. Use that path and never
`/dev/ttyUSB0`: USB numbering is not stable across reboots, and pointing a
coordinator at the wrong adapter is not a failure that announces itself.

## Radio settings

`NOBO_ZIGBEE_CHANNEL` and `NOBO_ZIGBEE_TRANSMIT_POWER` reach Zigbee2MQTT as
`ZIGBEE2MQTT_CONFIG_*` variables, because `configuration.yaml` is generated
inside the named volume and nothing in this repository can edit it directly.

Transmit power defaults to 20 dBm, the ZBDongle-P's maximum against a firmware
default of 5. It needs no re-pairing and can be put back.

The channel defaults to **empty on purpose**, and an empty `ZIGBEE2MQTT_CONFIG_*`
variable is ignored, so an existing network stays where it is. Set it before
pairing anything and treat it as permanent: sleepy Aqara and Xiaomi devices do
not follow a channel change and have to be re-paired one at a time.

Both land only when Zigbee2MQTT writes its configuration — on first run, or
when a setting is persisted — not on an ordinary restart. Verify with:

    sudo docker exec nobo-zigbee2mqtt \
        grep -E 'channel|transmit_power' /app/data/configuration.yaml

`scripts/zigbee-map.sh` reports which devices route and which parent each
sensor chose. See "Extending range" in `docs/SENSORS.md`.

Zigbee2MQTT keeps its network key and device database in the
`zigbee2mqtt-data` volume. **Losing it means re-pairing every sensor by hand.**

## Moving a dongle to another Pi

The sensors stay paired to the *coordinator*, not to the Pi, but the network
key lives in that volume. Moving the dongle without it means every sensor must
be paired again.

Sensor names, door/window types and room assignments are keyed by IEEE address
and live in the application's own `data/zigbee_sensor_metadata.json`. Carry
that file across and re-paired sensors come back to the rooms and names they
already had, instead of arriving anonymous.
