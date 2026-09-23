"""Settings as collapsible topics, and one vocabulary for the controls.

Ten cards had grown into one scroll. Worse, the same kind of question was
being asked in several different ways: demo mode for heaters was a toggle
reading On/Off, while demo mode for sensors was a row called "Sensor source"
with a Change button that opened a sheet. A settings screen that answers one
question four ways feels assembled rather than designed, and it is the kind
of thing that grows one reasonable decision at a time.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")


def _settings_template() -> str:
    start = CABIN.index("$('#viewSettings').innerHTML = `")
    return CABIN[start:CABIN.index("const root = $('#viewSettings');", start)]


class TestEveryTopicCollapses:
    def test_the_settings_screen_is_built_from_sections(self):
        assert "function settingsSection" in CABIN
        template = _settings_template()
        assert "<section class=\"card\"" not in template, (
            "a card was left behind, so one topic cannot collapse with the rest"
        )

    def test_every_topic_is_named_and_identified(self):
        """The id is what the open/closed memory is keyed on, so a topic
        without one silently forgets."""
        template = _settings_template()
        ids = re.findall(r"settingsSection\('([a-z]+)',\s*'([^']+)'", template)
        assert len(ids) >= 6, ids
        assert len(set(i for i, _ in ids)) == len(ids), "two topics share an id"

    def test_a_details_element_is_used_rather_than_a_hand_rolled_toggle(self):
        """It comes with the keyboard behaviour, the open state and the
        find-in-page support already correct."""
        start = CABIN.index("function settingsSection")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "<details" in body
        assert "<summary>" in body


class TestAClosedTopicStillReports:
    """The only thing that makes collapsing worth doing. A summary repeating
    its own heading would hide the settings and give nothing back."""

    def test_each_section_takes_a_summary(self):
        start = CABIN.index("function settingsSection")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "sec-state" in body

    def test_the_summaries_carry_live_values(self):
        template = _settings_template()
        # The site name, the group count, which source the heaters are on.
        assert "esc(site.name" in template
        assert "hub.demo_mode ? '<b>Demo</b>'" in template

    def test_a_summary_that_arrives_late_is_filled_in_when_it_does(self):
        """Frost exceptions and alert settings are fetched after the page is
        drawn. A summary reading "loading" for ever would be worse than none,
        so those two are written by their own loaders."""
        assert "#excState" in CABIN
        assert "#notifyState" in CABIN


class TestCollapsingCannotHideAFault:
    def test_a_section_with_a_problem_opens_itself(self):
        start = CABIN.index("function settingsSection")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "opts.alert === true ||" in body, (
            "the alert flag must win over whatever was remembered"
        )

    def test_the_default_password_is_such_a_problem(self):
        template = _settings_template()
        assert "alert: !!(me && me.using_default_password)" in template


class TestOpenTopicsAreRememberedPerDevice:
    def test_it_is_local_storage_rather_than_the_hub(self):
        """Which topics somebody leaves open is a habit of the phone in their
        hand, not a fact about the cabin."""
        assert "SETTINGS_OPEN_KEY" in CABIN
        assert "localStorage" in CABIN
        start = CABIN.index("function settingsOpenMap")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "try {" in body, "a browser with storage disabled must not break Settings"

    def test_the_toggle_is_recorded(self):
        start = CABIN.index("function wireSettingsSections")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "rememberSettingsOpen(sec.dataset.section, sec.open)" in body


class TestOneVocabularyForOneKindOfQuestion:
    """The fault that prompted this: demo mode for heaters was a toggle and
    demo mode for sensors was a Change button opening a sheet."""

    def test_both_sources_use_the_same_control(self):
        assert "segControl('hub-source'" in CABIN
        assert "segControl('sensor-source'" in CABIN

    def test_the_old_two_shapes_are_gone(self):
        assert 'data-act="toggle-demo"' not in CABIN, "the heater toggle survived"
        assert 'data-act="change-source"' not in CABIN, "the sensor Change button survived"
        assert "function sensorSourceRow" not in CABIN

    def test_sensors_get_an_honest_third_state(self):
        """Off is not a source, and pretending it was one is what made this
        need two controls in the first place."""
        start = CABIN.index("function renderSensorSettingsCard")
        card = CABIN[start:CABIN.index("\n  async function saveSensorSettings", start)]
        assert "'off', 'Off'" in card

    def test_the_segmented_control_says_which_is_chosen(self):
        start = CABIN.index("function segControl")
        body = CABIN[start:CABIN.index("\n  }\n", start)]
        assert "aria-pressed" in body


class TestItLooksLikeTheRestOfTheApp:
    def test_a_select_anywhere_in_settings_is_styled(self):
        """Only `.field select` was styled, so eleven pickers added later fell
        through to the browser default: square, system font, grey, beside
        rounded paper-coloured ones."""
        assert re.search(r"\.sec select\s*\{[^}]*border-radius:\s*11px", CSS, re.S)
        assert re.search(r"\.card select\s*\{[^}]*border-radius:\s*11px", CSS, re.S)

    def test_the_controls_stay_thumb_sized(self):
        assert re.search(r"\.choice-btn\s*\{[^}]*min-height:\s*38px", CSS, re.S)
        assert re.search(r"\.sec > summary\s*\{[^}]*min-height:\s*44px", CSS, re.S)

    def test_a_three_way_control_wraps_rather_than_shrinking_on_a_phone(self):
        """Squeezing it would make the targets narrower than a thumb."""
        assert re.search(r"@media \(max-width: 30rem\)[^@]*\.opt-row\s*\{[^}]*flex-wrap", CSS, re.S)

    def test_the_new_classes_do_not_take_names_that_were_already_used(self):
        """What the screenshot showed. `.seg-btn` already belonged to the away
        sheet's two-answer control — a grid of full-width boxes, defined later
        in the file — and `.set-label` to the zone card's uppercase "SET TO".
        Reusing both names silently redressed these controls as those ones:
        pale-green pills and labels in small caps.

        Checked by counting definitions rather than by reading the rules,
        because the failure was invisible in either file alone.
        """
        for taken in (".seg-btn", ".set-label", ".set-row"):
            blocks = re.findall(rf"^\{re.escape(taken)}\s*[,{{]", CSS, re.M)
            assert len(blocks) <= 1, f"{taken} is defined {len(blocks)} times"
        # And the settings controls use their own names.
        assert ".choice-btn" in CSS and ".opt-label" in CSS
        assert 'class="choice-btn"' in CABIN
        assert 'class="opt-label"' in CABIN

    def test_the_labels_are_not_shouting(self):
        """`.set-label` is uppercase with letter-spacing, which is right for
        "SET TO" above a big number and wrong for a sentence."""
        block = re.search(r"\.opt-label strong\s*\{([^}]*)\}", CSS, re.S).group(1)
        assert "text-transform: none" in block
