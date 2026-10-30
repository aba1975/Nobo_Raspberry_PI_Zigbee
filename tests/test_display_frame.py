"""The wall display: its read-only keys, and the picture it is sent.

The picture is drawn on the Pi in the PaperColor's six inks, so what the device
shows is decided — and tested — here. Nothing in this file has touched the
device itself; that the panel draws these PNGs is checked by hand.
"""

import io
import json
import time
from datetime import datetime, timedelta

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import auth
import display_render
import display_tokens
import server
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service,
)

PALETTE = {
    display_render.BLACK, display_render.WHITE, display_render.YELLOW,
    display_render.RED, display_render.BLUE, display_render.GREEN,
}


def simulate(client, sensor_id, **fields):
    response = client.post(f"/api/sensors/{sensor_id}/simulate", json=fields)
    assert response.status_code == 200, response.text


def new_key(client, name="Hall"):
    response = client.post("/api/displays", json={"name": name})
    assert response.status_code == 200, response.text
    body = response.json()
    return body["display"], body["token"]


@pytest.fixture
def device():
    """A client with no session: only the key it is given."""
    with TestClient(server.app) as value:
        yield value


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# -- keys -----------------------------------------------------------------


def test_a_key_is_shown_once_and_only_its_hash_is_stored(client, tmp_path):
    display, token = new_key(client)
    assert token.startswith("nd_") and len(token) > 40
    listed = client.get("/api/displays").json()["displays"]
    assert listed == [display]
    assert token not in json.dumps(listed)
    assert "token_hash" not in display
    raw = display_tokens.DISPLAYS_FILE.read_text()
    assert token not in raw
    assert json.loads(raw)["displays"][0]["token_hash"] == display_tokens._hash(token)
    assert display_tokens.DISPLAYS_FILE.stat().st_mode & 0o077 == 0


def test_keys_survive_a_restart_and_revoking_one_stops_it(client, device):
    display, token = new_key(client)
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.verify(token) == display["display_id"]
    assert device.get("/api/display", headers=bearer(token)).status_code == 200
    assert client.delete(f"/api/displays/{display['display_id']}").status_code == 200
    assert device.get("/api/display", headers=bearer(token)).status_code == 401
    assert client.delete(f"/api/displays/{display['display_id']}").status_code == 404


def test_a_damaged_key_file_loads_no_keys(tmp_path):
    display_tokens.DISPLAYS_FILE.write_text("{not json")
    registry = display_tokens.DisplayRegistry()
    registry.load()
    assert registry.list() == []


def test_names_are_required_and_the_number_of_displays_is_capped(client):
    assert client.post("/api/displays", json={"name": "  "}).status_code == 400
    for index in range(display_tokens.MAX_DISPLAYS):
        new_key(client, f"Display {index}")
    assert client.post("/api/displays", json={"name": "One more"}).status_code == 400


def test_only_admins_manage_displays(client, monkeypatch):
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.get("/api/displays").status_code == 403
    assert client.post("/api/displays", json={"name": "Hall"}).status_code == 403
    assert client.delete("/api/displays/abc").status_code == 403


def test_managing_displays_needs_a_session(device):
    assert device.get("/api/displays", follow_redirects=False).status_code == 401
    assert device.post("/api/displays", json={"name": "Hall"}).status_code == 401


# -- what a key opens -------------------------------------------------------


def test_a_key_opens_the_display_and_nothing_else(client, device):
    _, token = new_key(client)
    headers = bearer(token)
    assert device.get("/api/display", headers=headers).status_code == 200
    assert device.get("/api/display/frame.png", headers=headers).status_code == 200
    for path in ("/api/zones", "/api/displays", "/api/sensors", "/api/hub/config", "/api/log"):
        assert device.get(path, headers=headers).status_code == 401, path
    assert device.post("/api/global/override/away", headers=headers).status_code == 401
    assert device.post("/api/display", headers=headers).status_code == 401
    assert device.get("/", headers=headers, follow_redirects=False).status_code == 401


def test_a_key_does_not_open_the_websocket(client, device):
    _, token = new_key(client)
    with pytest.raises(Exception):
        with device.websocket_connect("/ws", headers=bearer(token)) as ws:
            ws.receive_json()


def test_a_wrong_key_is_refused_even_on_the_display_paths(client, device):
    new_key(client)
    for value in ("nd_wrong", "Bearer", "nd_" + "x" * 300, "not-a-display-key"):
        response = device.get("/api/display/frame.png", headers=bearer(value))
        assert response.status_code == 401, value
    assert device.get("/api/display", follow_redirects=False).status_code == 401


def test_a_session_can_still_see_the_picture(client):
    response = client.get("/api/display/frame.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


# -- the picture -------------------------------------------------------------


def pixels(png):
    image = Image.open(io.BytesIO(png))
    assert image.size == (display_render.WIDTH, display_render.HEIGHT)
    return image.convert("RGB")


def test_the_picture_uses_only_the_six_inks(client, device):
    enable(client, warning=0)
    window = add_sensor(client, name="Bath window")
    add_sensor(client, name="Door", kind="door")
    simulate(client, window["sensor_id"], state="open")
    _, token = new_key(client)
    png = device.get("/api/display/frame.png", headers=bearer(token)).content
    colours = {colour for _, colour in pixels(png).getcolors(maxcolors=1 << 16)}
    assert colours <= PALETTE
    # The banner is red: the window was left open (no delay in this room).
    assert pixels(png).getpixel((5, 5)) == display_render.RED


def test_the_all_closed_band_is_green_darkened_with_black(client, device):
    enable(client)
    window = add_sensor(client, name="Bath window")
    simulate(client, window["sensor_id"], state="closed")
    _, token = new_key(client)
    image = pixels(device.get("/api/display/frame.png", headers=bearer(token)).content)
    assert image.getpixel((5, 5)) == display_render.GREEN
    assert image.getpixel((4, 4)) == display_render.BLACK
    band = image.crop((0, 0, display_render.WIDTH, 10))
    counts = {colour: n for n, colour in band.getcolors()}
    assert counts[display_render.BLACK] * 3 == counts[display_render.GREEN]


def test_a_new_design_changes_the_fingerprint(monkeypatch):
    payload = {"sensors_enabled": True, "contact_count": 1}
    before = display_render.sensor_fingerprint(payload)
    monkeypatch.setattr(display_render, "DESIGN_VERSION", display_render.DESIGN_VERSION + 1)
    assert display_render.sensor_fingerprint(payload) != before


def test_the_etag_holds_until_something_shown_changes(client, device):
    enable(client)
    window = add_sensor(client, name="Bath window")
    simulate(client, window["sensor_id"], state="closed")
    _, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache"
    again = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": etag}
    )
    assert again.status_code == 304 and again.content == b""
    simulate(client, window["sensor_id"], state="open")
    changed = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": etag}
    )
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert pixels(changed.content).getpixel((5, 5)) == display_render.YELLOW


def test_the_display_reports_its_battery_and_a_low_one_is_drawn(client, device):
    display, token = new_key(client)
    headers = {**bearer(token), "X-Display-Battery": "12", "X-Display-Firmware": "1.0.0"}
    response = device.get("/api/display/frame.png", headers=headers)
    assert response.status_code == 200
    listed = client.get("/api/displays").json()["displays"][0]
    assert listed["battery"] == 12 and listed["firmware"] == "1.0.0"
    assert listed["last_seen_at"] is not None
    # The note is on the picture, so it differs from a full battery's.
    full = device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Battery": "90"})
    assert full.headers["etag"] != response.headers["etag"]


def test_the_battery_redraws_once_when_it_goes_low_not_at_every_percent(client, device):
    _, token = new_key(client)

    def etag(level):
        return device.get(
            "/api/display/frame.png", headers={**bearer(token), "X-Display-Battery": str(level)},
        ).headers["etag"]

    assert etag(90) == etag(40)
    assert etag(15) == etag(12) != etag(40)


def test_nonsense_battery_and_firmware_headers_are_ignored(client, device):
    _, token = new_key(client)
    headers = {**bearer(token), "X-Display-Battery": "lots", "X-Display-Firmware": "x" * 500}
    assert device.get("/api/display/frame.png", headers=headers).status_code == 200
    listed = client.get("/api/displays").json()["displays"][0]
    assert listed["battery"] is None
    assert listed["firmware"] is None or len(listed["firmware"]) <= 40


def test_seen_is_written_to_disk_at_most_every_interval():
    registry = display_tokens.DisplayRegistry()
    display, _ = registry.create("Hall", now=1000.0)
    display_id = display["display_id"]
    # The first contact is written at once, so Settings shows it.
    registry.seen(display_id, now=1001.0)
    assert json.loads(display_tokens.DISPLAYS_FILE.read_text())["displays"][0]["last_seen_at"] == 1001.0
    written = display_tokens.DISPLAYS_FILE.stat().st_mtime_ns
    time.sleep(0.01)
    registry.seen(display_id, now=1100.0)
    assert display_tokens.DISPLAYS_FILE.stat().st_mtime_ns == written
    registry.seen(display_id, now=1000.0 + display_tokens.SEEN_WRITE_INTERVAL + 1)
    on_disk = json.loads(display_tokens.DISPLAYS_FILE.read_text())["displays"][0]
    assert on_disk["last_seen_at"] == 1000.0 + display_tokens.SEEN_WRITE_INTERVAL + 1
    # A battery change is written at once.
    registry.seen(display_id, battery=55, now=1700.0)
    assert json.loads(display_tokens.DISPLAYS_FILE.read_text())["displays"][0]["battery"] == 55


# -- the frame, from a payload ----------------------------------------------

# Local, as the server's own clock is: "since" is drawn in the Pi's time zone.
NOW = datetime(2026, 10, 5, 10, 0).astimezone()


def ago(**delta):
    return (NOW - timedelta(**delta)).isoformat()


def payload(**fields):
    base = {
        "site": "Cabin", "hub_connected": True, "sensors_enabled": True,
        "contact_count": 4, "open_contacts": [], "unavailable_sensors": [],
        "alarm": None, "weather_outlook": None, "outdoor": None, "rooms": [],
    }
    base.update(fields)
    return base


def contact(zone="Kitchen", sensor="Window", left_open=False, since=None):
    since = since or ago(minutes=13)
    return {"zone": zone, "sensor": sensor, "kind": "window", "since": since, "left_open": left_open}


def test_all_closed_is_green_and_says_so():
    frame = display_render.build_frame(payload(), now=NOW)
    assert frame.tone == "green" and frame.headline == "All closed" and frame.rows == []


def test_open_is_yellow_and_left_open_is_red_and_listed_first():
    frame = display_render.build_frame(payload(open_contacts=[contact()]), now=NOW)
    assert frame.tone == "yellow" and frame.headline == "1 open"
    # The weekday even today: the picture can stay up past midnight.
    assert frame.rows[0].colour == "yellow" and frame.rows[0].tag == "Mon 09:47"
    frame = display_render.build_frame(payload(open_contacts=[
        contact(zone="Kitchen"), contact(zone="Woodshed", sensor="Bod", left_open=True),
    ]), now=NOW)
    assert frame.tone == "red" and frame.headline == "Left open"
    assert [row.title for row in frame.rows] == ["Woodshed", "Kitchen"]
    assert frame.rows[0].colour == "red" and "left open" in frame.rows[0].detail


def test_an_open_day_old_contact_shows_its_date():
    frame = display_render.build_frame(
        payload(open_contacts=[contact(since=ago(hours=36, minutes=45))]), now=NOW,
    )
    assert frame.rows[0].tag.startswith("Sat ") and frame.rows[0].tag.endswith("21:15")


SILENT_DOOR = {"zone": "Loft", "sensor": "Hatch", "kind": "door"}
SILENT_THERMOMETER = {"zone": "Loft", "sensor": "Loft Temp", "kind": "climate"}


def test_a_silent_door_is_listed_in_blue_but_does_not_turn_the_panel_blue():
    frame = display_render.build_frame(payload(unavailable_sensors=[SILENT_DOOR]), now=NOW)
    assert frame.tone == "green" and frame.headline == "All closed"
    # Never counted as closed: the subline says how many cannot be heard.
    assert frame.subline == "3 closed · 1 not heard from"
    assert [(row.colour, row.detail) for row in frame.rows] == [("blue", "Hatch · no signal")]
    assert frame.calm is None


def test_an_entry_without_a_kind_is_still_a_door_or_window():
    frame = display_render.build_frame(
        payload(unavailable_sensors=[{"zone": "Loft", "sensor": "Hatch"}]), now=NOW,
    )
    assert [row.colour for row in frame.rows] == ["blue"]


def test_a_silent_thermometer_is_not_on_the_display_at_all():
    quiet = display_render.build_frame(payload(), now=NOW)
    frame = display_render.build_frame(payload(unavailable_sensors=[SILENT_THERMOMETER]), now=NOW)
    assert frame == quiet
    assert fingerprint(unavailable_sensors=[SILENT_THERMOMETER]) == fingerprint()
    assert light(unavailable_sensors=[SILENT_THERMOMETER]) == "green"


def test_the_payload_marks_a_silent_thermometer_so_the_panel_can_leave_it_out(client):
    enable(client)
    door = add_sensor(client, name="Hatch", kind="door")
    thermometer = add_sensor(client, name="Loft Temp", kind="climate")
    simulate(client, door["sensor_id"], available=False)
    simulate(client, thermometer["sensor_id"], available=False)
    body = client.get("/api/display").json()
    kinds = {item["sensor"]: item["kind"] for item in body["unavailable_sensors"]}
    assert kinds == {"Hatch": "door", "Loft Temp": "climate"}
    frame = display_render.build_frame(body)
    assert [row.detail for row in frame.rows if row.colour == "blue"] == ["Hatch · no signal"]
    assert frame.tone == "green" and display_render.status_light(body) == "green"


def test_a_room_near_freezing_still_shows_without_its_thermometer_row():
    frame = display_render.build_frame(payload(
        unavailable_sensors=[SILENT_THERMOMETER],
        rooms=[{"name": "Loft", "warnings": ["frost"]}],
    ), now=NOW)
    assert [(row.colour, row.detail) for row in frame.rows] == [("red", "Near freezing")]


def test_an_open_window_comes_before_a_silent_door_and_keeps_its_colour():
    frame = display_render.build_frame(
        payload(open_contacts=[contact()], unavailable_sensors=[SILENT_DOOR, SILENT_THERMOMETER]),
        now=NOW,
    )
    assert frame.tone == "yellow"
    assert [row.colour for row in frame.rows] == ["yellow", "blue"]


def test_the_alarm_left_open_warning_is_red_even_with_nothing_else():
    frame = display_render.build_frame(
        payload(alarm={"left_open": True, "reason": "locked_outside"}), now=NOW,
    )
    assert frame.tone == "red"
    assert frame.rows[0].title == "Locked up"


def test_without_sensors_the_display_says_so_rather_than_all_closed():
    frame = display_render.build_frame(payload(sensors_enabled=False), now=NOW)
    assert frame.tone == "blue" and frame.headline != "All closed"


def test_a_low_battery_is_noted_without_a_percentage_that_would_go_stale():
    frame = display_render.build_frame(payload(), display_battery=10, now=NOW)
    text = " ".join(note for _, note in frame.notes)
    assert "battery low" in text and "%" not in text
    assert display_render.build_frame(payload(), display_battery=60, now=NOW).notes == []


def test_the_hub_is_not_on_the_picture_because_it_is_not_redrawn_for_it():
    frame = display_render.build_frame(payload(hub_connected=False), now=NOW)
    assert frame.notes == [] and frame.tone == "green"


def test_the_footer_has_the_outdoor_temperature_and_outlook():
    frame = display_render.build_frame(payload(
        outdoor={"temperature": -3.6}, weather_outlook={"tendency": "falling"},
    ), now=NOW)
    assert "\u22124° at 10:00" in frame.footer and "Weather turning" in frame.footer
    assert (frame.outdoor, frame.outdoor_at, frame.outlook) == (-4, "10:00", "Weather turning")


def test_the_grip_wax_follows_the_swix_chart():
    cases = {
        -31: "polar", -20.1: "polar", -20: "green", -10.1: "green", -10: "blue",
        -3.1: "blue", -3: "violet", 0.9: "violet", 1: "red", 25: "red",
    }
    for temperature, wax in cases.items():
        assert display_render.grip_wax(temperature) == wax, temperature
    assert display_render.grip_wax(None) is None


def test_the_grip_wax_holds_its_colour_half_a_degree_past_an_edge():
    wax = display_render.grip_wax
    assert wax(-2.6, "blue") == "blue"
    assert wax(-2.4, "blue") == "violet"
    assert wax(-3.4, "violet") == "violet"
    assert wax(-3.6, "violet") == "blue"
    assert wax(0.6, "red") == "red"
    # Far from the colour it had, it does not hold on.
    assert wax(-25, "blue") == "polar"
    assert wax(-2.6, "nonsense") == "violet"


def test_the_picture_shows_the_wax_only_with_an_outdoor_reading():
    frame = display_render.build_frame(payload(outdoor={"temperature": -6.2}), now=NOW)
    assert frame.wax == "blue"
    assert display_render.build_frame(payload(), now=NOW).wax is None
    # The server's choice, with its margin, is what is drawn.
    held = display_render.build_frame(payload(outdoor={"temperature": -2.8}, grip_wax="blue"), now=NOW)
    assert held.wax == "blue"
    off = display_render.build_frame(payload(sensors_enabled=False, outdoor={"temperature": -12}), now=NOW)
    assert off.wax == "green"
    for temperature in (-24, -14, -6, -1, 3):
        png = display_render.render_png(payload(outdoor={"temperature": temperature}), now=NOW)
        image = Image.open(io.BytesIO(png))
        assert image.size == (display_render.WIDTH, display_render.HEIGHT)


def test_violet_is_drawn_in_red_and_blue_because_the_panel_has_no_violet():
    image = display_render.draw_frame(
        display_render.build_frame(payload(outdoor={"temperature": -1}), now=NOW),
    )
    box = image.crop((display_render.WAX_LEFT + 4, display_render.FOOTER_TOP + 42,
                      display_render.WIDTH - display_render.MARGIN - 4, display_render.FOOTER_TOP + 48))
    colours = set(box.getdata())
    assert {display_render.RED, display_render.BLUE} <= colours
    assert colours <= set(display_render.PALETTE)


def test_more_rows_than_fit_are_summed_up_not_dropped():
    many = [contact(zone=f"Room {index}") for index in range(12)]
    png = display_render.render_png(payload(open_contacts=many), now=NOW)
    image = Image.open(io.BytesIO(png))
    assert image.size == (display_render.WIDTH, display_render.HEIGHT)
    frame = display_render.build_frame(payload(open_contacts=many), now=NOW)
    assert frame.headline == "12 open"


def test_norwegian_letters_render():
    png = display_render.render_png(payload(open_contacts=[contact(zone="Bod ute", sensor="Dør på tørkerom")]), now=NOW)
    assert Image.open(io.BytesIO(png)).size == (display_render.WIDTH, display_render.HEIGHT)


# -- the Settings card ------------------------------------------------------



def _static(relative):
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    return (root / relative).read_text(encoding="utf-8")


def test_the_displays_card_is_admin_only_and_needs_sensors_on():
    cabin = _static("ui/cabin/cabin.js")
    assert ("if (!isAdmin || !state.me || !state.sensorSettings || "
            "!state.sensorSettings.enabled) return '';") in cabin
    assert "${renderDisplaySettingsCard(isAdmin)}" in cabin
    assert "wireDisplaySettings(root);" in cabin


def test_display_calls_go_through_the_shared_client_and_names_are_escaped():
    cabin = _static("ui/cabin/cabin.js")
    core = _static("ui/shared/core.js")
    assert "req('/api/displays')" in core
    assert "fetch('/api/displays" not in cabin
    assert "${esc(display.name)}" in cabin
    assert "value=\"${esc(created.token)}\"" in cabin
    # The key is shown once, from the create answer, and kept nowhere.
    assert "localStorage" not in cabin.split("function showDisplayKey")[1].split("function ")[0]


def test_the_light_legend_matches_what_the_lights_now_mean():
    # Blue is no longer a sensor nobody hears from; the firmware still shows
    # it, but only when the display itself cannot reach the Pi.
    legend = _static("ui/cabin/cabin.js").split('class="light-legend"')[1].split("</div>")[0]
    assert "a sensor cannot be heard from" not in legend
    assert "<b>Blinking blue</b> \\u2014 the display cannot reach the Pi" in legend
    assert "on battery the lights stay off" in legend.lower()


# -- redrawn only when a sensor changes ---------------------------------------


def fingerprint(**fields):
    return display_render.sensor_fingerprint(payload(**fields))


def test_the_fingerprint_ignores_weather_temperatures_and_the_hub():
    base = fingerprint()
    assert fingerprint(outdoor={"temperature": -3.6}) == fingerprint(outdoor={"temperature": -8})
    assert fingerprint(weather_outlook={"tendency": "storm"}) == base
    assert fingerprint(hub_connected=False) == base
    assert fingerprint(site="Elsewhere") == base
    assert fingerprint(rooms=[{"name": "Kitchen", "actual_temperature": 21.5, "warnings": []}]) == base
    assert fingerprint(rooms=[{"name": "Kitchen", "warnings": ["humid", "too_warm"]}]) == base


def test_a_new_grip_wax_redraws_the_panel():
    blue = fingerprint(outdoor={"temperature": -6})
    assert fingerprint() != blue
    assert fingerprint(outdoor={"temperature": -14}) != blue
    assert fingerprint(outdoor={"temperature": -2.8}, grip_wax="blue") == blue


def test_the_server_holds_the_wax_across_an_edge(monkeypatch):
    monkeypatch.setattr(server, "_display_wax", {})
    readings = iter([-3.4, -2.8, -2.6, -2.4, -2.8, -3.4, -3.6])
    monkeypatch.setattr(server, "_display_outdoor", lambda: {"temperature": next(readings)})
    shown = [server._display_payload()["grip_wax"] for _ in range(7)]
    assert shown == ["blue", "blue", "blue", "violet", "violet", "violet", "blue"]


def test_the_fingerprint_follows_every_sensor_change():
    base = fingerprint()
    changed = {
        "open": fingerprint(open_contacts=[contact()]),
        "left open": fingerprint(open_contacts=[contact(left_open=True)]),
        "unavailable": fingerprint(unavailable_sensors=[SILENT_DOOR]),
        "alarm": fingerprint(alarm={"left_open": True, "reason": "armed_away"}),
        "frost": fingerprint(rooms=[{"name": "Loft", "warnings": ["frost"]}]),
        "count": fingerprint(contact_count=5),
        "disabled": fingerprint(sensors_enabled=False),
    }
    assert base not in changed.values()
    assert len(set(changed.values())) == len(changed)
    assert changed["open"] != changed["left open"]
    # The order the rooms come in is not a change.
    two = [contact(zone="A"), contact(zone="B")]
    assert fingerprint(open_contacts=two) == fingerprint(open_contacts=list(reversed(two)))


def test_a_new_outdoor_reading_does_not_redraw_the_panel(client, device, monkeypatch):
    enable(client)
    add_sensor(client, name="Bath window")
    _, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    original = server._display_payload

    def warmer():
        value = original()
        value["outdoor"] = {"temperature": 31.0}
        value["hub_connected"] = not value["hub_connected"]
        return value

    monkeypatch.setattr(server, "_display_payload", warmer)
    again = device.get(
        "/api/display/frame.png",
        headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304


def test_a_waiting_request_answers_as_soon_as_a_sensor_changes(client, device, monkeypatch):
    enable(client)
    add_sensor(client, name="Bath window")
    _, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    original = server._display_payload
    calls = {"n": 0}

    def opens_on_the_third_look():
        calls["n"] += 1
        value = original()
        if calls["n"] >= 3:
            value["open_contacts"] = [contact()]
        return value

    monkeypatch.setattr(server, "_display_payload", opens_on_the_third_look)
    monkeypatch.setattr(server, "DISPLAY_WAIT_STEP_SECONDS", 0.01)
    started = time.monotonic()
    response = device.get(
        "/api/display/frame.png?wait=30",
        headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert response.status_code == 200 and response.content
    assert response.headers["etag"] != first.headers["etag"]
    assert calls["n"] == 3
    assert time.monotonic() - started < 5


def test_a_waiting_request_gives_up_with_304_when_nothing_changes(client, device, monkeypatch):
    _, token = new_key(client)
    etag = device.get("/api/display/frame.png", headers=bearer(token)).headers["etag"]
    monkeypatch.setattr(server, "DISPLAY_WAIT_STEP_SECONDS", 0.05)
    started = time.monotonic()
    response = device.get(
        "/api/display/frame.png?wait=1", headers={**bearer(token), "If-None-Match": etag},
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 304 and response.headers["etag"] == etag
    assert 0.9 <= elapsed < 5


def test_the_wait_is_capped_and_needs_an_etag(client, device, monkeypatch):
    _, token = new_key(client)
    # Without If-None-Match there is nothing to wait for a change from.
    started = time.monotonic()
    assert device.get("/api/display/frame.png?wait=30", headers=bearer(token)).status_code == 200
    assert time.monotonic() - started < 5
    etag = device.get("/api/display/frame.png", headers=bearer(token)).headers["etag"]
    monkeypatch.setattr(server, "DISPLAY_MAX_WAIT_SECONDS", 0)
    started = time.monotonic()
    response = device.get(
        "/api/display/frame.png?wait=99999", headers={**bearer(token), "If-None-Match": etag},
    )
    assert response.status_code == 304 and time.monotonic() - started < 5


def test_an_older_strong_etag_from_the_first_firmware_still_matches(client, device):
    _, token = new_key(client)
    etag = device.get("/api/display/frame.png", headers=bearer(token)).headers["etag"]
    assert etag.startswith('W/"')
    response = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": etag[2:]},
    )
    assert response.status_code == 304


# -- how often a display on battery asks ----------------------------------------


def test_every_answer_tells_the_display_its_battery_interval(client, device):
    display, token = new_key(client)
    assert display["battery_minutes"] == display_tokens.DEFAULT_BATTERY_MINUTES
    first = device.get("/api/display/frame.png", headers=bearer(token))
    assert first.headers["x-display-interval"] == str(display_tokens.DEFAULT_BATTERY_MINUTES)
    response = client.patch(f"/api/displays/{display['display_id']}", json={"battery_minutes": 5})
    assert response.status_code == 200, response.text
    assert response.json()["display"]["battery_minutes"] == 5
    again = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304 and again.headers["x-display-interval"] == "5"
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.battery_minutes(display["display_id"]) == 5


@pytest.mark.parametrize("value", [0, 1, 7, 61, 240, -5, "5", True, None])
def test_only_the_offered_battery_intervals_are_accepted(client, value):
    display, _ = new_key(client)
    response = client.patch(f"/api/displays/{display['display_id']}", json={"battery_minutes": value})
    if value == "5":
        # Pydantic accepts a numeric string for an int; it is still a choice.
        assert response.status_code == 200
        return
    assert response.status_code in (400, 422), value
    listed = client.get("/api/displays").json()["displays"][0]
    assert listed["battery_minutes"] == display_tokens.DEFAULT_BATTERY_MINUTES


def test_changing_the_interval_is_for_admins_with_a_session(client, device, monkeypatch):
    display, token = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    assert client.patch("/api/displays/nope", json={"battery_minutes": 5}).status_code == 404
    # A display's own key cannot change how often it asks.
    assert device.patch(path, json={"battery_minutes": 2}, headers=bearer(token)).status_code == 401
    assert device.patch(path, json={"battery_minutes": 2}).status_code == 401
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.patch(path, json={"battery_minutes": 2}).status_code == 403


def test_a_damaged_interval_or_power_on_disk_falls_back_safely():
    registry = display_tokens.DisplayRegistry()
    display, _ = registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    raw["displays"][0]["battery_minutes"] = 3
    raw["displays"][0]["power"] = "mains"
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    listed = reloaded.list()[0]
    assert listed["battery_minutes"] == display_tokens.DEFAULT_BATTERY_MINUTES
    assert listed["power"] is None


def test_the_display_says_whether_it_is_on_usb_or_battery(client, device):
    _, token = new_key(client)
    device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Power": "usb"})
    assert client.get("/api/displays").json()["displays"][0]["power"] == "usb"
    device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Power": "battery"})
    assert client.get("/api/displays").json()["displays"][0]["power"] == "battery"
    device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Power": "nuclear"})
    assert client.get("/api/displays").json()["displays"][0]["power"] == "battery"
    on_disk = json.loads(display_tokens.DISPLAYS_FILE.read_text())["displays"][0]
    assert on_disk["power"] == "battery"


def test_settings_offers_exactly_the_intervals_the_server_accepts():
    root = server.Path(server.__file__).resolve().parent / "static" / "ui"
    cabin = (root / "cabin" / "cabin.js").read_text()
    core = (root / "shared" / "core.js").read_text()
    offered = "[" + ", ".join(str(item) for item in display_tokens.BATTERY_MINUTES_CHOICES) + "]"
    assert f"const DISPLAY_BATTERY_MINUTES = {offered};" in cabin
    assert "data-display-interval" in cabin and "Nobo.api.updateDisplay" in cabin
    assert "updateDisplay:" in core and "method: 'PATCH'" in core


# -- the lights, on USB power ---------------------------------------------------


def light(**fields):
    return display_render.status_light(payload(**fields))


def test_the_light_is_the_headline_from_afar():
    assert light() == "green"
    assert light(open_contacts=[contact()]) == "yellow"
    assert light(open_contacts=[contact(left_open=True)]) == "red"
    assert light(open_contacts=[contact(), contact(zone="Loft", left_open=True)]) == "red"
    assert light(alarm={"left_open": True, "reason": "armed_away"}) == "red"
    assert light(open_contacts=[contact()], alarm={"left_open": True, "reason": "armed_away"}) == "red"
    # A silent sensor is listed on the panel, never signalled by the light.
    assert light(unavailable_sensors=[SILENT_DOOR]) == "green"
    assert light(open_contacts=[contact()], unavailable_sensors=[SILENT_DOOR]) == "yellow"
    assert light(contact_count=0) == "off"
    assert light(sensors_enabled=False, open_contacts=[contact()]) == "off"
    assert set(display_render.LIGHTS) == {"green", "yellow", "red", "blue", "off"}


def test_older_firmware_is_told_red_rather_than_a_yellow_it_cannot_show():
    for_firmware = display_render.light_for_firmware
    assert for_firmware("yellow", "1.4.0") == "yellow"
    assert for_firmware("yellow", "1.10.2") == "yellow"
    assert for_firmware("yellow", "2.0") == "yellow"
    for old in ("1.3.9", "1.2.0", None, "", "dev", "1"):
        assert for_firmware("yellow", old) == "red", old
    for other in ("green", "red", "blue", "off"):
        assert for_firmware(other, "1.3.9") == other


def test_the_firmware_knows_every_light_the_pi_sends():
    firmware = (
        server.Path(server.__file__).resolve().parent.parent
        / "display" / "papercolor" / "src" / "main.cpp"
    ).read_text()
    for name in display_render.LIGHTS:
        if name != "off":
            assert f'value == "{name}"' in firmware, name
    version = firmware.split('#define FIRMWARE_VERSION "', 1)[1].split('"', 1)[0]
    assert display_render.light_for_firmware("yellow", version) == "yellow"


def test_the_light_never_changes_without_the_etag_changing():
    """A waiting display only hears of what moves the fingerprint."""
    cases = [
        {}, {"open_contacts": [contact()]}, {"open_contacts": [contact(left_open=True)]},
        {"alarm": {"left_open": True, "reason": "armed_away"}},
        {"unavailable_sensors": [SILENT_DOOR]}, {"unavailable_sensors": [SILENT_THERMOMETER]},
        {"contact_count": 0}, {"sensors_enabled": False},
    ]
    seen = {}
    for fields in cases:
        seen.setdefault(fingerprint(**fields), set()).add(light(**fields))
    assert all(len(lights) == 1 for lights in seen.values())


def test_the_light_rides_on_every_answer_and_follows_the_sensors(client, device):
    enable(client)
    sensor = add_sensor(client, name="Bath window")
    display, token = new_key(client)
    assert display["light"] is True
    first = device.get("/api/display/frame.png", headers=bearer(token))
    assert first.headers["x-display-light"] == "green"
    same = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert same.status_code == 304 and same.headers["x-display-light"] == "green"
    simulate(client, sensor["sensor_id"], state="open")
    opened = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert opened.status_code == 200 and opened.headers["x-display-light"] == "red"
    # Red above only because this display has not said what it runs.
    newer = {**bearer(token), "X-Display-Firmware": "1.4.0"}
    assert device.get("/api/display/frame.png", headers=newer).headers["x-display-light"] == "yellow"


def test_the_light_can_be_turned_off_per_display_and_stays_off(client, device):
    enable(client)
    sensor = add_sensor(client, name="Bath window")
    simulate(client, sensor["sensor_id"], state="open")
    display, token = new_key(client)
    other, other_token = new_key(client, "Kitchen")
    path = f"/api/displays/{display['display_id']}"
    response = client.patch(path, json={"light": False})
    assert response.status_code == 200, response.text
    assert response.json()["display"]["light"] is False
    assert device.get("/api/display/frame.png", headers=bearer(token)).headers["x-display-light"] == "off"
    assert device.get("/api/display/frame.png", headers=bearer(other_token)).headers["x-display-light"] == "red"
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.light(display["display_id"]) is False
    assert reloaded.light(other["display_id"]) is True
    assert client.patch(path, json={"light": True}).json()["display"]["light"] is True


@pytest.mark.parametrize("body", [{}, {"light": "yes"}, {"light": 1}, {"light": None}])
def test_a_light_change_must_be_a_real_boolean(client, body):
    display, _ = new_key(client)
    response = client.patch(f"/api/displays/{display['display_id']}", json=body)
    assert response.status_code in (400, 422), body
    assert client.get("/api/displays").json()["displays"][0]["light"] is True


def test_the_light_is_for_admins_with_a_session(client, device, monkeypatch):
    display, token = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    assert client.patch("/api/displays/nope", json={"light": False}).status_code == 404
    assert device.patch(path, json={"light": False}, headers=bearer(token)).status_code == 401
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.patch(path, json={"light": False}).status_code == 403


def test_a_display_saved_before_the_lights_existed_has_them_on():
    registry = display_tokens.DisplayRegistry()
    display, _ = registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    del raw["displays"][0]["light"]
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.list()[0]["light"] is True


def test_settings_offers_the_light_switch():
    root = server.Path(server.__file__).resolve().parent / "static" / "ui"
    cabin = (root / "cabin" / "cabin.js").read_text()
    assert "data-display-light" in cabin
    assert "{ light: false }" in cabin
    assert "{ light: true, light_level: select.value }" in cabin


def test_settings_offers_exactly_the_light_levels_the_server_accepts():
    root = server.Path(server.__file__).resolve().parent / "static" / "ui"
    cabin = (root / "cabin" / "cabin.js").read_text()
    offered = ", ".join(f"['{key}', '{key.capitalize()}']" for key in display_tokens.LIGHT_LEVELS)
    assert f"const DISPLAY_LIGHT_LEVELS = [{offered}];" in cabin


# -- how bright -------------------------------------------------------------------


def test_the_light_level_rides_on_every_answer_and_starts_dim(client, device):
    enable(client)
    add_sensor(client, name="Bath window")
    display, token = new_key(client)
    assert display["light_level"] == "dim"
    first = device.get("/api/display/frame.png", headers=bearer(token))
    assert first.headers["x-display-light-level"] == str(display_tokens.LIGHT_LEVELS["dim"])
    same = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert same.status_code == 304
    assert same.headers["x-display-light-level"] == str(display_tokens.LIGHT_LEVELS["dim"])


def test_the_dim_level_is_dimmer_than_the_old_fixed_one():
    levels = display_tokens.LIGHT_LEVELS
    assert levels["dim"] < levels["medium"] < levels["bright"] <= 48
    assert all(1 <= level <= 255 for level in levels.values())


def test_the_light_level_is_set_per_display_and_survives_a_restart(client, device):
    display, token = new_key(client)
    other, other_token = new_key(client, "Kitchen")
    path = f"/api/displays/{display['display_id']}"
    response = client.patch(path, json={"light_level": "bright"})
    assert response.status_code == 200, response.text
    assert response.json()["display"]["light_level"] == "bright"
    assert response.json()["display"]["light"] is True
    bright = str(display_tokens.LIGHT_LEVELS["bright"])
    dim = str(display_tokens.LIGHT_LEVELS["dim"])
    assert device.get("/api/display/frame.png", headers=bearer(token)).headers["x-display-light-level"] == bright
    assert device.get("/api/display/frame.png", headers=bearer(other_token)).headers["x-display-light-level"] == dim
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.light_level(display["display_id"]) == display_tokens.LIGHT_LEVELS["bright"]
    assert reloaded.light_level(other["display_id"]) == display_tokens.LIGHT_LEVELS["dim"]


def test_settings_turns_the_lights_on_and_sets_the_level_in_one_change(client):
    display, _ = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    assert client.patch(path, json={"light": False}).status_code == 200
    changed = client.patch(path, json={"light": True, "light_level": "medium"}).json()["display"]
    assert changed["light"] is True and changed["light_level"] == "medium"


@pytest.mark.parametrize("level", ["blinding", "", "Dim", 6, True, None])
def test_a_bad_light_level_changes_nothing(client, level):
    display, _ = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    response = client.patch(path, json={"light_level": level, "battery_minutes": 60})
    if level is None:
        # Only the interval was sent.
        assert response.status_code == 200
        return
    assert response.status_code in (400, 422), level
    saved = client.get("/api/displays").json()["displays"][0]
    assert saved["light_level"] == "dim" and saved["battery_minutes"] == display["battery_minutes"]


def test_a_display_saved_before_the_level_existed_is_dim():
    registry = display_tokens.DisplayRegistry()
    registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    del raw["displays"][0]["light_level"]
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.list()[0]["light_level"] == "dim"
    raw["displays"][0]["light_level"] = "nonsense"
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded.load()
    assert reloaded.list()[0]["light_level"] == "dim"


# -- "Redraw now" -------------------------------------------------------------------


def test_redraw_now_makes_that_display_fetch_the_picture_again(client, device):
    display, token = new_key(client)
    other, other_token = new_key(client, "Kitchen")
    first = device.get("/api/display/frame.png", headers=bearer(token))
    other_first = device.get("/api/display/frame.png", headers=bearer(other_token))
    assert first.status_code == 200
    same = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert same.status_code == 304

    response = client.post(f"/api/displays/{display['display_id']}/redraw")
    assert response.status_code == 200, response.text
    asked = response.json()["display"]
    assert asked["redraw_asked_at"] >= asked["picture_sent_at"]

    again = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 200 and again.headers["etag"] != first.headers["etag"]
    assert again.headers["content-type"] == "image/png"
    settled = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": again.headers["etag"]},
    )
    assert settled.status_code == 304
    # Only the display that was asked.
    untouched = device.get(
        "/api/display/frame.png",
        headers={**bearer(other_token), "If-None-Match": other_first.headers["etag"]},
    )
    assert untouched.status_code == 304
    listed = {item["display_id"]: item for item in client.get("/api/displays").json()["displays"]}
    shown = listed[display["display_id"]]
    assert shown["picture_sent_at"] >= shown["redraw_asked_at"]
    assert listed[other["display_id"]]["redraw_asked_at"] is None


def test_a_held_ask_is_answered_soon_after_redraw_now(client, device, monkeypatch):
    display, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    monkeypatch.setattr(server, "DISPLAY_WAIT_STEP_SECONDS", 0.05)
    original = server._display_payload
    calls = {"n": 0}

    def payload():
        calls["n"] += 1
        if calls["n"] == 2:
            server.display_registry.ask_redraw(display["display_id"])
        return original()

    monkeypatch.setattr(server, "_display_payload", payload)
    started = time.monotonic()
    held = device.get(
        "/api/display/frame.png?wait=5",
        headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert held.status_code == 200
    assert time.monotonic() - started < 3


def test_redraw_now_survives_a_restart(client, device):
    display, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    assert client.post(f"/api/displays/{display['display_id']}/redraw").status_code == 200
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.redraw(display["display_id"]) == 1
    listed = reloaded.list()[0]
    assert listed["redraw_asked_at"] and listed["picture_sent_at"]
    assert first.status_code == 200


def test_a_display_saved_before_redraw_existed_loads_with_none_asked():
    registry = display_tokens.DisplayRegistry()
    registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    for field in ("redraw", "redraw_asked_at", "picture_sent_at"):
        raw["displays"][0].pop(field, None)
    raw["displays"][0]["redraw"] = "lots"
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    item = reloaded.list()[0]
    assert reloaded.redraw(item["display_id"]) == 0
    assert item["redraw_asked_at"] is None and item["picture_sent_at"] is None


def test_redraw_now_is_for_admins_with_a_session(client, device, monkeypatch):
    display, token = new_key(client)
    path = f"/api/displays/{display['display_id']}/redraw"
    assert client.post("/api/displays/nosuch/redraw").status_code == 404
    assert device.post(path, follow_redirects=False).status_code == 401
    assert device.post(path, headers=bearer(token)).status_code == 401
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.post(path).status_code == 403


def test_the_preview_in_settings_keeps_the_plain_etag(client):
    display, _ = new_key(client)
    before = client.get("/api/display/frame.png").headers["etag"]
    client.post(f"/api/displays/{display['display_id']}/redraw")
    assert client.get("/api/display/frame.png").headers["etag"] == before


def test_settings_offers_redraw_now():
    root = server.Path(server.__file__).resolve().parent / "static" / "ui"
    cabin = (root / "cabin" / "cabin.js").read_text()
    core = (root / "shared" / "core.js").read_text()
    assert "data-redraw-display" in cabin and "Nobo.api.redrawDisplay(" in cabin
    assert "/redraw`, { method: 'POST' }" in core


# -- what the display says about its own redraws ------------------------------------


def test_each_refresh_the_display_reports_is_kept_once(client, device):
    display, token = new_key(client)
    report = {**bearer(token), "X-Display-Refresh": "n=3;ms=15234;busy=0;chip=31"}
    device.get("/api/display/frame.png", headers=report)
    device.get("/api/display/frame.png", headers=report)
    device.get(
        "/api/display/frame.png",
        headers={**bearer(token), "X-Display-Refresh": "n=4;ms=9100;busy=1;chip=12"},
    )
    shown = client.get("/api/displays").json()["displays"][0]
    assert [(r["ms"], r["busy"], r["chip_c"]) for r in shown["refreshes"]] == [
        (15234, False, 31), (9100, True, 12),
    ]
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.list()[0]["refreshes"] == shown["refreshes"]


def test_a_restarted_count_is_a_new_refresh_and_only_twenty_are_kept(client, device):
    _, token = new_key(client)
    for n in [*range(1, 25), 1]:
        device.get(
            "/api/display/frame.png",
            headers={**bearer(token), "X-Display-Refresh": f"n={n};ms={n * 1000};busy=0;chip=20"},
        )
    kept = client.get("/api/displays").json()["displays"][0]["refreshes"]
    assert len(kept) == display_tokens.MAX_REFRESHES
    assert kept[-1]["ms"] == 1000 and kept[-2]["ms"] == 24000


@pytest.mark.parametrize("value", [
    "", "n=1;ms=1", "n=0;ms=1;busy=0;chip=1", "n=1;ms=-1;busy=0;chip=1",
    "n=1;ms=1;busy=2;chip=1", "n=1;ms=1;busy=0;chip=900", "n=x;ms=1;busy=0;chip=1",
    "n=1;ms=1;busy=0;chip=1;extra=2", "n=1;" * 30,
])
def test_a_garbled_refresh_report_is_ignored(client, device, value):
    _, token = new_key(client)
    response = device.get(
        "/api/display/frame.png", headers={**bearer(token), "X-Display-Refresh": value},
    )
    assert response.status_code == 200
    assert client.get("/api/displays").json()["displays"][0]["refreshes"] == []


def test_only_a_restart_the_display_did_not_choose_is_recorded(client, device):
    _, token = new_key(client)
    for reason in ("wake", "power_on", "software", "nonsense"):
        device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Reset": reason})
    assert client.get("/api/displays").json()["displays"][0]["last_restart"] is None
    device.get("/api/display/frame.png", headers={**bearer(token), "X-Display-Reset": "watchdog"})
    restart = client.get("/api/displays").json()["displays"][0]["last_restart"]
    assert restart["reason"] == "watchdog" and restart["at"] > 0
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.list()[0]["last_restart"] == restart


def test_damaged_refresh_records_load_as_none():
    registry = display_tokens.DisplayRegistry()
    registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    raw["displays"][0]["refreshes"] = [
        {"at": 5.0, "ms": 1000, "busy": False, "chip_c": 20}, "junk", {"at": "x", "ms": 1},
    ]
    raw["displays"][0]["last_restart"] = {"reason": "rm -rf", "at": 5}
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    item = reloaded.list()[0]
    assert item["refreshes"] == [{"at": 5.0, "ms": 1000, "busy": False, "chip_c": 20}]
    assert item["last_restart"] is None


def test_settings_shows_what_the_display_reported():
    root = server.Path(server.__file__).resolve().parent / "static" / "ui"
    cabin = (root / "cabin" / "cabin.js").read_text()
    assert "data-display-health" in cabin and "Last redraw took" in cabin
    for reason in display_tokens.UNEXPECTED_RESTARTS:
        assert f"{reason}:" in cabin, reason


def test_the_firmware_sends_what_the_pi_parses():
    firmware = (
        server.Path(server.__file__).resolve().parent.parent
        / "display" / "papercolor" / "src" / "main.cpp"
    ).read_text()
    assert '"n=%lu;ms=%lu;busy=%d;chip=%d"' in firmware
    assert display_tokens.parse_refresh("n=1;ms=15000;busy=0;chip=31")
    for reason in display_tokens.UNEXPECTED_RESTARTS:
        assert f'return "{reason}";' in firmware, reason


def test_the_picture_a_redraw_asked_for_says_draw_it_now(client, device):
    # 20 October 2026: "Redraw now" pressed just after a door closed was kept
    # for the panel's three-minute rest, and the display looked stuck.
    display, token = new_key(client)
    first = device.get("/api/display/frame.png", headers=bearer(token))
    assert first.status_code == 200 and "x-display-redraw" not in first.headers
    client.post(f"/api/displays/{display['display_id']}/redraw")
    again = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 200 and again.headers["x-display-redraw"] == "now"
    # Once only: the next picture, sent because a door changed, is not.
    fresh = device.get("/api/display/frame.png", headers=bearer(token))
    assert fresh.status_code == 200 and "x-display-redraw" not in fresh.headers
    held = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": again.headers["etag"]},
    )
    assert held.status_code == 304 and "x-display-redraw" not in held.headers


def test_a_redraw_is_pending_until_a_picture_is_sent():
    registry = display_tokens.DisplayRegistry()
    display, _ = registry.create("Hall")
    display_id = display["display_id"]
    assert not registry.redraw_pending(display_id)
    assert not registry.redraw_pending("nosuch")
    registry.ask_redraw(display_id, now=100.0)
    assert registry.redraw_pending(display_id)
    registry.picture_sent(display_id, now=99.0)
    assert registry.redraw_pending(display_id)
    registry.picture_sent(display_id, now=101.0)
    assert not registry.redraw_pending(display_id)


def test_the_firmware_draws_at_once_when_asked():
    firmware = (
        server.Path(server.__file__).resolve().parent.parent
        / "display" / "papercolor" / "src" / "main.cpp"
    ).read_text()
    assert '"X-Display-Redraw"' in firmware
    assert 'http.header("X-Display-Redraw") == "now"' in firmware
    # Only a picture nobody asked for waits for the panel to rest.
    assert "if (usbPower && !now && (!panelRested() || cycling()))" in firmware


# -- grip or glide wax, or none ------------------------------------------------


def test_the_glide_wax_follows_the_swix_table_split_where_it_overlaps():
    cases = {
        -25: "turquoise", -11.1: "turquoise", -11: "blue", -7.1: "blue", -7: "violet",
        -3.1: "violet", -3: "red", 1.9: "red", 2: "yellow", 15: "yellow",
    }
    for temperature, wax in cases.items():
        assert display_render.glide_wax(temperature) == wax, temperature
    assert display_render.glide_wax(None) is None
    assert display_render.wax_for("nonsense", -5) is None
    # Every edge lies inside the overlap of the two colours either side of it.
    table = {"turquoise": (-18, -10), "blue": (-12, -6), "violet": (-8, -2),
             "red": (-4, 4), "yellow": (0, 10)}
    chart = display_render.GLIDE_WAX
    for (cold, _, edge), (warm, _, _) in zip(chart, chart[1:]):
        assert table[warm][0] <= edge <= table[cold][1], (cold, warm)


def test_the_glide_wax_holds_its_colour_half_a_degree_past_an_edge():
    wax = display_render.glide_wax
    assert wax(2.4, "red") == "red"
    assert wax(2.6, "red") == "yellow"
    assert wax(1.6, "yellow") == "yellow"
    # A grip-only colour is no reason to hold on.
    assert wax(-25, "polar") == "turquoise"


def test_the_picture_shows_the_wax_the_display_chose():
    reading = payload(outdoor={"temperature": 3})
    grip = display_render.build_frame(reading, now=NOW, wax_kind="grip")
    glide = display_render.build_frame(reading, now=NOW, wax_kind="glide")
    off = display_render.build_frame(reading, now=NOW, wax_kind=None)
    assert (grip.wax, grip.wax_kind) == ("red", "grip")
    assert (glide.wax, glide.wax_kind) == ("yellow", "glide")
    assert off.wax is None and off.outdoor == 3
    # The server's held colour is per kind.
    held = display_render.build_frame(
        payload(outdoor={"temperature": 2.3}, glide_wax="red", grip_wax="red"),
        now=NOW, wax_kind="glide",
    )
    assert held.wax == "red"


def test_turning_the_wax_off_or_changing_kind_redraws_the_panel():
    reading = {"outdoor": {"temperature": -6}}
    grip = display_render.sensor_fingerprint(payload(**reading), wax_kind="grip")
    glide = display_render.sensor_fingerprint(payload(**reading), wax_kind="glide")
    off = display_render.sensor_fingerprint(payload(**reading), wax_kind=None)
    assert len({grip, glide, off}) == 3
    # With no wax shown, the temperature outside is not a reason to redraw.
    assert off == display_render.sensor_fingerprint(payload(outdoor={"temperature": 5}), wax_kind=None)
    assert off == display_render.sensor_fingerprint(payload(), wax_kind="glide")


def _wax_box(image):
    return image.crop((display_render.WAX_LEFT + 4, display_render.FOOTER_TOP + 42,
                       display_render.WIDTH - display_render.MARGIN - 4, display_render.FOOTER_TOP + 48))


def test_glide_colours_are_drawn_in_the_panel_inks():
    def box(temperature):
        frame = display_render.build_frame(
            payload(outdoor={"temperature": temperature}), now=NOW, wax_kind="glide",
        )
        return set(_wax_box(display_render.draw_frame(frame)).getdata())

    assert {display_render.BLUE, display_render.GREEN} <= box(-15)
    assert display_render.YELLOW in box(6)
    assert {display_render.RED, display_render.BLUE} <= box(-5)
    for temperature in (-15, -9, -5, 0, 6):
        assert box(temperature) <= set(display_render.PALETTE)
        png = display_render.render_png(
            payload(outdoor={"temperature": temperature}), now=NOW, wax_kind="glide",
        )
        assert Image.open(io.BytesIO(png)).size == (display_render.WIDTH, display_render.HEIGHT)


def test_with_the_wax_off_the_footer_has_no_wax_box():
    reading = payload(outdoor={"temperature": -6})
    image = display_render.draw_frame(display_render.build_frame(reading, now=NOW, wax_kind=None))
    assert set(_wax_box(image).getdata()) == {display_render.WHITE}


def test_the_server_holds_each_kind_of_wax_on_its_own(monkeypatch):
    monkeypatch.setattr(server, "_display_wax", {})
    readings = iter([1.6, 2.3, 2.6, 0.6])
    monkeypatch.setattr(server, "_display_outdoor", lambda: {"temperature": next(readings)})
    shown = [
        (value["grip_wax"], value["glide_wax"])
        for value in (server._display_payload() for _ in range(4))
    ]
    # Grip red holds at 0.6, half a degree from its edge at 1; glide yellow,
    # whose edge is at 2, does not.
    assert shown == [("red", "red"), ("red", "red"), ("red", "yellow"), ("red", "red")]


def test_the_wax_is_grip_by_default_set_per_display_and_survives_a_restart(client, device, monkeypatch):
    enable(client)
    add_sensor(client, name="Bath window")
    monkeypatch.setattr(server, "_display_outdoor", lambda: {"temperature": -6.0})
    display, token = new_key(client)
    other, other_token = new_key(client, "Kitchen")
    assert display["wax"] == "grip"
    first = device.get("/api/display/frame.png", headers=bearer(token))
    path = f"/api/displays/{display['display_id']}"
    response = client.patch(path, json={"wax": "glide"})
    assert response.status_code == 200, response.text
    assert response.json()["display"]["wax"] == "glide"
    # The new choice is drawn at once: the ETag has moved.
    again = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 200
    assert client.patch(path, json={"wax": "off"}).status_code == 200
    off = device.get(
        "/api/display/frame.png", headers={**bearer(token), "If-None-Match": again.headers["etag"]},
    )
    assert off.status_code == 200
    # The other display is untouched.
    assert device.get(
        "/api/display/frame.png", headers=bearer(other_token),
    ).headers["etag"] == first.headers["etag"]
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.wax(display["display_id"]) == "off"
    assert reloaded.wax(other["display_id"]) == "grip"
    log = json.dumps(client.get("/api/log").json()["entries"])
    assert "shows glide wax" in log and "shows no ski wax" in log


def test_a_display_key_cannot_choose_its_own_wax(client, device, monkeypatch):
    enable(client)
    add_sensor(client, name="Bath window")
    monkeypatch.setattr(server, "_display_outdoor", lambda: {"temperature": -6.0})
    _, token = new_key(client)
    plain = device.get("/api/display/frame.png", headers=bearer(token)).headers["etag"]
    asked = device.get("/api/display/frame.png?wax=glide", headers=bearer(token)).headers["etag"]
    assert plain == asked
    # The Settings preview, with a session and no key, may.
    grip = client.get("/api/display/frame.png").headers["etag"]
    glide = client.get("/api/display/frame.png?wax=glide").headers["etag"]
    assert grip != glide
    assert client.get("/api/display/frame.png?wax=nonsense").headers["etag"] == grip


@pytest.mark.parametrize("wax", ["Glide", "", "none", 1, True])
def test_a_bad_wax_changes_nothing(client, wax):
    display, _ = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    response = client.patch(path, json={"wax": wax, "battery_minutes": 60})
    assert response.status_code in (400, 422), wax
    saved = client.get("/api/displays").json()["displays"][0]
    assert saved["wax"] == "grip" and saved["battery_minutes"] == display["battery_minutes"]


def test_the_wax_choice_needs_an_admin_session(client, device, monkeypatch):
    display, token = new_key(client)
    path = f"/api/displays/{display['display_id']}"
    assert device.patch(path, json={"wax": "off"}, headers=bearer(token)).status_code in (401, 403)
    assert client.get("/api/displays").json()["displays"][0]["wax"] == "grip"


def test_a_display_saved_before_the_wax_choice_shows_grip_wax():
    registry = display_tokens.DisplayRegistry()
    registry.create("Hall")
    raw = json.loads(display_tokens.DISPLAYS_FILE.read_text())
    del raw["displays"][0]["wax"]
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded = display_tokens.DisplayRegistry()
    reloaded.load()
    assert reloaded.list()[0]["wax"] == "grip"
    raw["displays"][0]["wax"] = "klister"
    display_tokens.DISPLAYS_FILE.write_text(json.dumps(raw))
    reloaded.load()
    assert reloaded.list()[0]["wax"] == "grip"


def test_settings_offers_exactly_the_waxes_the_server_accepts():
    cabin = _static("ui/cabin/cabin.js")
    labels = {"grip": "Grip wax", "glide": "Glide wax", "off": "Off"}
    offered = ", ".join(f"['{key}', '{labels[key]}']" for key in display_tokens.DISPLAY_WAX_CHOICES)
    assert f"const DISPLAY_WAX = [{offered}];" in cabin
    assert set(display_tokens.DISPLAY_WAX_CHOICES) == set(display_render.WAX_CHARTS) | {"off"}
    assert "data-display-wax" in cabin
    assert "{ wax: select.value }" in cabin
    assert "frame.png?wax=${encodeURIComponent(" in cabin


def _digit_rows(temperature, wax_kind):
    frame = display_render.build_frame(
        payload(outdoor={"temperature": temperature}), now=NOW, wax_kind=wax_kind,
    )
    image = display_render.draw_frame(frame)
    return [y for y in range(display_render.FOOTER_TOP + 12, display_render.HEIGHT)
            if any(image.getpixel((x, y)) == display_render.BLACK
                   for x in range(display_render.MARGIN + 30, display_render.MARGIN + 60))]


def test_the_temperature_is_drawn_at_one_size_whatever_it_reads():
    # A figure that grows on a mild day and shrinks in a frost reads as a
    # change of meaning. Every reading is the size "-24" needs, wax or none.
    sizes = {(t, kind): _digit_rows(t, kind)
             for t in (-24, -14, -6, -1, 0, 6, 12)
             for kind in ("grip", "glide", None)}
    heights = {key: rows[-1] - rows[0] for key, rows in sizes.items()}
    assert max(heights.values()) - min(heights.values()) <= 2, heights
    top = display_render.FOOTER_TOP + 18
    assert all(rows[0] >= top - 1 and rows[-1] <= display_render.HEIGHT - 9
               for rows in sizes.values())


@pytest.mark.parametrize("temperature", [-28, -14, -1, 6, 12])
def test_the_reading_and_its_time_fit_beside_it_left_of_the_wax_box(temperature):
    image = display_render.draw_frame(
        display_render.build_frame(payload(outdoor={"temperature": temperature}), now=NOW),
    )
    assert not any(image.getpixel((x, y)) == display_render.BLACK
                   for x in range(display_render.WAX_LEFT - 9, display_render.WAX_LEFT)
                   for y in range(display_render.FOOTER_TOP + 12, display_render.HEIGHT))


def test_the_outlook_is_not_on_the_wax_footer():
    def picture(outlook):
        reading = payload(outdoor={"temperature": -6}, weather_outlook={"tendency": outlook})
        frame = display_render.build_frame(reading, now=NOW)
        return frame, display_render.draw_frame(frame).tobytes()

    falling, drawn = picture("falling")
    assert falling.outlook  # still known, just not drawn here
    assert picture("rising")[1] == drawn
