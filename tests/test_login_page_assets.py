"""The sign-in page's artwork has to load without a session.

The bug: every asset was behind the login wall, including the icons that appear
*on* the sign-in page — which is, by definition, shown to somebody who has not
logged in. The browser asked for the icon, got a 302 to /login, received HTML
where it expected an image, gave up, and drew a letter from the hostname
instead. A big "N" for nobo.example.com.

Nothing caught it because every existing test authenticates first, and an
authenticated request for the icon works perfectly.

There is a second, older half to the same problem: ``/favicon.ico`` had been on
the public allow-list since the beginning but nothing ever served it, so it
returned 404. Browsers request that path unprompted for any page that does not
declare an icon, and treat a 404 the same way — draw a letter.
"""

import pytest
from fastapi.testclient import TestClient

import server
from server import app


@pytest.fixture
def anon():
    """A client with no session at all — what a stranger gets."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed_in():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


def _declared_icons(html: str):
    """The icon URLs a page actually asks the browser to fetch."""
    import re

    return re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', html)


class TestTheSignInPageCanLoadItsOwnArtwork:
    def test_the_page_declares_at_least_one_icon(self, anon):
        """Without a declaration the browser falls back to /favicon.ico, and
        then to a generated letter."""
        icons = _declared_icons(anon.get("/login").text)
        assert icons, "the sign-in page declares no icon at all"

    def test_every_icon_it_declares_is_readable_without_a_session(self, anon):
        """This is the actual bug. Each icon must return an image, not a
        redirect to the very page that is trying to display it."""
        icons = _declared_icons(anon.get("/login").text)
        for url in icons:
            if not url.startswith("/"):
                continue
            response = anon.get(url, follow_redirects=False)
            assert response.status_code == 200, (
                f"{url} answered {response.status_code} to a signed-out browser "
                f"— the sign-in page cannot show its own icon"
            )
            assert response.headers["content-type"].startswith("image/"), (
                f"{url} returned {response.headers['content-type']}, not an image"
            )

    def test_the_icon_is_not_a_redirect_to_login(self, anon):
        """Spelled out separately because this is the exact failure: a 302 here
        hands the browser an HTML page where an image was expected."""
        response = anon.get("/static/ui/cabin/icon.svg", follow_redirects=False)
        assert response.status_code != 302, "the icon redirects to /login"

    def test_the_public_list_matches_what_the_page_asks_for(self, anon):
        """If someone adds an asset to the sign-in page and forgets the
        allow-list, this fails instead of the icon quietly vanishing."""
        icons = {u for u in _declared_icons(anon.get("/login").text) if u.startswith("/")}
        missing = icons - server.PUBLIC_ASSET_PATHS - server.PUBLIC_PATHS
        assert not missing, f"declared on the sign-in page but not public: {sorted(missing)}"


class TestFaviconIco:
    def test_it_is_served_rather_than_404(self, anon):
        """It has been on the public allow-list all along with nothing behind
        it. Browsers ask for this path on their own."""
        response = anon.get("/favicon.ico")
        assert response.status_code == 200, "/favicon.ico still 404s"

    def test_it_returns_an_image(self, anon):
        response = anon.get("/favicon.ico")
        assert response.headers["content-type"].startswith("image/"), (
            f"got {response.headers['content-type']} — a browser will ignore this"
        )

    def test_it_needs_no_session(self, anon):
        assert anon.get("/favicon.ico", follow_redirects=False).status_code == 200

    def test_it_is_the_same_icon_the_interface_uses(self, anon, signed_in):
        """One icon to maintain, not two that drift apart."""
        assert anon.get("/favicon.ico").content == \
            signed_in.get("/static/ui/cabin/icon.svg").content


class TestBothInterfacesDeclareAnIcon:
    """The classic interface declared none, so browsers asked for
    /favicon.ico, got a 404, and drew a letter."""

    @pytest.mark.parametrize("path", ["/", "/cabin", "/classic"])
    def test_the_page_declares_an_icon(self, signed_in, path):
        icons = _declared_icons(signed_in.get(path).text)
        assert icons, f"{path} declares no icon"

    @pytest.mark.parametrize("path", ["/", "/cabin", "/classic"])
    def test_the_declared_icon_actually_exists(self, signed_in, path):
        for url in _declared_icons(signed_in.get(path).text):
            if url.startswith("/"):
                assert signed_in.get(url).status_code == 200, f"{path} points at a missing {url}"


class TestOpeningTheIconsDidNotOpenAnythingElse:
    """The allow-list is a hole in a deny-by-default policy, so its edges
    matter more than the entries themselves."""

    def test_the_public_asset_list_is_small_and_explicit(self):
        """A prefix rule like "/static/ui/ is public" would grow silently. Every
        entry has to be a specific file somebody chose."""
        assert len(server.PUBLIC_ASSET_PATHS) <= 4, (
            "the public asset list is growing — is every entry really needed "
            "before sign-in?"
        )
        for path in server.PUBLIC_ASSET_PATHS:
            assert not path.endswith("/"), f"{path} looks like a directory, not a file"

    def test_no_public_asset_is_a_script_or_a_page(self):
        """Images and stylesheets only. A public .js or .html would be a real
        widening of what a stranger can reach."""
        for path in server.PUBLIC_ASSET_PATHS:
            assert path.rsplit(".", 1)[-1] in {"svg", "png", "ico", "css"}, (
                f"{path} is not artwork"
            )

    @pytest.mark.parametrize("path", [
        "/api/zones",
        "/api/status",
        "/api/site",
        "/static/ui/cabin/cabin.js",
        "/static/ui/shared/core.js",
        "/static/app.js",
    ])
    def test_everything_else_still_needs_a_session(self, anon, path):
        response = anon.get(path, follow_redirects=False)
        assert response.status_code in (302, 401), (
            f"{path} became reachable without a login ({response.status_code})"
        )

    def test_the_interface_itself_still_needs_a_session(self, anon):
        assert anon.get("/", follow_redirects=False).status_code == 302
