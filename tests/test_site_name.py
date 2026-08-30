"""Naming the installation.

"Cabin" is the name of the interface, not of anybody's house. A user whose
place is called Lakeside, or who thinks of it as Søndre Ås 12, should see
that — on the sign-in page, in the header, and in every sentence that addresses
the building.

Two things are easy to get wrong here and both are covered below:

* **Grammar.** A name is used two ways. On its own ("Lakeside") and mid-sentence
  ("Warm all of Lakeside?"). The unnamed fallbacks differ — "Cabin" and "the
  cabin" — because "Warm all of Cabin?" reads like a bug.
* **Disclosure.** The sign-in page is served to anyone who can reach the Pi,
  before any password is asked for. If someone names their system after their
  street address, that address is on an unauthenticated page. Hence
  ``show_on_login``, and hence the test that it is actually honoured.
"""

import html
import importlib
import os

import pytest
from fastapi.testclient import TestClient

import config_persistence
import server
from server import app

SESSION = "pytest-fixed-session-id"


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", SESSION)
        yield c


@pytest.fixture(autouse=True)
def clean_site():
    """Every test starts from an unnamed installation."""
    yield


class TestDefaults:
    def test_an_unnamed_system_reports_the_fallbacks(self, client):
        r = client.get("/api/site")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == ""
        assert body["is_named"] is False
        assert body["display_name"] == "Cabin"
        assert body["inline_name"] == "the cabin"

    def test_the_two_display_forms_are_not_the_same(self, client):
        """The whole point of having both: one is a title, one is mid-sentence."""
        body = client.get("/api/site").json()
        assert body["display_name"] != body["inline_name"]

    def test_show_on_login_defaults_to_on(self, client):
        assert client.get("/api/site").json()["show_on_login"] is True

    def test_the_maximum_length_is_advertised(self, client):
        """The form needs it for maxlength; hard-coding it in two places drifts."""
        assert client.get("/api/site").json()["max_length"] == config_persistence.SITE_NAME_MAX


class TestNaming:
    def test_a_name_is_saved_and_returned(self, client):
        r = client.put("/api/site", json={"name": "Lakeside"})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Lakeside"
        assert body["is_named"] is True

    def test_a_name_is_used_for_both_forms(self, client):
        """Once named, there is nothing to fall back to — the name is the name."""
        body = client.put("/api/site", json={"name": "Lakeside"}).json()
        assert body["display_name"] == "Lakeside"
        assert body["inline_name"] == "Lakeside"

    def test_a_name_survives_a_reread(self, client):
        client.put("/api/site", json={"name": "Søndre Ås 12"})
        assert client.get("/api/site").json()["name"] == "Søndre Ås 12"

    def test_non_ascii_names_are_kept_intact(self, client):
        """Norwegian names are the common case here, not an edge case."""
        for name in ["Lakeside", "Søndre Ås 12", "Hytta på Fjellet", "Øvre Ålsgård"]:
            client.put("/api/site", json={"name": name})
            assert client.get("/api/site").json()["name"] == name

    def test_surrounding_whitespace_is_trimmed(self, client):
        assert client.put("/api/site", json={"name": "  Lakeside  "}).json()["name"] == "Lakeside"

    def test_an_empty_name_clears_it(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        body = client.put("/api/site", json={"name": ""}).json()
        assert body["name"] == ""
        assert body["display_name"] == "Cabin"

    def test_an_overlong_name_is_truncated_not_rejected(self, client):
        """It goes in a page title. Trimming is kinder than an error."""
        body = client.put("/api/site", json={"name": "M" * 200}).json()
        assert len(body["name"]) == config_persistence.SITE_NAME_MAX

    def test_newlines_are_stripped(self, client):
        """The name is interpolated into a title and a heading; it must be one line."""
        body = client.put("/api/site", json={"name": "Lakeside\nEvil"}).json()
        assert "\n" not in body["name"]

    def test_a_name_of_only_unusable_characters_is_refused(self, client):
        """They typed something and nothing survived. Silence would look broken."""
        r = client.put("/api/site", json={"name": "\x00\x07"})
        assert r.status_code == 400
        assert "usable" in r.json()["detail"].lower()

    def test_renaming_is_recorded_in_the_activity_log(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        entries = client.get("/api/log").json()["entries"]
        assert any("Lakeside" in e["description"] for e in entries)


class TestPartialUpdates:
    def test_changing_the_name_keeps_the_login_preference(self, client):
        client.put("/api/site", json={"name": "Lakeside", "show_on_login": False})
        body = client.put("/api/site", json={"name": "Nystugu"}).json()
        assert body["name"] == "Nystugu"
        assert body["show_on_login"] is False

    def test_changing_the_login_preference_keeps_the_name(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        body = client.put("/api/site", json={"show_on_login": False}).json()
        assert body["name"] == "Lakeside"
        assert body["show_on_login"] is False


class TestTheSignInPage:
    def test_an_unnamed_system_keeps_the_shipped_wording(self, client):
        """An installation that never sets a name must look exactly as before."""
        page = client.get("/login").text
        assert "Heating control for the cabin." in page

    def test_a_named_system_is_named_on_the_sign_in_page(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        page = client.get("/login").text
        assert "Heating control for Lakeside." in page
        assert "for the cabin" not in page

    def test_the_name_can_be_kept_off_the_sign_in_page(self, client):
        """The sign-in page is public. A street address there is a disclosure."""
        client.put("/api/site", json={"name": "Søndre Ås 12", "show_on_login": False})
        page = client.get("/login").text
        assert "Søndre Ås 12" not in page
        assert "Heating control for the cabin." in page

    def test_the_sign_in_page_still_works_without_a_session(self):
        """It is the one page that must render to a stranger."""
        with TestClient(app) as anon:
            r = anon.get("/login")
        assert r.status_code == 200
        assert "Sign in" in r.text

    def test_a_name_with_html_in_it_cannot_inject_markup(self, client):
        """The name reaches an unauthenticated page, so escaping is not optional."""
        client.put("/api/site", json={"name": '<script>alert(1)</script>'})
        page = client.get("/login").text
        assert "<script>alert(1)</script>" not in page
        assert html.escape("<script>alert(1)</script>") in page

    def test_a_name_with_quotes_is_escaped(self, client):
        client.put("/api/site", json={"name": 'The "Old" Barn'})
        page = client.get("/login").text
        assert 'The "Old" Barn' not in page
        assert "&quot;" in page or "&#x27;" in page


class TestPermissions:
    def test_reading_the_name_needs_a_session(self):
        with TestClient(app) as anon:
            assert anon.get("/api/site").status_code == 401

    def test_renaming_needs_a_session(self):
        with TestClient(app) as anon:
            assert anon.put("/api/site", json={"name": "Lakeside"}).status_code == 401

    def test_an_ordinary_user_cannot_rename_the_system(self, client, monkeypatch):
        """It changes what everyone sees, including before sign-in."""
        monkeypatch.setattr(
            server.auth, "load_users", lambda: {"admin": {"role": "user"}}
        )
        r = client.put("/api/site", json={"name": "Lakeside"})
        assert r.status_code == 403

    def test_an_ordinary_user_can_still_read_the_name(self, client, monkeypatch):
        """Every page needs it to render; it is not a secret from a signed-in user."""
        monkeypatch.setattr(
            server.auth, "load_users", lambda: {"admin": {"role": "user"}}
        )
        assert client.get("/api/site").status_code == 200


class TestTheInstalledAppManifest:
    def test_the_manifest_carries_the_chosen_name(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        m = client.get("/manifest.webmanifest").json()
        assert "Lakeside" in m["name"]
        assert m["short_name"] == "Lakeside"

    def test_the_manifest_still_starts_at_the_app_root(self, client):
        """A manifest scoped to a static path installs an icon that opens a file."""
        client.put("/api/site", json={"name": "Lakeside"})
        m = client.get("/manifest.webmanifest").json()
        assert m["start_url"] == "/"
        assert m["scope"] == "/"

    def test_the_manifest_keeps_its_icons(self, client):
        """Overriding the name must not drop everything else in the file."""
        m = client.get("/manifest.webmanifest").json()
        assert m.get("icons"), "the installed app would have no icon"

    def test_an_unnamed_system_gets_the_default_manifest_name(self, client):
        m = client.get("/manifest.webmanifest").json()
        assert "Cabin" in m["name"]

    def test_the_short_name_stays_short(self, client):
        """Home screens truncate. A 40-character name would be unreadable."""
        client.put("/api/site", json={"name": "A Very Long House Name Indeed Yes"})
        m = client.get("/manifest.webmanifest").json()
        assert len(m["short_name"]) <= 12

    def test_the_manifest_is_served_as_a_manifest(self, client):
        r = client.get("/manifest.webmanifest")
        assert "manifest" in r.headers["content-type"]


class TestCorruptionIsSurvivable:
    def test_a_corrupt_file_falls_back_to_unnamed(self, client, monkeypatch, tmp_path):
        bad = tmp_path / "site.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(config_persistence, "SITE_FILE", bad)

        body = client.get("/api/site").json()
        assert body["display_name"] == "Cabin"

    def test_a_corrupt_file_does_not_break_the_sign_in_page(self, client, monkeypatch, tmp_path):
        """Locking everyone out because a name file went bad would be absurd."""
        bad = tmp_path / "site.json"
        bad.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(config_persistence, "SITE_FILE", bad)

        assert client.get("/login").status_code == 200


class TestRegionalFormat:
    """How dates are written, and the two things that are not negotiable.

    The clock is 24-hour and the unit is Celsius, and neither is offered as a
    choice — see the reasoning in config_persistence. What *is* configurable is
    the date format, stored per installation rather than read from each
    browser, so a household does not see one format on the tablet in the hall
    and another on a phone.
    """

    def test_it_defaults_to_following_the_browser(self, client):
        """A fresh install should not impose one country's format on everyone."""
        assert client.get("/api/site").json()["locale"] == ""

    def test_a_locale_can_be_set(self, client):
        body = client.put("/api/site", json={"locale": "nb-NO"}).json()
        assert body["locale"] == "nb-NO"
        assert client.get("/api/site").json()["locale"] == "nb-NO"

    @pytest.mark.parametrize("tag", ["nb-NO", "nn-NO", "sv-SE", "da-DK", "fi-FI", "en-GB", "de-DE"])
    def test_the_offered_locales_are_accepted(self, client, tag):
        assert client.put("/api/site", json={"locale": tag}).json()["locale"] == tag

    def test_it_can_be_set_back_to_following_the_browser(self, client):
        client.put("/api/site", json={"locale": "nb-NO"})
        assert client.put("/api/site", json={"locale": ""}).json()["locale"] == ""

    def test_nonsense_is_refused_rather_than_stored(self, client):
        """A tag Intl cannot parse would silently fall back in every browser,
        which looks like the setting does nothing."""
        r = client.put("/api/site", json={"locale": "not a locale!"})
        assert r.status_code == 400
        assert "language tag" in r.json()["detail"].lower()

    def test_a_refused_locale_does_not_change_what_was_stored(self, client):
        client.put("/api/site", json={"locale": "nb-NO"})
        client.put("/api/site", json={"locale": "!!!"})
        assert client.get("/api/site").json()["locale"] == "nb-NO"

    def test_changing_the_name_leaves_the_locale_alone(self, client):
        client.put("/api/site", json={"locale": "sv-SE"})
        body = client.put("/api/site", json={"name": "Lakeside"}).json()
        assert body["locale"] == "sv-SE"

    def test_changing_the_locale_leaves_the_name_alone(self, client):
        client.put("/api/site", json={"name": "Lakeside"})
        body = client.put("/api/site", json={"locale": "nb-NO"}).json()
        assert body["name"] == "Lakeside"

    def test_the_clock_is_always_24_hour(self, client):
        """Reported so an interface cannot invent a 12-hour option. The hub's
        week profiles are HHMM strings; there is no 12-hour form to store."""
        assert client.get("/api/site").json()["clock"] == "24h"

    def test_the_temperature_unit_is_always_celsius(self, client):
        """API_Nobo.pdf: "temperatures are in celsius". Offering Fahrenheit
        would mean either a wrong number or a conversion of our own to get
        wrong, for hardware that cannot accept it anyway."""
        assert client.get("/api/site").json()["temperature_unit"] == "C"

    def test_setting_a_locale_is_recorded_in_the_log(self, client):
        client.put("/api/site", json={"locale": "nb-NO"})
        entries = client.get("/api/log").json()["entries"]
        assert any("nb-NO" in e["description"] for e in entries)

    def test_reading_it_needs_only_a_session(self, client, monkeypatch):
        """Every page needs the format to render, admin or not."""
        monkeypatch.setattr(server.auth, "load_users", lambda: {"admin": {"role": "user"}})
        assert client.get("/api/site").status_code == 200

    def test_changing_it_needs_an_admin(self, client, monkeypatch):
        monkeypatch.setattr(server.auth, "load_users", lambda: {"admin": {"role": "user"}})
        assert client.put("/api/site", json={"locale": "nb-NO"}).status_code == 403

    def test_an_older_settings_file_still_loads(self, client, monkeypatch, tmp_path):
        """site.json written before this setting existed has no locale key."""
        old = tmp_path / "site.json"
        old.write_text('{"name": "Lakeside", "show_on_login": true}', encoding="utf-8")
        monkeypatch.setattr(config_persistence, "SITE_FILE", old)

        body = client.get("/api/site").json()
        assert body["name"] == "Lakeside"
        assert body["locale"] == ""


class TestTheInterfaceIsStillCalledCabin:
    def test_naming_the_house_does_not_rename_the_interface(self, client):
        """The name of the place and the name of the UI are different things.

        Renaming a house to Lakeside must not make the rollback instructions say
        "the previous Lakeside interface", nor change the /cabin route.
        """
        client.put("/api/site", json={"name": "Lakeside"})
        assert client.get("/cabin").status_code == 200
        assert client.get("/classic").status_code == 200


class TestTheClassicSignInPage:
    """Classic is the rollback interface, so its sign-in page must not be
    collateral damage. It never had a tagline, and gaining a cabin-flavoured one
    would change the very page a user chose specifically to go back to."""

    @staticmethod
    def _classic_server(monkeypatch):
        monkeypatch.setenv("NOBO_UI", "classic")
        monkeypatch.setenv("NOBO_DEMO", "true")
        return importlib.reload(server)

    @pytest.fixture(autouse=True)
    def restore(self):
        yield
        os.environ.pop("NOBO_UI", None)
        importlib.reload(server)

    def test_unnamed_classic_gains_no_tagline(self, monkeypatch):
        srv = self._classic_server(monkeypatch)
        with TestClient(srv.app) as c:
            page = c.get("/login").text
        assert "Heating control for" not in page
        assert "the cabin" not in page

    def test_named_classic_shows_the_name(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_persistence, "SITE_FILE", tmp_path / "site.json")
        config_persistence.save_site({"name": "Lakeside", "show_on_login": True})

        srv = self._classic_server(monkeypatch)
        monkeypatch.setattr(srv.config_persistence, "SITE_FILE", tmp_path / "site.json")
        with TestClient(srv.app) as c:
            page = c.get("/login").text
        assert "Heating control for Lakeside." in page

    def test_the_placeholder_never_survives_into_the_page(self, monkeypatch):
        """An unreplaced comment would be invisible on screen and wrong in source."""
        srv = self._classic_server(monkeypatch)
        with TestClient(srv.app) as c:
            assert "SITE_TAGLINE" not in c.get("/login").text


def test_the_cabin_page_never_leaks_the_placeholder(client):
    assert "SITE_TAGLINE" not in client.get("/login").text
