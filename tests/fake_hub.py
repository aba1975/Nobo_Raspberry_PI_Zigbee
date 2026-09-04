"""A fake Nobø Eco Hub that speaks the real protocol.

The point of this is to be able to test the real-hub code paths without real
hardware. It is a genuine TCP server implementing the handshake and command set
from ``API_Nobo.pdf``, so the real ``pynobo`` client connects to it exactly as
it would to a hub — no mocking of pynobo itself.

What it covers:

* the ``HELLO`` / ``HANDSHAKE`` handshake, including ``REJECT`` on a serial mismatch
* ``G00`` returning ``H00``, ``H01``, ``H02``, ``H03``, ``H04``, ``H05``
* add / update / remove for zones, components and week profiles
* receiver search and pairing (``X00``, ``X01``, ``X03`` → ``Y00``, ``Y01``, ``Y03``, ``Y04``)
* ``E00`` errors for malformed commands

Deliberate simplifications, so nobody mistakes this for an emulator: overrides
are stored but never applied to anything, temperatures are only reported if a
test sets them, and the 30-second auto-stop of a real receiver search is not
simulated — tests drive the timing themselves.

pynobo connects to a hard-coded port 27779, so that is what this binds on
localhost.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional

HUB_PORT = 27779

# Spaces inside names are carried as non-breaking spaces on the wire, because
# the protocol is space-delimited. See API_Nobo.pdf, "Data structures".
NBSP = "\u00a0"


def encode_name(name: str) -> str:
    return name.replace(" ", NBSP)


def decode_name(name: str) -> str:
    return name.replace(NBSP, " ")


class FakeHub:
    """An in-process Nobø hub. Use as an async context manager."""

    def __init__(
        self,
        serial: str = "123123123123",
        name: str = "Fake Hub",
        zones: Optional[Dict[str, List[str]]] = None,
        components: Optional[Dict[str, List[str]]] = None,
        week_profiles: Optional[Dict[str, List[str]]] = None,
        discoverable: Optional[List[str]] = None,
        pair_should_succeed: bool = True,
    ):
        self.serial = serial
        self.name = name

        # Each record is stored as the protocol's field list, minus the command
        # itself, so replies are a matter of prefixing the right command code.
        # Zone:      <id> <name> <week profile id> <comfort> <eco> <allow overrides> <override id>
        self.zones: Dict[str, List[str]] = zones if zones is not None else {
            "1": ["1", encode_name("Living Room"), "1", "21", "18", "1", "-1"],
            "2": ["2", encode_name("Bathroom"), "1", "24", "20", "1", "-1"],
        }
        # Component: <serial> <status> <name> <reverse> <zone id> <override id> <temp sensor for zone>
        self.components: Dict[str, List[str]] = components if components is not None else {
            "186100000001": ["186100000001", "0", encode_name("Living Room Panel"), "0", "1", "-1", "-1"],
            "186100000002": ["186100000002", "0", encode_name("Bathroom Floor"), "0", "2", "-1", "-1"],
        }
        # Week profile: <id> <name> <profile>
        self.week_profiles: Dict[str, List[str]] = week_profiles if week_profiles is not None else {
            "1": ["1", encode_name("Default"), "00000,07001,23000,00000,07001,23000,00000,"
                                                "07001,23000,00000,07001,23000,00000,07001,"
                                                "23000,00000,07001,23000,00000,07001,23000"],
        }
        self.overrides: Dict[str, List[str]] = {}
        self.temperatures: Dict[str, str] = {}

        # Serial numbers a receiver search will "find".
        self.discoverable: List[str] = discoverable if discoverable is not None else ["186100000009"]
        self.pair_should_succeed = pair_should_succeed

        self.received: List[List[str]] = []
        self.search_active = False

        # Lets a test make the next command fail, to exercise error handling.
        self.fail_next: Optional[str] = None

        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: List[asyncio.StreamWriter] = []
        self._next_zone_id = 100
        self._next_week_profile_id = 100
        self._next_override_id = 100
        self.on_command: Optional[Callable[[List[str]], None]] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", HUB_PORT)

    async def stop(self) -> None:
        for writer in list(self._clients):
            try:
                writer.close()
            except Exception:
                pass
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "FakeHub":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -- helpers for tests -------------------------------------------------

    def zone_named(self, name: str) -> Optional[List[str]]:
        for zone in self.zones.values():
            if decode_name(zone[1]) == name:
                return zone
        return None

    def component_name(self, serial: str) -> Optional[str]:
        record = self.components.get(serial)
        return decode_name(record[2]) if record else None

    def commands_of_type(self, code: str) -> List[List[str]]:
        return [c for c in self.received if c and c[0] == code]

    def last_command(self, code: str) -> Optional[List[str]]:
        matches = self.commands_of_type(code)
        return matches[-1] if matches else None

    async def push(self, fields: List[str]) -> None:
        """Push an unsolicited message to every connected client."""
        for writer in list(self._clients):
            await self._send(writer, fields)

    # -- protocol ----------------------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, fields: List[str]) -> None:
        writer.write((" ".join(str(f) for f in fields) + "\r").encode("utf-8"))
        await writer.drain()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.append(writer)
        try:
            # HELLO <version> <hub serial> <timestamp>
            hello = await self._read(reader)
            if hello is None:
                return
            if len(hello) < 4:
                await self._send(writer, ["REJECT", "2"])
                return
            if hello[2] != self.serial:
                await self._send(writer, ["REJECT", "1"])
                return
            await self._send(writer, ["HELLO", "1.1"])

            handshake = await self._read(reader)
            if handshake is None or handshake[0] != "HANDSHAKE":
                return
            await self._send(writer, ["HANDSHAKE"])

            while True:
                command = await self._read(reader)
                if command is None:
                    return
                await self._dispatch(writer, command)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if writer in self._clients:
                self._clients.remove(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _read(self, reader: asyncio.StreamReader) -> Optional[List[str]]:
        try:
            raw = await reader.readuntil(b"\r")
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        if not raw:
            return None
        return raw[:-1].decode("utf-8").split(" ")

    async def _dispatch(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        code = command[0]

        # HANDSHAKE doubles as pynobo's keep-alive; echo it and record nothing.
        if code == "HANDSHAKE":
            await self._send(writer, ["HANDSHAKE"])
            return

        self.received.append(command)
        if self.on_command is not None:
            self.on_command(command)

        if self.fail_next == code:
            self.fail_next = None
            await self._send(writer, ["E00", code, "Rejected by test"])
            return

        handler = {
            "G00": self._get_all,
            "A00": self._add_zone,
            "A01": self._add_component,
            "A02": self._add_week_profile,
            "A03": self._add_override,
            "U00": self._update_zone,
            "U01": self._update_component,
            "U02": self._update_week_profile,
            "R00": self._remove_zone,
            "R01": self._remove_component,
            "R02": self._remove_week_profile,
            "X00": self._start_search,
            "X01": self._stop_search,
            "X03": self._pair,
        }.get(code)

        if handler is None:
            await self._send(writer, ["E00", code, "Unknown command"])
            return

        await handler(writer, command)

    # -- command handlers --------------------------------------------------

    async def _get_all(self, writer: asyncio.StreamWriter, _command: List[str]) -> None:
        await self._send(writer, ["H00"])
        for zone in self.zones.values():
            await self._send(writer, ["H01"] + zone)
        for component in self.components.values():
            await self._send(writer, ["H02"] + component)
        for profile in self.week_profiles.values():
            await self._send(writer, ["H03"] + profile)
        for override in self.overrides.values():
            await self._send(writer, ["H04"] + override)
        for serial, temp in self.temperatures.items():
            await self._send(writer, ["Y02", serial, temp])
        # H05 terminates the G00 response; pynobo waits for it.
        await self._send(
            writer,
            ["H05", self.serial, encode_name(self.name), "12", "-1", "1.7", "1f", "20180101"],
        )

    async def _add_zone(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        if len(command) < 8:
            await self._send(writer, ["E00", "A00", "Wrong number of arguments"])
            return
        # "The Hub ignores the incoming IDs of new Zone ... and instead returns
        # a newly assigned ID" — API_Nobo.pdf.
        zone_id = str(self._next_zone_id)
        self._next_zone_id += 1
        record = [zone_id] + command[2:8]
        record[6] = "-1"
        self.zones[zone_id] = record
        await self._broadcast(["B00"] + record)

    async def _add_component(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        if len(command) < 8:
            await self._send(writer, ["E00", "A01", "Wrong number of arguments"])
            return
        serial = command[1]
        record = command[1:8]
        self.components[serial] = record
        await self._broadcast(["B01"] + record)

    async def _add_week_profile(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        if len(command) < 4:
            await self._send(writer, ["E00", "A02", "Wrong number of arguments"])
            return
        profile_id = str(self._next_week_profile_id)
        self._next_week_profile_id += 1
        record = [profile_id, command[2], command[3]]
        self.week_profiles[profile_id] = record
        await self._broadcast(["B02"] + record)

    async def _add_override(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        # The hub keeps at most one override per target: a new one for the same
        # target replaces whatever was there, and an override in NORMAL mode is
        # how a target is cleared rather than a record in its own right.
        #
        # Modelling that here matters. Without it a cancelled override lingers,
        # pynobo keeps reporting the old mode, and a test would happily pass
        # while the zone stayed stuck — which is the exact defect this models.
        target_type = command[6] if len(command) > 6 else None
        target_id = command[7] if len(command) > 7 else None
        superseded = [
            override_id for override_id, record in self.overrides.items()
            if record[5] == target_type and record[6] == target_id
        ]
        for override_id in superseded:
            record = self.overrides.pop(override_id)
            await self._broadcast(["S03", override_id] + record[1:])

        if command[2] == "0":  # NORMAL — the target is simply left with none
            return

        override_id = str(self._next_override_id)
        self._next_override_id += 1
        record = [override_id] + command[2:8]
        self.overrides[override_id] = record
        await self._broadcast(["B03"] + record)

    async def _update_zone(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        zone_id = command[1]
        if zone_id not in self.zones:
            await self._send(writer, ["E00", "U00", "Unknown zone"])
            return
        record = command[1:8]
        self.zones[zone_id] = record
        await self._broadcast(["V00"] + record)

    async def _update_component(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        serial = command[1]
        if serial not in self.components:
            await self._send(writer, ["E00", "U01", "Unknown component"])
            return
        record = command[1:8]
        self.components[serial] = record
        await self._broadcast(["V01"] + record)

    async def _update_week_profile(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        profile_id = command[1]
        if profile_id not in self.week_profiles:
            await self._send(writer, ["E00", "U02", "Unknown week profile"])
            return
        record = [profile_id, command[2], command[3]]
        self.week_profiles[profile_id] = record
        await self._broadcast(["V02"] + record)

    async def _remove_zone(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        zone_id = command[1]
        record = self.zones.pop(zone_id, None)
        if record is None:
            await self._send(writer, ["E00", "R00", "Unknown zone"])
            return
        await self._broadcast(["S00"] + record)

    async def _remove_component(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        serial = command[1]
        record = self.components.pop(serial, None)
        if record is None:
            await self._send(writer, ["E00", "R01", "Unknown component"])
            return
        await self._broadcast(["S01"] + record)

    async def _remove_week_profile(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        profile_id = command[1]
        record = self.week_profiles.pop(profile_id, None)
        if record is None:
            await self._send(writer, ["E00", "R02", "Unknown week profile"])
            return
        await self._broadcast(["S02"] + record)

    async def _start_search(self, writer: asyncio.StreamWriter, _command: List[str]) -> None:
        self.search_active = True
        await self._send(writer, ["Y00"])
        for serial in self.discoverable:
            await self._send(writer, ["Y04", serial])

    async def _stop_search(self, writer: asyncio.StreamWriter, _command: List[str]) -> None:
        self.search_active = False
        await self._send(writer, ["Y01"])

    async def _pair(self, writer: asyncio.StreamWriter, command: List[str]) -> None:
        serial = command[1]
        if not self.pair_should_succeed:
            await self._send(writer, ["Y03", serial, "0"])
            return
        await self._send(writer, ["Y03", serial, "1"])
        # A real hub follows a successful pairing by reporting the component.
        record = [serial, "0", encode_name(f"Device {serial[-4:]}"), "0", "-1", "-1", "-1"]
        self.components[serial] = record
        await self._broadcast(["B01"] + record)

    async def _broadcast(self, fields: List[str]) -> None:
        for writer in list(self._clients):
            await self._send(writer, fields)


class FakeHubThread:
    """Run a :class:`FakeHub` on its own event loop in a background thread.

    The application connects to the hub from a worker thread with its own loop,
    so the fake has to live somewhere that is not the test's loop.
    """

    def __init__(self, **kwargs: Any):
        self.hub = FakeHub(**kwargs)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def __enter__(self) -> FakeHub:
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.hub.start())
            self._ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("fake hub did not start")
        return self.hub

    def __exit__(self, *exc: Any) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.hub.stop(), self._loop).result(timeout=10)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def run(self, coro: Any) -> Any:
        """Run a coroutine on the hub's loop, from the test thread."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)
