"""A fake Zigbee2MQTT, faithful to its topic contract.

The real provider talks to this in tests.  What is faked is Zigbee2MQTT's
*application* protocol — its topics, payloads and request/response pattern —
because that is the part this application can get wrong.  The MQTT wire
protocol underneath is the client library's responsibility and is deliberately
not reimplemented here.

The same caveat applies as to ``fake_hub.py``: this proves that a message was
understood, not that a radio delivered it.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable, Optional


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT topic-filter matching, including ``+`` and ``#``."""
    if filter_ == topic:
        return True
    parts = filter_.split("/")
    actual = topic.split("/")
    for index, part in enumerate(parts):
        if part == "#":
            return index <= len(actual)
        if index >= len(actual):
            return False
        if part != "+" and part != actual[index]:
            return False
    return len(parts) == len(actual)


class FakeBroker:
    """Just enough broker: retained messages and wildcard subscriptions."""

    def __init__(self):
        self._retained: dict[str, bytes] = {}
        self._subscribers: list[tuple[list[str], Callable[[str, bytes], Awaitable[None]]]] = []

    def register(self, filters: list[str], callback) -> None:
        self._subscribers.append((filters, callback))

    async def deliver_retained(self, filters: list[str], callback) -> None:
        for topic, payload in list(self._retained.items()):
            if any(topic_matches(item, topic) for item in filters):
                await callback(topic, payload)

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        data = payload.encode("utf-8")
        if retain:
            self._retained[topic] = data
        for filters, callback in list(self._subscribers):
            if any(topic_matches(item, topic) for item in filters):
                await callback(topic, data)


class FakeTransport:
    """An :class:`MqttTransport` backed by :class:`FakeBroker`."""

    def __init__(self, broker: FakeBroker):
        self._broker = broker
        self._callback: Optional[Callable[[str, bytes], Awaitable[None]]] = None
        self._filters: list[str] = []
        self.connected = False

    def on_message(self, callback) -> None:
        self._callback = callback

    async def connect(self) -> None:
        self.connected = True
        self._broker.register(self._filters, self._dispatch)

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe(self, topic: str) -> None:
        self._filters.append(topic)
        # A real broker replays retained messages on subscribe, which is how a
        # restart gets current state without waiting for something to move.
        await self._broker.deliver_retained([topic], self._dispatch)

    async def publish(self, topic: str, payload: str) -> None:
        await self._broker.publish(topic, payload)

    async def _dispatch(self, topic: str, payload: bytes) -> None:
        if self.connected and self._callback is not None:
            await self._callback(topic, payload)


def contact_device(
    address: str,
    *,
    friendly_name: Optional[str] = None,
    model: str = "MCCGQ11LM",
    description: str = "Door and window sensor",
) -> dict:
    """A ``bridge/devices`` entry shaped like a real contact sensor.

    Copied from what a real Aqara MCCGQ11LM sent on joining, not invented.
    Note ``value_on: false`` / ``value_off: true``: the device reports whether
    the magnet is in contact, so ``contact: true`` is a *closed* opening.
    """
    return {
        "ieee_address": address,
        "friendly_name": friendly_name or address,
        "type": "EndDevice",
        "supported": True,
        "disabled": False,
        "definition": {
            "model": model,
            "vendor": "Aqara",
            "description": description,
            "exposes": [
                {
                    "type": "numeric",
                    "name": "battery",
                    "property": "battery",
                    "access": 1,
                    "unit": "%",
                    "value_min": 0,
                    "value_max": 100,
                    # The real definition warns this "can take up to 24 hours
                    # before reported", which is why a sensor with no battery
                    # reading is normal rather than faulty.
                    "category": "diagnostic",
                },
                {
                    "type": "binary",
                    "name": "contact",
                    "property": "contact",
                    "access": 1,
                    "value_on": False,
                    "value_off": True,
                },
                {
                    "type": "numeric",
                    "name": "linkquality",
                    "property": "linkquality",
                    "access": 1,
                    "unit": "lqi",
                    "value_min": 0,
                    "value_max": 255,
                    "category": "diagnostic",
                },
            ],
        },
    }


def other_device(address: str, *, friendly_name: Optional[str] = None) -> dict:
    """A device with no contact expose, which must be ignored."""
    return {
        "ieee_address": address,
        "friendly_name": friendly_name or address,
        "type": "Router",
        "supported": True,
        "disabled": False,
        "definition": {
            "model": "E22x4",
            "vendor": "IKEA",
            "description": "TRETAKT smart plug",
            "exposes": [
                {
                    "type": "switch",
                    "features": [
                        {
                            "type": "binary",
                            "name": "state",
                            "property": "state",
                            "access": 7,
                        }
                    ],
                }
            ],
        },
    }


def unhelpful_device(address: str, *, friendly_name: Optional[str] = None) -> dict:
    """A battery device that is neither a contact sensor nor a router.

    A remote or a button: it joins, it is not a sensor, and being an end device
    it will not relay for anything either. The one case where "that is not a
    contact sensor" really is the whole story.
    """
    return {
        "ieee_address": address,
        "friendly_name": friendly_name or address,
        "type": "EndDevice",
        "supported": True,
        "disabled": False,
        "definition": {
            "model": "E1524",
            "vendor": "IKEA",
            "description": "TRADFRI remote control",
            "exposes": [
                {"type": "enum", "name": "action", "property": "action", "access": 1},
            ],
        },
    }


class FakeZigbee2Mqtt:
    """Publishes what Zigbee2MQTT publishes and answers what it answers."""

    def __init__(self, broker: FakeBroker, base_topic: str = "zigbee2mqtt"):
        self._broker = broker
        self._base = base_topic
        self.devices: list[dict] = []
        self.permit_join_requests: list[int] = []
        self.removed: list[str] = []
        self.remove_requests: list[dict] = []
        # A sleeping battery device cannot be told to leave, which is the
        # ordinary case for a contact sensor rather than an exotic one.
        self.refuse_removal = False
        # Zigbee2MQTT wedged or gone: requests accepted, never answered.
        self.ignore_requests = False
        broker.register([f"{base_topic}/bridge/request/#"], self._on_request)

    async def go_online(self) -> None:
        await self._broker.publish(
            f"{self._base}/bridge/state", json.dumps({"state": "online"}), retain=True
        )
        await self.publish_devices()

    async def go_offline(self) -> None:
        await self._broker.publish(
            f"{self._base}/bridge/state", json.dumps({"state": "offline"}), retain=True
        )

    async def publish_devices(self) -> None:
        await self._broker.publish(
            f"{self._base}/bridge/devices", json.dumps(self.devices), retain=True
        )

    async def add_device(self, device: dict) -> None:
        self.devices.append(device)
        await self._broker.publish(
            f"{self._base}/bridge/event",
            json.dumps(
                {
                    "type": "device_joined",
                    "data": {
                        "ieee_address": device["ieee_address"],
                        "friendly_name": device["friendly_name"],
                    },
                }
            ),
        )
        await self.publish_devices()

    async def device_left(self, address: str) -> None:
        self.devices = [
            item for item in self.devices if item["ieee_address"] != address
        ]
        await self._broker.publish(
            f"{self._base}/bridge/event",
            json.dumps(
                {"type": "device_leave", "data": {"ieee_address": address}}
            ),
        )
        await self.publish_devices()

    async def report(self, friendly_name: str, *, last_seen=None, **payload) -> None:
        """Zigbee2MQTT stamps each report with the device's own last_seen when
        configured to, which is what makes a replayed retained message
        distinguishable from a fresh one."""
        if last_seen is not None:
            payload["last_seen"] = last_seen
        await self._broker.publish(
            f"{self._base}/{friendly_name}", json.dumps(payload), retain=True
        )

    async def availability(self, friendly_name: str, online: bool) -> None:
        await self._broker.publish(
            f"{self._base}/{friendly_name}/availability",
            json.dumps({"state": "online" if online else "offline"}),
            retain=True,
        )

    async def _on_request(self, topic: str, payload: bytes) -> None:
        if self.ignore_requests:
            return
        action = topic[len(f"{self._base}/bridge/request/"):]
        body = json.loads(payload.decode("utf-8"))
        if action == "permit_join":
            self.permit_join_requests.append(body["time"])
            await self._broker.publish(
                f"{self._base}/bridge/response/permit_join",
                json.dumps({"data": {"time": body["time"]}, "status": "ok"}),
            )
        elif action == "device/remove":
            self.remove_requests.append(dict(body))
            if self.refuse_removal and not body.get("force"):
                # Verbatim from a real Zigbee2MQTT refusing to evict a
                # sleeping Aqara. Note "data" is EMPTY: the id is only in the
                # error text, which is why correlating on data.id silently
                # dropped every failure.
                await self._broker.publish(
                    f"{self._base}/bridge/response/device/remove",
                    json.dumps({
                        "data": {},
                        "status": "error",
                        "error": (
                            f"Failed to remove device '{body['id']}' (block: "
                            "false, force: false, keep config: false, clear "
                            "cache: false) (Error: AREQ - ZDO - mgmtLeaveRsp "
                            "after 10000ms)"
                        ),
                    }),
                )
                return
            self.removed.append(body["id"])
            self.devices = [
                item for item in self.devices
                if item["ieee_address"] != body["id"]
            ]
            # Order matters and is copied from the real thing: the device list
            # is republished BEFORE the reply, so anything that cleans up on
            # the reply must not depend on the device still being registered.
            await self.publish_devices()
            await self._broker.publish(
                f"{self._base}/bridge/response/device/remove",
                json.dumps({
                    "data": {"id": body["id"], "force": bool(body.get("force"))},
                    "status": "ok",
                }),
            )
