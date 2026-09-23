"""What an alert looks like when it arrives.

Plain text was honest and unreadable: every alert looked like every other one
and the room it concerned was a word in the middle of a sentence. These are
read on a telephone, usually while doing something else, so the tests are about
whether the *place* and the *severity* can be taken in at a glance — and about
the two ways HTML mail goes wrong, which are escaping and clients that will not
render it.
"""

import pytest

from notifications import SEVERITY_STYLES, render_html


def test_the_severity_is_named_in_words_not_only_colour():
    """Colour alone fails for a colour-blind reader and in a client that
    re-colours text."""
    html = render_html("Kitchen is open", "Body.", "critical", "Cabin")
    assert "Urgent" in html
    html = render_html("A sensor is quiet", "Body.", "warning", "Cabin")
    assert "Warning" in html


def test_the_place_is_picked_out_of_the_headline():
    html = render_html(
        "Open and empty: Kitchen, Loft", "Body.", "critical", "Cabin",
        highlight=["Kitchen", "Loft"],
    )
    colour = SEVERITY_STYLES["critical"][1]
    assert f'color:{colour};font-weight:800;">Kitchen<' in html
    assert f'color:{colour};font-weight:800;">Loft<' in html


def test_a_longer_name_is_not_carved_up_by_a_shorter_one():
    """"Large Bathroom Window" contains "Large Bathroom". Marking the short one
    first would split the long one and leave broken markup in the middle."""
    html = render_html(
        "Large Bathroom Window is quiet", "Body.", "warning", "Cabin",
        highlight=["Large Bathroom", "Large Bathroom Window"],
    )
    assert ">Large Bathroom Window<" in html
    assert html.count("<span") == 1


def test_a_room_name_cannot_inject_markup():
    """Room names are typed by a person and go straight into the message."""
    html = render_html(
        "<script>alert(1)</script> is open", "Body.", "warning", "Cabin",
        highlight=["<script>alert(1)</script>"],
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_injected_name_is_still_highlighted_after_escaping():
    """Escaping first and marking afterwards has to leave the name findable,
    or the safety fix would quietly disable the feature."""
    html = render_html(
        "Kitchen & Loft is open", "Body.", "warning", "Cabin",
        highlight=["Kitchen & Loft"],
    )
    assert "Kitchen &amp; Loft</span>" in html


def test_facts_are_laid_out_as_labels_and_values():
    html = render_html(
        "Large Bathroom Window is quiet", "Body.", "warning", "Cabin",
        facts=(("Sensor", "Large Bathroom Window"), ("Last heard", "11:20")),
    )
    assert "Sensor" in html and "Last heard" in html
    assert "<table" in html


def test_no_facts_means_no_empty_table():
    html = render_html("Kitchen is open", "Body.", "warning", "Cabin")
    assert "<table" not in html


def test_the_first_paragraph_leads_and_the_rest_is_quieter():
    """The lead carries the news; the rest is explanation and must not compete
    with it."""
    html = render_html(
        "Kitchen is open", "This is the news.\n\nThis is the detail.",
        "warning", "Cabin",
    )
    lead = html.index("This is the news.")
    detail = html.index("This is the detail.")
    assert lead < detail
    assert "font-size:16px" in html[:lead]
    assert "color:#666" in html[lead:detail]


def test_nothing_has_a_background_to_invert():
    """A client in dark mode re-colours backgrounds and leaves text alone, so a
    coloured banner can end up dark red on near-black. Colour goes on text."""
    body = render_html("Kitchen is open", "Body.", "critical", "Cabin")
    body = body[body.index("<body"):]
    for banned in ("background:#b3261e", "background-color:#b3261e",
                   "background:#9a6b00"):
        assert banned not in body


def test_there_are_no_images_and_no_style_block():
    """Remote images are blocked by default and would report back the moment
    the mail was opened; Gmail strips a <style> block."""
    html = render_html("Kitchen is open", "Body.", "warning", "Cabin")
    assert "<img" not in html
    assert "<style" not in html


def test_it_says_where_to_turn_it_off():
    html = render_html("Kitchen is open", "Body.", "warning", "Cabin")
    assert "Settings" in html


# -- the plain part, which is still the version of record ------------------


def test_html_is_an_alternative_never_a_replacement(monkeypatch):
    """A client that refuses HTML, or a person on a connection that will not
    load it, must lose nothing."""
    import notifications

    captured = {}

    class _FakeServer:
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, msg): captured["msg"] = msg
        def quit(self): pass

    monkeypatch.setattr(notifications.smtplib, "SMTP",
                        lambda *a, **k: _FakeServer())
    notifications._send_email_blocking(
        {"email": {
            "host": "h", "port": 587, "to_addrs": ["a@b.c"],
            "from_addr": "x@y.z", "username": "", "password": "",
            "security": "starttls",
        }},
        "Subject", "The plain words.", "<html><body>Rich</body></html>",
    )

    msg = captured["msg"]
    assert msg.get_content_type() == "multipart/alternative"
    parts = [p.get_content_type() for p in msg.iter_parts()]
    assert parts == ["text/plain", "text/html"], (
        "plain must come first — order is what tells a client which to prefer"
    )
    assert "The plain words." in msg.get_body(("plain",)).get_content()


def test_a_sender_given_no_html_still_sends(monkeypatch):
    """The html argument is optional so anything calling this the old way —
    a script, an older test — still delivers."""
    import notifications

    captured = {}

    class _FakeServer:
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, msg): captured["msg"] = msg
        def quit(self): pass

    monkeypatch.setattr(notifications.smtplib, "SMTP",
                        lambda *a, **k: _FakeServer())
    notifications._send_email_blocking(
        {"email": {
            "host": "h", "port": 587, "to_addrs": ["a@b.c"],
            "from_addr": "x@y.z", "username": "", "password": "",
            "security": "starttls",
        }},
        "Subject", "Plain only.",
    )

    assert captured["msg"].get_content_type() == "text/plain"


def test_the_plain_part_carries_the_facts_too():
    """Somebody reading the plain version must not be given less information,
    only a plainer arrangement of it."""
    import notifications

    sent = []
    n = notifications.Notifier()
    n.settings = notifications.load_settings()
    n.settings["enabled"] = True
    n.settings["events"]["hub_offline"] = True
    n.settings["min_minutes_between"] = 0
    n.send_impl = lambda cfg, subject, body, html=None: sent.append((body, html))

    n.notify("hub_offline", "Subject", "Prose.", severity="critical",
             facts=(("Room", "Kitchen"), ("Since", "17:42")))

    body, html = sent[0]
    assert "Room:" in body and "Kitchen" in body
    assert "Since:" in body and "17:42" in body
    assert "Kitchen" in html
