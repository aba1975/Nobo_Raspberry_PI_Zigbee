"""Static guards for the UI concept prototypes.

These do not need a browser. They exist because of a specific failure: a button
was added to Concept D's markup and shipped looking perfectly normal, but
nothing was listening for its click, so pressing it did nothing at all and gave
the user no clue why. A missing handler is invisible to every other test in this
suite, because the server is entirely happy.
"""

import re
from pathlib import Path

import pytest

CONCEPTS = Path(__file__).resolve().parent.parent / "app" / "static" / "concepts"
PRODUCTION_UI = Path(__file__).resolve().parent.parent / "app" / "static" / "ui"

# The adopted interface matters most here, so it is checked alongside the
# concepts it came from rather than being trusted because it shipped.
UI_DIRS = sorted(
    [p for p in CONCEPTS.glob("*") if p.is_dir() and p.name != "shared"]
    + [p for p in PRODUCTION_UI.glob("*") if p.is_dir() and p.name != "shared"]
)


def _concept_sources(concept: Path):
    html = (concept / "index.html").read_text(encoding="utf-8")
    js = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(concept.glob("*.js"))
    )
    return html, js


@pytest.mark.parametrize("concept", UI_DIRS, ids=lambda p: p.name)
def test_every_button_with_an_id_is_wired_up(concept):
    """A <button id="..."> that no script ever mentions is a dead control."""
    html, js = _concept_sources(concept)
    if not js.strip():
        pytest.skip(f"{concept.name} has no script of its own")

    dead = []
    for tag in re.findall(r"<button\b[^>]*>", html):
        match = re.search(r'\bid="([^"]+)"', tag)
        if not match:
            continue
        button_id = match.group(1)
        if button_id not in js:
            dead.append(button_id)

    assert not dead, (
        f"{concept.name}/index.html has buttons no script refers to, so clicking "
        f"them does nothing: {dead}"
    )


@pytest.mark.parametrize("concept", UI_DIRS, ids=lambda p: p.name)
def test_every_view_section_is_switched(concept):
    """A view that switchView() never hides stays on screen underneath another."""
    html, js = _concept_sources(concept)
    if "switchView" not in js:
        pytest.skip(f"{concept.name} does not use a switchView")

    views = re.findall(r'<section class="view" id="([^"]+)"', html)
    body = js.split("function switchView", 1)[1].split("\n  }", 1)[0]
    missing = [v for v in views if v not in body]

    assert not missing, (
        f"{concept.name}: switchView() never touches {missing}, so those views "
        f"cannot be shown or hidden"
    )


@pytest.mark.parametrize("concept", UI_DIRS, ids=lambda p: p.name)
def test_every_local_asset_reference_resolves(concept):
    """A moved file leaves a 404 that no server test would ever notice."""
    static_root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, _ = _concept_sources(concept)

    broken = []
    for ref in re.findall(r'(?:href|src)="(/static/[^"?#]+)', html):
        if not (static_root / ref[len("/static/"):]).exists():
            broken.append(ref)

    assert not broken, f"{concept.name}/index.html points at files that do not exist: {broken}"


def test_the_adopted_interface_is_where_the_server_looks_for_it():
    """The production entry points must exist, whichever way NOBO_UI is set."""
    static_root = Path(__file__).resolve().parent.parent / "app" / "static"
    assert (static_root / "ui" / "cabin" / "index.html").exists()
    assert (static_root / "ui" / "shared" / "core.js").exists()
    assert (static_root / "index.html").exists(), "the classic interface must stay installed"


def test_the_installed_app_starts_at_the_app_root():
    """A manifest scoped to a static path installs a home-screen icon that opens
    a file rather than the app, and traps the user outside their own site."""
    import json

    manifest = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "app" / "static" / "ui" / "cabin" / "manifest.webmanifest"
        ).read_text(encoding="utf-8")
    )
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
