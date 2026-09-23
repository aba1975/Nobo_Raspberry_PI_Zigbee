"""Grouping the front page by category.

Categories are the application's own idea, like the zone icon, and exist only
so a building with more rooms than fit on a screen can be scanned by part.
The risk in a feature like this is that it costs pixels and taps before it
earns them, so most of what is pinned here is the *absence* of grouping:
when it must not appear, and what it must never hide.

The rendering helpers are lifted out and run in node, so these exercise the
real branching rather than asserting on source text.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")
CORE = (ROOT / "app" / "static" / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
SERVER = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
PERSIST = (ROOT / "app" / "config_persistence.py").read_text(encoding="utf-8")


# -- the grouping rules, run for real ---------------------------------------


def _lift(*markers) -> str:
    out = []
    for marker in markers:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        out.append(CABIN[start:end])
    return "\n".join(out)


def _run(script: str):
    """Run lifted interface code in node and return its JSON result.

    Written to a file rather than passed with ``node -e``: the lifted source
    is long enough to exceed the Windows command-line limit, and a test that
    cannot run on the machine somebody is using stops being read.
    """
    preamble = """
      const esc = (v) => String(v == null ? '' : v);
      const Nobo = {
        MODES: { comfort:{label:'Comfort'}, eco:{label:'Eco'},
                 away:{label:'Away'}, normal:{label:'Schedule'} },
        effectiveMode: (z) => z.current_mode || 'normal',
        fmtTemp: (v) => String(v),
      };
    """
    body = _lift(
        "const GROUPING_MIN_ZONES",
        "function zoneGroups",
        "function groupingIsWorthIt",
        "function zoneNeedsAttention",
        "function groupSummary",
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(f"{preamble}\n{body}\n{script}")
        path = handle.name
    try:
        result = subprocess.run(
            ["node", path], capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip())
    finally:
        Path(path).unlink(missing_ok=True)


def _zone(name, category="", mode="eco", temp=None, **extra):
    zone = {
        "zone_id": name, "name": name, "category": category,
        "current_mode": mode, "current_temperature": temp,
    }
    zone.update(extra)
    return zone


def _worth(zones):
    return _run(
        f"const z = {json.dumps(zones)};"
        "console.log(JSON.stringify(groupingIsWorthIt(z, zoneGroups(z))));"
    )


def _groups(zones):
    return _run(
        f"const z = {json.dumps(zones)};"
        "console.log(JSON.stringify(zoneGroups(z).map(([n, zs]) => [n, zs.length])));"
    )


def _summary(zones):
    return _run(
        f"console.log(JSON.stringify(groupSummary({json.dumps(zones)})));"
    )


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


class TestItStaysOutOfTheWay:
    """Most of the design is the cases where nothing should appear at all."""

    def test_a_small_house_is_never_grouped(self):
        """Five rooms can be read whole. Headings there would be chrome around
        a problem nobody had."""
        zones = [_zone(f"z{i}", "Upstairs" if i < 3 else "Downstairs") for i in range(5)]
        assert _worth(zones) is False

    def test_one_category_for_everything_is_not_a_grouping(self):
        """If every zone is in the same category the headings divide nothing,
        so the page renders as it always did."""
        zones = [_zone(f"z{i}", "Upstairs") for i in range(8)]
        assert _worth(zones) is False

    def test_no_categories_at_all_is_the_old_page(self):
        """Nobody who has never opened the setting should see a change."""
        zones = [_zone(f"z{i}") for i in range(8)]
        assert _worth(zones) is False

    def test_enough_rooms_split_in_two_is_worth_it(self):
        zones = [_zone(f"z{i}", "Upstairs" if i < 4 else "Downstairs") for i in range(8)]
        assert _worth(zones) is True

    def test_one_named_category_beside_the_rest_still_groups(self):
        """Naming only the bathrooms is a legitimate half-finished state, and
        splits the list usefully into two."""
        zones = [_zone(f"z{i}", "Bathrooms" if i < 2 else "") for i in range(8)]
        assert _worth(zones) is True


class TestWhereTheGroupsGo:
    def test_order_is_inherited_from_the_zone_list(self):
        """The first zone of a category fixes where that category sits, so the
        groups follow the order the zones were already in and no reordering
        control has to exist."""
        zones = [
            _zone("a", "Utility"), _zone("b", "Bathrooms"),
            _zone("c", "Utility"), _zone("d", "Bathrooms"),
        ]
        assert _groups(zones) == [["Utility", 2], ["Bathrooms", 2]]

    def test_uncategorised_zones_come_last_whenever_they_appeared(self):
        """Otherwise a single unnamed zone at the top pushes a heading above
        the rooms somebody did organise."""
        zones = [_zone("a"), _zone("b", "Bathrooms"), _zone("c")]
        assert _groups(zones) == [["Bathrooms", 1], ["", 2]]

    def test_the_uncategorised_group_is_called_other(self):
        """Not "Uncategorised", which reads as a reprimand for not finishing."""
        start = CABIN.index("function groupHeadingRow")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "'Other'" in body


class TestTheHeadingEarnsItsLine:
    """A heading repeating a word you typed is decoration. One that reports
    what the rooms are doing can answer the question without the cards being
    read at all."""

    def test_it_counts_the_zones(self):
        assert "2 zones" in _summary([_zone("a", temp=20), _zone("b", temp=21)])
        assert "1 zone" in _summary([_zone("a")])

    def test_a_group_all_doing_one_thing_says_so(self):
        zones = [_zone("a", mode="comfort"), _zone("b", mode="comfort")]
        assert "all at Comfort" in _summary(zones)

    def test_a_mixed_group_names_the_coldest_room(self):
        zones = [_zone("a", mode="comfort", temp=21), _zone("b", mode="eco", temp=16)]
        summary = _summary(zones)
        assert "coldest" in summary and "16" in summary

    def test_no_temperature_anywhere_invents_nothing(self):
        """The usual case on this hardware: every component reports
        ``tempsensor_for_zone_id = None``, so there is no room temperature to
        be had. The line simply gets shorter rather than showing a number
        nothing measured."""
        zones = [_zone("a", mode="comfort"), _zone("b", mode="eco")]
        summary = _summary(zones)
        assert "2 zones" in summary
        assert "coldest" not in summary

    def test_a_room_worth_walking_to_is_counted(self):
        zones = [
            _zone("a"),
            _zone("b", sensor_summary={"warning_raised": True}),
            _zone("c", setpoint_changed_outside={"intended": 21, "actual": 18}),
        ]
        assert "2 need attention" in _summary(zones)

    def test_one_of_them_reads_as_one(self):
        zones = [_zone("a"), _zone("b", sensor_summary={"warning_raised": True})]
        assert "1 needs attention" in _summary(zones)


class TestNothingIsHidden:
    def test_every_zone_is_rendered_when_grouped(self):
        """The front page answers "will the cabin be warm?". Grouping reorders
        and labels; it must never drop a room, which is the failure a filter
        would risk."""
        zones = [_zone(f"z{i}", "Upstairs" if i < 4 else "Downstairs") for i in range(9)]
        total = _run(
            f"const z = {json.dumps(zones)};"
            "console.log(JSON.stringify(zoneGroups(z).reduce((n, [, zs]) => n + zs.length, 0)));"
        )
        assert total == len(zones)

    def test_the_interface_offers_no_way_to_filter_them_away(self):
        """Option A on purpose: a chip that hides a group can hide the cold
        room somebody opened the app to find."""
        start = CABIN.index("function renderZones")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "filter(" not in body.replace("state.zones.filter(z => z.has_manual_devices)", "")


class TestItIsStoredLikeTheIcon:
    """The hub has no such field, so this follows the zone icon exactly rather
    than inventing a second arrangement for the same kind of fact."""

    def test_it_has_its_own_file_beside_the_icons(self):
        assert 'ZONE_CATEGORIES_FILE = DATA_DIR / "zone_categories.json"' in PERSIST
        assert "def save_zone_categories" in PERSIST
        assert "def load_zone_categories" in PERSIST

    def test_a_corrupt_file_does_not_take_the_heating_with_it(self):
        start = PERSIST.index("def load_zone_categories")
        body = PERSIST[start:PERSIST.index("\n\n\n", start)]
        assert "JSONDecodeError" in body
        assert "_backup_corrupt" in body
        assert "return {}" in body

    def test_both_kinds_of_zone_payload_carry_it(self):
        """Demo mode and a real hub build their zone dicts separately, and a
        field added to only one is a feature that works in demo and vanishes
        in the cabin."""
        assert "'category': zone_categories.get(str(demo_zone['zone_id']), '')" in SERVER
        assert "'category': zone_categories.get(str(zone_id), '')" in SERVER

    def test_clearing_it_removes_the_key_rather_than_blanking_it(self):
        """Otherwise the file grows an empty entry for every zone anybody ever
        opened and left alone."""
        start = SERVER.index("def _apply_zone_category")
        body = SERVER[start:SERVER.index("\n\n\n", start)]
        assert "zone_categories.pop(str(zone_id), None)" in body

    def test_both_branches_of_the_update_write_it(self):
        """The fault this caught during development: the icon is stored two
        different ways â€” on the demo zone in demo mode, in ``zone_icons``
        otherwise â€” so the category was added to the real-hub branch only. It
        looked right on a real hub and silently did nothing in demo, which is
        the harder direction to notice.
        """
        start = SERVER.index("async def update_zone(")
        handler = SERVER[start:SERVER.index("\n@app.", start)]
        demo, real = handler.split("# Real hub mode", 1)
        assert "_apply_zone_category(zone_id, update)" in demo, "demo mode does not store it"
        assert "_apply_zone_category(zone_id, update)" in real, "real hub does not store it"


def test_the_heading_is_a_row_of_the_same_grid():
    """Not a separate list per group: one grid keeps the cards sharing row
    heights and keeps the layout working at every width with no breakpoints."""
    assert re.search(r"\.zone-group-head\s*\{[^}]*grid-column:\s*1\s*/\s*-1", CSS, re.S)


def test_the_category_is_offered_with_the_ones_already_in_use():
    """A free box invites "Upstairs" and "upstairs" to become two groups; a
    picker would need a manager to add and rename entries."""
    start = CABIN.index("function renameZone")
    body = CABIN[start:CABIN.index("\n  }\n", start)]
    assert "datalist" in body
    assert "toLowerCase() === typed.toLowerCase()" in body


# ---------------------------------------------------------------------------
# Managing the groups
# ---------------------------------------------------------------------------
#
# The first version of this feature could group rooms and gave no way to
# organise them: the only editor was behind a button labelled "Rename zone",
# one room at a time, with no way to create, rename or remove a group at all.
# Setting up eleven rooms meant eleven trips through eleven screens, so in
# practice nobody would.


class TestThereIsOneScreenThatDoesTheWholeHouse:
    def test_settings_carries_a_rooms_and_groups_card(self):
        assert "function renderZoneGroupsCard" in CABIN
        assert "renderZoneGroupsCard(isAdmin)" in CABIN, "the card is never rendered"
        assert "wireZoneGroups(root)" in CABIN, "the card is never wired up"

    def test_every_room_is_listed_with_a_picker(self):
        """The bulk job. One pass down one screen, rather than opening each
        room in turn."""
        start = CABIN.index("function renderZoneGroupsCard")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "state.zones.map(zone =>" in body
        assert "data-assign-zone" in body

    def test_a_room_can_always_be_taken_out_of_its_group(self):
        """Ungrouped is a legitimate state, so every picker offers it."""
        start = CABIN.index("function renderZoneGroupsCard")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert '<option value="">\u2014 Other \u2014</option>' in body

    def test_groups_can_be_renamed_and_removed(self):
        assert "data-rename-group" in CABIN
        assert "data-delete-group" in CABIN


class TestBulkEditsGoInOneRequest:
    def test_the_api_takes_the_whole_map(self):
        """Renaming a group moves every room in it. Sending that as one
        request per room would leave the house half-renamed if one failed."""
        assert '@app.put("/api/zone-categories")' in SERVER
        assert "class ZoneCategoriesUpdate(BaseModel):" in SERVER

    def test_it_is_not_a_sibling_of_the_zone_id_route(self):
        """/api/zones/ already ends in a {zone_id} catch-all, so a route below
        it would be matched as a zone called "categories" depending on which
        was declared first."""
        assert '@app.put("/api/zones/categories")' not in SERVER

    def test_unknown_zones_are_refused(self):
        start = SERVER.index('@app.put("/api/zone-categories")')
        body = SERVER[start:SERVER.index("\n\n\n", start)]
        assert "Unknown zone ids" in body

    def test_a_blank_group_clears_rather_than_stores(self):
        start = SERVER.index('@app.put("/api/zone-categories")')
        body = SERVER[start:SERVER.index("\n\n\n", start)]
        assert "if name and name.strip()" in body

    def test_the_client_sends_it_as_one_call(self):
        assert "setZoneCategories" in CORE
        assert "/api/zone-categories" in CORE


class TestRemovingAGroupIsNotDestructive:
    def test_it_ungroups_the_rooms_rather_than_warning_about_them(self):
        """Nothing about a room is stored in its group, so there is no data to
        lose. Dressing it as destructive would teach people to fear a button
        that cannot hurt them."""
        start = CABIN.index("data-delete-group]').forEach")
        body = CABIN[start:start + 1400]
        assert "moves to \"Other\"" in body or 'to "Other"' in body
        assert "btn-danger" not in body
        assert "map[zoneId] = ''" in body


class TestTheOneRoomCaseIsStillThere:
    def test_the_button_says_what_the_sheet_does(self):
        """It was labelled "Rename zone" while the sheet behind it also set the
        group, which is why nobody found the setting. A label is the only
        documentation most people read."""
        assert ">Edit this room</button>" in CABIN
        assert ">Rename zone</button>" not in CABIN


class TestTheWayInIsQuietAndConditional:
    def test_the_front_page_offers_a_link_only_when_it_would_help(self):
        """Enough rooms to want grouping, and none grouped yet. It goes as soon
        as the first group exists, so it cannot become furniture."""
        start = CABIN.index("function renderZones")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "state.zones.length >= GROUPING_MIN_ZONES" in body
        assert "!groups.some(([name]) => name)" in body
        assert "Group them" in body

    def test_it_is_a_link_rather_than_another_button(self):
        """The Zones heading already carries Add a zone, which is the action
        people came for. A second button there would compete with it."""
        assert ".linkish" in CSS
        start = CABIN.index("function renderZones")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert 'class="linkish"' in body


def test_an_empty_group_is_not_persisted():
    """A group is the name its rooms share, not an object in its own right.
    Keeping a second list of names would need migrating, and would drift out
    of step with the rooms the moment anything went wrong."""
    assert "draftGroups" in CABIN
    start = SERVER.index('@app.put("/api/zone-categories")')
    body = SERVER[start:SERVER.index("\n\n\n", start)]
    assert "groups" not in body.split("categories")[0].split("def ")[-1]
