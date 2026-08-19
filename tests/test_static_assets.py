"""
Static asset consistency checks.

These guard against a class of bug that is invisible to the Python test suite and
easy to miss in review: the front-end JavaScript and the stylesheet disagreeing
about a class name, so a dialog is "opened" but never actually rendered.

That is exactly what happened to the user settings panel — auth.js toggled
``.active`` while style.css only ever defined ``.modal.show``, leaving the user
icon apparently dead.
"""

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
if not STATIC_DIR.is_dir():
    # Inside the container the app is unpacked at /app, so static/ is a sibling
    # of the mounted tests directory rather than under an extra app/ level.
    STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture(scope="module")
def css():
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _modal_open_state_classes(css_text):
    """Every class X for which the stylesheet defines a `.modal.X` rule."""
    return set(re.findall(r"\.modal\.([A-Za-z0-9_-]+)", css_text))


class TestModalOpenStates:
    def test_stylesheet_defines_show(self, css):
        assert "show" in _modal_open_state_classes(css)

    def test_user_panel_toggle_class_is_styled(self, css):
        """auth.js must open the user panel with a class the stylesheet renders."""
        auth_js = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

        toggle = re.search(
            r"function toggleUserPanel\(\).*?\n\}", auth_js, re.S
        )
        assert toggle, "toggleUserPanel() not found in auth.js"

        added = re.findall(r"classList\.add\('([A-Za-z0-9_-]+)'\)", toggle.group(0))
        assert added, "toggleUserPanel() no longer adds a class to open the panel"

        styled = _modal_open_state_classes(css)
        for cls in added:
            assert cls in styled, (
                f"auth.js opens the user panel with '.{cls}', but style.css has no "
                f"'.modal.{cls}' rule, so the panel would stay invisible. "
                f"Styled open-state classes: {sorted(styled)}"
            )

    def test_app_js_modal_classes_are_styled(self, css):
        """Same guarantee for the modals driven from app.js."""
        app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styled = _modal_open_state_classes(css)

        used = set(
            re.findall(
                r"getElementById\('\w*[Mm]odal'\)\??\.classList\.add\('([A-Za-z0-9_-]+)'\)",
                app_js,
            )
        )
        assert used, "no modal open calls found in app.js — has the pattern changed?"
        for cls in used:
            assert cls in styled, f"app.js opens a modal with unstyled class '.{cls}'"


class TestCacheBusting:
    def test_local_scripts_are_versioned(self, index_html):
        """
        Browsers cache these aggressively. A fix that ships without a bumped
        version string reaches nobody, which has already caused one false
        "the fix did not work" report.
        """
        scripts = re.findall(r'<script src="(/static/[^"]+)"', index_html)
        assert scripts, "no local script tags found in index.html"

        unversioned = [s for s in scripts if "?v=" not in s]
        assert not unversioned, f"script tags missing a ?v= cache buster: {unversioned}"

    def test_local_stylesheets_are_versioned(self, index_html):
        """The stylesheet had no cache buster, so CSS-only fixes never arrived."""
        sheets = re.findall(r'<link[^>]+href="(/static/[^"]+\.css[^"]*)"', index_html)
        assert sheets, "no local stylesheet links found in index.html"

        unversioned = [s for s in sheets if "?v=" not in s]
        assert not unversioned, f"stylesheet links missing a ?v= cache buster: {unversioned}"


class TestUserPanelMarkup:
    def test_panel_and_hooks_exist(self, index_html):
        assert 'id="userPanel"' in index_html
        assert 'id="userPanelBody"' in index_html
        assert "toggleUserPanel()" in index_html
        assert "closeUserPanel()" in index_html

    def test_panel_uses_modal_class(self, index_html):
        panel = re.search(r'<div id="userPanel"[^>]*>', index_html)
        assert panel, "userPanel element not found"
        assert 'class="modal"' in panel.group(0), (
            "userPanel must carry the base .modal class for the open-state rule to apply"
        )
