"""Choosing the order the groups are shown in.

Groups used to be ordered by whichever of their rooms the hub happened to list
first, which on a real house reads as random. The order can now be chosen
under Settings, and these pin the parts that are easy to get subtly wrong:
that the order reaches every browser with the zones, that a room moving
between groups does not scramble it, that a renamed group keeps its place, and
that an installation which never reorders anything looks exactly as before.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

import config_persistence
import server

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (ROOT / "app" / "static" / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
SESSION = "pytest-fixed-session-id"


@pytest.fixture(autouse=True)
def fresh_groups(monkeypatch):
    monkeypatch.setattr(server, "zone_categories", {})
    monkeypatch.setattr(server, "zone_group_order", [])


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.cookies.set("session_id", SESSION)
    return c


def _zone_ids(client):
    return [str(z["zone_id"]) for z in client.get("/api/zones").json()["zones"]]


def _ranks(client):
    return {z["category"]: z["category_rank"] for z in client.get("/api/zones").json()["zones"]}


# -- the server ---------------------------------------------------------------


def test_without_a_chosen_order_every_group_is_unplaced(client):
    ids = _zone_ids(client)
    client.put("/api/zone-categories", json={"categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms"}})
    ranks = _ranks(client)
    assert ranks["Upstairs"] is None and ranks["Bathrooms"] is None
    assert ranks[""] is None
    assert not config_persistence.ZONE_GROUP_ORDER_FILE.exists()


def test_the_chosen_order_reaches_every_zone_and_is_saved(client):
    ids = _zone_ids(client)
    r = client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms", ids[2]: "Bathrooms"},
        "order": ["Bathrooms", "Upstairs"],
    })
    assert r.status_code == 200
    assert r.json()["order"] == ["Bathrooms", "Upstairs"]
    assert _ranks(client) == {"Bathrooms": 0, "Upstairs": 1, "": None}
    assert config_persistence.load_zone_group_order() == ["Bathrooms", "Upstairs"]


def test_moving_a_room_without_restating_the_order_keeps_it(client):
    ids = _zone_ids(client)
    client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms"},
        "order": ["Bathrooms", "Upstairs"],
    })
    client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms", ids[2]: "Upstairs"},
    })
    assert server.zone_group_order == ["Bathrooms", "Upstairs"]


def test_a_group_nobody_is_in_any_more_leaves_the_order(client):
    """Otherwise a new group of the same name inherits a place nobody chose."""
    ids = _zone_ids(client)
    client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms"},
        "order": ["Bathrooms", "Upstairs"],
    })
    client.put("/api/zone-categories", json={"categories": {ids[0]: "Upstairs"}})
    assert server.zone_group_order == ["Upstairs"]
    assert config_persistence.load_zone_group_order() == ["Upstairs"]


def test_the_one_room_editor_prunes_it_too(client):
    ids = _zone_ids(client)
    client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms"},
        "order": ["Bathrooms", "Upstairs"],
    })
    r = client.put(f"/api/zones/{ids[1]}", json={"category": ""})
    assert r.status_code == 200
    assert server.zone_group_order == ["Upstairs"]


def test_names_are_cleaned_and_duplicates_dropped(client):
    ids = _zone_ids(client)
    client.put("/api/zone-categories", json={
        "categories": {ids[0]: "Upstairs", ids[1]: "Bathrooms"},
        "order": [" Upstairs ", "", "Upstairs", "Bathrooms", "Nowhere"],
    })
    assert server.zone_group_order == ["Upstairs", "Bathrooms"]


def test_a_corrupt_order_file_is_set_aside_not_fatal():
    config_persistence.ZONE_GROUP_ORDER_FILE.write_text("{not json", encoding="utf-8")
    assert config_persistence.load_zone_group_order() == []
    config_persistence.ZONE_GROUP_ORDER_FILE.write_text('{"a": 1}', encoding="utf-8")
    assert config_persistence.load_zone_group_order() == []


def test_the_client_sends_the_order_only_when_it_has_one():
    start = CORE.index("setZoneCategories:")
    body = CORE[start:CORE.index("}),", start)]
    assert "order ? { categories, order } : { categories }" in body


# -- the interface, run for real -------------------------------------------


def _lift(*markers):
    out = []
    for marker in markers:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        out.append(CABIN[start:end])
    return "\n".join(out)


def _run(script):
    body = _lift("function zoneGroups", "function movedGroupOrder")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(f"{body}\n{script}")
        path = fh.name
    try:
        result = subprocess.run(["node", path], capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip())
    finally:
        Path(path).unlink(missing_ok=True)


def _groups(zones):
    return _run(
        f"const z = {json.dumps(zones)};"
        "console.log(JSON.stringify(zoneGroups(z).map(([n]) => n)));"
    )


def _z(category, rank=None):
    return {"category": category, "category_rank": rank}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


@needs_node
def test_the_chosen_order_beats_the_order_the_hub_lists_rooms_in():
    zones = [_z("Upstairs", 1), _z("Bathrooms", 0), _z("Upstairs", 1)]
    assert _groups(zones) == ["Bathrooms", "Upstairs"]


@needs_node
def test_a_new_group_follows_the_placed_ones_and_other_is_still_last():
    zones = [_z(""), _z("New"), _z("Upstairs", 1), _z("Kitchen"), _z("Bathrooms", 0)]
    assert _groups(zones) == ["Bathrooms", "Upstairs", "New", "Kitchen", ""]


@needs_node
def test_no_order_at_all_is_the_old_first_seen_order():
    zones = [_z("Utility"), _z("Bathrooms"), _z("Utility")]
    assert _groups(zones) == ["Utility", "Bathrooms"]


@needs_node
def test_moving_swaps_with_the_neighbour_and_stops_at_the_ends():
    out = _run("""
      const o = ['A', 'B', 'C'];
      console.log(JSON.stringify([
        movedGroupOrder(o, 'B', 'up'), movedGroupOrder(o, 'B', 'down'),
        movedGroupOrder(o, 'A', 'up'), movedGroupOrder(o, 'C', 'down'),
        movedGroupOrder(o, 'Z', 'up'), o,
      ]));
    """)
    assert out == [["B", "A", "C"], ["A", "C", "B"], None, None, None, ["A", "B", "C"]]


# -- the Settings controls --------------------------------------------------


def _groups_card():
    start = CABIN.index("function renderZoneGroupsCard")
    return CABIN[start:CABIN.index("\n  }\n", start)]


def test_each_group_has_move_up_and_down_buttons():
    card = _groups_card()
    assert 'data-dir="up"' in card and 'data-dir="down"' in card
    assert 'aria-label="Move ${esc(name)} up"' in card
    assert "Nobo.icon('up')" in card and "Nobo.icon('down')" in card
    assert "up:" in CORE and "down:" in CORE


def test_the_ends_and_unsaved_groups_cannot_move():
    card = _groups_card()
    assert "const canUp = isAdmin && at > 0;" in card
    assert "const canDown = isAdmin && at >= 0 && at < saved.length - 1;" in card


def test_settings_lists_groups_in_the_order_the_front_page_shows():
    start = CABIN.index("function knownGroupNames")
    body = CABIN[start:CABIN.index("\n  }\n", start)]
    assert "savedGroupOrder()" in body
    assert "localeCompare" not in body


def test_a_rename_keeps_the_group_in_its_place():
    start = CABIN.index("card.querySelectorAll('[data-rename-group]')")
    body = CABIN[start:CABIN.index("card.querySelectorAll('[data-delete-group]')", start)]
    assert "savedGroupOrder().map(name => name === was ? now : name)" in body
    assert "saveZoneCategories(map, order)" in body


def test_focus_follows_the_group_that_moved():
    start = CABIN.index("card.querySelectorAll('[data-move-group]')")
    body = CABIN[start:CABIN.index("const addBtn", start)]
    assert "target.focus()" in body
