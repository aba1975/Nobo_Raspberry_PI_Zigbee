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
    assert frame.rows[0].colour == "yellow" and frame.rows[0].tag == "09:47"
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


def test_unavailable_sensors_are_blue_and_never_counted_as_closed():
    frame = display_render.build_frame(
        payload(unavailable_sensors=[{"zone": "Loft", "sensor": "Hatch"}]), now=NOW,
    )
    assert frame.tone == "blue" and frame.headline != "All closed"
    assert frame.rows[0].colour == "blue"


def test_the_alarm_left_open_warning_is_red_even_with_nothing_else():
    frame = display_render.build_frame(
        payload(alarm={"left_open": True, "reason": "locked_outside"}), now=NOW,
    )
    assert frame.tone == "red"
    assert frame.rows[0].title == "Locked up"


def test_without_sensors_the_display_says_so_rather_than_all_closed():
    frame = display_render.build_frame(payload(sensors_enabled=False), now=NOW)
    assert frame.tone == "blue" and frame.headline != "All closed"


def test_a_lost_hub_and_a_low_battery_are_noted():
    frame = display_render.build_frame(payload(hub_connected=False), display_battery=10, now=NOW)
    text = " ".join(note for _, note in frame.notes)
    assert "hub" in text and "10%" in text


def test_the_footer_has_the_outdoor_temperature_and_outlook():
    frame = display_render.build_frame(payload(
        outdoor={"temperature": -3.6}, weather_outlook={"tendency": "falling"},
    ), now=NOW)
    assert "-4°" in frame.footer and "Weather turning" in frame.footer


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


def test_classic_has_no_display_surface():
    assert "/api/displays" not in _static("app.js")
