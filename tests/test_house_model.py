"""The floor plan behind the house view: what it accepts, what it keeps, and where.

``house_model`` is the only thing between an uploaded file and what every
signed-in phone draws, so each rejection is pinned here, as is the promise
that a placement never outlives what it points at without saying so.
"""

import copy
import json
import math

import pytest

import house_model
import user_prefs
from house_model import HouseModelError


def plan(**overrides):
    """A small valid two-room, two-level plan to vary one thing at a time."""
    model = {
        "schema": 1,
        "levels": [{"id": "ground", "elevation": 0, "height": 2.5},
                   {"id": "loft", "elevation": 2.7, "height": 2.4}],
        "walls": [
            {"id": "s", "level": "ground", "a": [0, 0], "b": [6, 0], "exterior": True,
             "openings": [{"code": "D1", "kind": "door", "at": 2, "width": 0.9, "swing": "out"},
                          {"code": "W1", "kind": "window", "at": 4, "width": 1.2, "sill": 0.9, "height": 1.2}]},
            {"id": "n", "level": "ground", "a": [6, 4], "b": [0, 4], "exterior": True},
            {"id": "i", "level": "ground", "a": [3, 0], "b": [3, 4],
             "openings": [{"kind": "gap", "at": 1, "width": 1}]},
        ],
        "rooms": [
            {"id": "hall", "level": "ground", "zone_id": "3", "polygon": [[0, 0], [3, 0], [3, 4], [0, 4]]},
            {"id": "sauna", "level": "ground", "name": "Sauna", "polygon": [[3, 0], [6, 0], [6, 4], [3, 4]]},
        ],
        "fixtures": [{"level": "ground", "box": [0.2, 0.2, 1, 1], "height": 0.9, "label": "Bench"}],
        "placements": {
            "heaters": {"160004028113": {"wall": "n", "at": 1, "length": 1},
                        "210000016247": {"floor": "hall"}},
            "sensors": {"0x00158d008c8bc44e": {"opening": "D1"},
                        "0x00158d008c8bc54f": {"opening": "W1", "sash": "left"},
                        "0x00158d000223b97c": {"point": [1, 1, 1.5], "level": "ground"},
                        "verisure-contact-3spc-jgfp": {"outside": True}},
        },
    }
    model.update(overrides)
    return model


def rejects(model, fragment):
    with pytest.raises(HouseModelError) as caught:
        house_model.validate(model)
    assert fragment in str(caught.value), str(caught.value)


class TestAccepts:
    def test_a_valid_plan_round_trips(self):
        clean = house_model.validate(plan())
        assert clean["placements"]["sensors"]["0x00158d008c8bc54f"] == {"opening": "W1", "sash": "left"}
        # Defaults are written out, so what is saved is what was checked.
        assert clean["placements"]["sensors"]["0x00158d008c8bc44e"]["sash"] == "whole"
        assert clean["walls"][1]["openings"] == []
        assert house_model.validate(clean) == clean

    def test_the_committed_demo_building_is_valid(self):
        with house_model.DEMO_TEMPLATE_FILE.open(encoding="utf-8") as fh:
            house_model.validate(json.load(fh))

    def test_a_numeric_zone_id_is_carried_as_text(self):
        model = plan()
        model["rooms"][0]["zone_id"] = 3
        assert house_model.validate(model)["rooms"][0]["zone_id"] == "3"

    def test_text_is_collapsed(self):
        model = plan()
        model["rooms"][1]["name"] = "  Sauna \n  room "
        assert house_model.validate(model)["rooms"][1]["name"] == "Sauna room"

    def test_a_clockwise_room_is_as_good_as_an_anticlockwise_one(self):
        model = plan()
        model["rooms"][0]["polygon"].reverse()
        house_model.validate(model)


class TestRejects:
    def test_unknown_top_level_field(self):
        rejects(plan(roof={}), "unknown field roof")

    def test_wrong_schema(self):
        rejects(plan(schema=2), "schema")

    def test_no_levels(self):
        rejects(plan(levels=[]), "at least one level")

    def test_duplicate_level(self):
        rejects(plan(levels=[{"id": "ground", "elevation": 0, "height": 2.5}] * 2), "used twice")

    def test_wall_on_an_unknown_level(self):
        model = plan()
        model["walls"][0]["level"] = "cellar"
        rejects(model, "no level called 'cellar'")

    def test_duplicate_wall(self):
        model = plan()
        model["walls"][1]["id"] = "s"
        rejects(model, "'s' is used twice")

    def test_a_wall_with_no_length(self):
        model = plan()
        model["walls"][1]["b"] = [6, 4]
        rejects(model, "at least 5 cm")

    def test_opening_past_the_end_of_its_wall(self):
        model = plan()
        model["walls"][0]["openings"][1]["at"] = 5.5
        rejects(model, "runs past the end of its wall")

    def test_a_window_needs_a_code(self):
        model = plan()
        del model["walls"][0]["openings"][1]["code"]
        rejects(model, "needs a code")

    def test_duplicate_opening_code(self):
        model = plan()
        model["walls"][0]["openings"][1]["code"] = "D1"
        rejects(model, "'D1' is used twice")

    def test_a_window_does_not_swing(self):
        model = plan()
        model["walls"][0]["openings"][1]["swing"] = "in"
        rejects(model, "only a door swings")

    def test_room_that_crosses_itself(self):
        model = plan()
        model["rooms"][0]["polygon"] = [[0, 0], [4, 0], [4, 4], [2, -1], [0, 4]]
        rejects(model, "crosses itself")

    def test_room_with_no_area(self):
        model = plan()
        model["rooms"][0]["polygon"] = [[0, 0], [1, 0], [2, 0]]
        rejects(model, "has no area")

    def test_room_with_too_many_corners(self):
        model = plan()
        corners = house_model.MAX_POLYGON_POINTS + 1
        model["rooms"][0]["polygon"] = [
            [math.cos(2 * math.pi * k / corners), math.sin(2 * math.pi * k / corners)] for k in range(corners)
        ]
        rejects(model, "at most")

    def test_inverted_fixture(self):
        model = plan()
        model["fixtures"][0]["box"] = [1, 1, 0.2, 0.2]
        rejects(model, "lower to the higher corner")

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_non_finite_number(self, value):
        model = plan()
        model["walls"][0]["a"] = [value, 0]
        rejects(model, "finite")

    def test_coordinate_out_of_range(self):
        model = plan()
        model["walls"][0]["b"] = [1000, 0]
        rejects(model, "between")

    def test_true_is_not_a_number(self):
        model = plan()
        model["walls"][0]["a"] = [True, 0]
        rejects(model, "must be a number")

    def test_control_characters_in_a_name(self):
        model = plan()
        model["rooms"][1]["name"] = "Sau\x00na"
        rejects(model, "control characters")

    def test_placement_on_an_unknown_opening(self):
        model = plan()
        model["placements"]["sensors"]["0x00158d008c8bc44e"] = {"opening": "W99"}
        rejects(model, "no door or window called 'W99'")

    def test_a_door_has_no_sashes(self):
        model = plan()
        model["placements"]["sensors"]["0x00158d008c8bc44e"] = {"opening": "D1", "sash": "left"}
        rejects(model, "a door has no sashes")

    def test_heater_on_an_unknown_wall(self):
        model = plan()
        model["placements"]["heaters"]["160004028113"]["wall"] = "x"
        rejects(model, "no wall called 'x'")

    def test_heater_past_the_end_of_its_wall(self):
        model = plan()
        model["placements"]["heaters"]["160004028113"]["at"] = 5.5
        rejects(model, "runs past the end of wall 'n'")

    def test_floor_heating_in_an_unknown_room(self):
        model = plan()
        model["placements"]["heaters"]["210000016247"] = {"floor": "attic"}
        rejects(model, "no room called 'attic'")

    def test_heater_key_must_be_a_serial(self):
        model = plan()
        model["placements"]["heaters"]["Living Room"] = {"floor": "hall"}
        rejects(model, "twelve-digit heater serial")

    def test_point_above_its_level(self):
        model = plan()
        model["placements"]["sensors"]["0x00158d000223b97c"]["point"] = [1, 1, 9]
        rejects(model, "outside the level")

    def test_outside_must_be_true(self):
        model = plan()
        model["placements"]["sensors"]["verisure-contact-3spc-jgfp"] = {"outside": False}
        rejects(model, "must be true")

    def test_parse_refuses_an_oversized_upload_before_reading_it(self):
        body = b" " * (house_model.MAX_BYTES + 1)
        with pytest.raises(HouseModelError, match="larger than"):
            house_model.parse(body)

    def test_parse_refuses_invalid_json(self):
        with pytest.raises(HouseModelError, match="not valid JSON"):
            house_model.parse(b"{not json")


class TestChangingAPlan:
    def test_new_geometry_keeps_placements_that_still_fit(self):
        old = house_model.validate(plan())
        new = plan()
        new["placements"] = {"heaters": {}, "sensors": {}}
        # W1 is gone from the new plan; D1 and the walls are still there.
        new["walls"][0]["openings"] = new["walls"][0]["openings"][:1]
        merged, dropped = house_model.replace_geometry(house_model.validate(new), old)
        assert merged["placements"]["heaters"] == old["placements"]["heaters"]
        assert "0x00158d008c8bc44e" in merged["placements"]["sensors"]
        assert dropped == {"heaters": [], "sensors": ["0x00158d008c8bc54f"]}

    def test_uploaded_placements_win_over_earlier_ones(self):
        old = house_model.validate(plan())
        new = plan()
        new["placements"]["heaters"]["160004028113"] = {"wall": "s", "at": 0, "length": 1}
        merged, _ = house_model.replace_geometry(house_model.validate(new), old)
        assert merged["placements"]["heaters"]["160004028113"]["wall"] == "s"

    def test_set_and_remove_return_copies(self):
        model = house_model.validate(plan())
        before = copy.deepcopy(model)
        placed = house_model.set_placement(model, "sensors", "new-sensor", {"opening": "W1", "sash": "right"})
        assert model == before
        assert placed["placements"]["sensors"]["new-sensor"] == {"opening": "W1", "sash": "right"}
        removed, existed = house_model.remove_placement(placed, "sensors", "new-sensor")
        assert existed and "new-sensor" not in removed["placements"]["sensors"]
        _, existed = house_model.remove_placement(model, "sensors", "never-placed")
        assert not existed

    def test_status_lists_unplaced_and_stale_devices(self):
        model = house_model.validate(plan())
        status = house_model.placement_status(
            model, ["160004028113", "160004028999"], ["0x00158d008c8bc44e", "new"])
        assert status["unplaced"] == {"heaters": ["160004028999"], "sensors": ["new"]}
        assert status["stale"]["heaters"] == ["210000016247"]
        assert "verisure-contact-3spc-jgfp" in status["stale"]["sensors"]

    def test_an_unknown_kind_has_no_lists_rather_than_empty_ones(self):
        model = house_model.validate(plan())
        status = house_model.placement_status(model, None, ["0x00158d008c8bc44e"])
        assert status["unplaced"]["heaters"] is None and status["stale"]["heaters"] is None
        assert status["unplaced"]["sensors"] == []

    def test_a_standby_sensor_is_neither_stale_nor_asked_for(self):
        model = house_model.validate(plan())
        status = house_model.placement_status(
            model, [], ["0x00158d008c8bc44e"], standby_sensor_ids=["verisure-contact-3spc-jgfp"])
        assert "verisure-contact-3spc-jgfp" not in status["stale"]["sensors"]
        assert "verisure-contact-3spc-jgfp" not in status["unplaced"]["sensors"]

    def test_carrying_placements_over_stops_at_the_limit(self):
        def many(prefix, count):
            model = plan()
            model["placements"] = {"heaters": {}, "sensors": {
                f"{prefix}{k}": {"opening": "W1"} for k in range(count)}}
            return house_model.validate(model)

        merged, dropped = house_model.replace_geometry(many("new-", 300), many("old-", 300))
        assert len(merged["placements"]["sensors"]) == house_model.MAX_PLACEMENTS
        assert len(dropped["sensors"]) == 600 - house_model.MAX_PLACEMENTS
        house_model.validate(merged)

    def test_a_placement_cannot_push_the_plan_over_the_limit(self):
        model = plan()
        model["placements"] = {"heaters": {}, "sensors": {
            f"s{k}": {"opening": "W1"} for k in range(house_model.MAX_PLACEMENTS)}}
        model = house_model.validate(model)
        with pytest.raises(HouseModelError, match="at most"):
            house_model.set_placement(model, "sensors", "one-more", {"opening": "W1"})


class TestStorage:
    def test_save_then_load(self):
        model = house_model.validate(plan())
        house_model.save(model, demo=False)
        assert house_model.load(demo=False) == (model, "file")
        assert not house_model.house_file(False).with_suffix(".tmp").exists()

    def test_real_mode_with_no_plan_has_none(self):
        assert house_model.load(demo=False) == (None, None)

    def test_demo_mode_falls_back_to_the_demo_building(self):
        model, source = house_model.load(demo=True)
        assert source == "demo" and model["rooms"]

    def test_demo_and_real_plans_are_separate(self):
        house_model.save(house_model.validate(plan()), demo=True)
        assert house_model.load(demo=True)[1] == "file"
        assert house_model.load(demo=False) == (None, None)

    def test_an_invalid_saved_plan_is_left_alone_and_not_used(self, tmp_path):
        path = house_model.house_file(False)
        path.write_text(json.dumps(plan(schema=99)), encoding="utf-8")
        assert house_model.load(demo=False) == (None, None)
        assert path.exists()

    def test_a_broken_demo_file_does_not_fall_back_to_the_template(self):
        # Somebody saved one; showing a different building instead would hide that it broke.
        house_model.house_file(True).write_text("{", encoding="utf-8")
        assert house_model.load(demo=True) == (None, None)

    def test_delete(self):
        house_model.save(house_model.validate(plan()), demo=False)
        assert house_model.delete(demo=False) is True
        assert house_model.delete(demo=False) is False


class TestUserPreferences:
    def test_defaults_are_todays_front_page(self):
        assert user_prefs.get("anyone") == {"rooms_view": "list"}

    def test_a_choice_is_kept_per_user(self):
        assert user_prefs.update("anders", {"rooms_view": "house"}) == {"rooms_view": "house"}
        assert user_prefs.get("anders") == {"rooms_view": "house"}
        assert user_prefs.get("guest") == {"rooms_view": "list"}

    @pytest.mark.parametrize("changes", [{"rooms_view": "3d"}, {"theme": "dark"}, {}, ["house"]])
    def test_a_bad_change_changes_nothing(self, changes):
        user_prefs.update("anders", {"rooms_view": "house"})
        with pytest.raises(user_prefs.PreferenceError):
            user_prefs.update("anders", changes)
        assert user_prefs.get("anders") == {"rooms_view": "house"}

    def test_going_back_to_the_default_forgets_the_user(self, tmp_path):
        user_prefs.update("anders", {"rooms_view": "house"})
        user_prefs.update("anders", {"rooms_view": "list"})
        saved = json.loads((tmp_path / "user_prefs.json").read_text(encoding="utf-8"))
        assert saved == {"schema": 1, "users": {}}

    def test_rename_and_remove(self):
        user_prefs.update("anders", {"rooms_view": "house"})
        user_prefs.rename_user("anders", "anna")
        assert user_prefs.get("anna") == {"rooms_view": "house"}
        assert user_prefs.get("anders") == {"rooms_view": "list"}
        user_prefs.remove_user("anna")
        assert user_prefs.get("anna") == {"rooms_view": "list"}

    def test_a_corrupt_file_means_defaults(self, tmp_path):
        (tmp_path / "user_prefs.json").write_text("{", encoding="utf-8")
        assert user_prefs.get("anders") == {"rooms_view": "list"}
