"""The deployment configuration has to agree with itself.

Three files describe how this runs — ``compose.yml``, ``tls/Caddyfile`` and
``.env.example`` — and nothing at runtime checks that they still match. A port
changed in one and not the others produces a proxy that answers 502, a variable
documented but never read, or a setting read but never documented. None of that
shows up until someone is standing in a cold house.

The rule these tests defend, above all: **turning HTTPS on is opt-in, and
leaving it alone changes nothing.** Existing installations must keep working
exactly as before after pulling this.
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read compose.yml")

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yml"
CADDYFILE = ROOT / "tls" / "Caddyfile"
CADDYFILE_ACME = ROOT / "tls" / "Caddyfile.acme"
CADDY_DOCKERFILE = ROOT / "tls" / "Dockerfile"
APP_DOCKERFILE = ROOT / "Dockerfile"
ENV_EXAMPLE = ROOT / ".env.example"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def env_example():
    return ENV_EXAMPLE.read_text(encoding="utf-8")


class TestHttpsIsOptional:
    """An existing installation must not change behaviour by pulling this."""

    def test_the_proxy_is_behind_a_profile(self, compose):
        """Without this, "docker compose up" would start Caddy for everybody —
        including people with no domain, who would get a container failing to
        get a certificate in a restart loop."""
        assert compose["services"]["caddy"]["profiles"] == ["tls"]

    def test_the_application_is_not_behind_a_profile(self, compose):
        """The heating must still start with a plain "docker compose up"."""
        assert "profiles" not in compose["services"]["nobo-web-control"]

    def test_the_default_bind_is_still_every_interface(self, compose):
        """The default has to stay 0.0.0.0:8000 or existing bookmarks break."""
        env = compose["services"]["nobo-web-control"]["environment"]
        assert "NOBO_BIND=${NOBO_BIND:-0.0.0.0}" in env
        assert "NOBO_PORT=${NOBO_PORT:-8000}" in env

    def test_the_shipped_env_file_leaves_http_working(self, env_example):
        """Someone copying .env.example must get the old behaviour, not a
        half-configured proxy."""
        assert re.search(r"^NOBO_BIND=0\.0\.0\.0\s*$", env_example, re.M)
        assert re.search(r"^NOBO_PORT=8000\s*$", env_example, re.M)

    def test_no_variable_is_substituted_without_a_default(self):
        """Every ${VAR} needs a default, even the TLS-only ones.

        Compose substitutes variables for the whole file before it filters by
        profile, so a bare ${NOBO_DOMAIN} makes "docker compose up" print
        warnings for someone who has never asked for HTTPS. Harmless, but it
        looks like something is broken, and this change is supposed to be
        invisible unless you opt in.
        """
        raw = COMPOSE.read_text(encoding="utf-8")
        bare = re.findall(r"\$\{([A-Z0-9_]+)\}", raw)
        assert not bare, f"substituted with no default: {sorted(set(bare))}"


class TestTheFilesAgreeWithEachOther:
    def test_every_variable_compose_reads_is_documented(self, compose, env_example):
        """A variable compose substitutes but .env.example never mentions is one
        nobody will know to set."""
        referenced = set(re.findall(r"\$\{([A-Z0-9_]+)", COMPOSE.read_text(encoding="utf-8")))
        assert referenced, "no variables found — the regex has stopped matching"
        undocumented = sorted(v for v in referenced if v not in env_example)
        assert not undocumented, f"not documented in .env.example: {undocumented}"

    def test_every_variable_the_caddyfile_reads_is_passed_to_it(self, compose):
        """Caddy substitutes {$VAR} from its own environment. Anything a
        Caddyfile expects but compose does not pass resolves to empty, and the
        failure surfaces as a certificate error rather than a missing-variable
        one. Both Caddyfiles are checked, because either can be mounted."""
        passed = {
            entry.split("=", 1)[0]
            for entry in compose["services"]["caddy"]["environment"]
        }
        for path in (CADDYFILE, CADDYFILE_ACME):
            wanted = set(re.findall(r"\{\$([A-Z0-9_]+)", path.read_text(encoding="utf-8")))
            missing = sorted(wanted - passed)
            assert not missing, f"{path.name} reads these but compose never sets them: {missing}"

    def test_the_proxy_and_the_application_use_the_same_port(self, compose):
        """Both sides read NOBO_PORT. If they ever stop, the proxy answers 502
        and the application looks broken while being perfectly healthy."""
        for path in (CADDYFILE, CADDYFILE_ACME):
            text = path.read_text(encoding="utf-8")
            assert "reverse_proxy 127.0.0.1:{$NOBO_PORT:8000}" in text, path.name
        assert "NOBO_PORT=${NOBO_PORT:-8000}" in compose["services"]["caddy"]["environment"]


class TestNothingNeedsAPortOpenToTheInternet:
    """The entire point: HTTPS on a machine nobody outside can reach."""

    def test_the_default_needs_no_external_anything(self):
        """Caddy's own CA: no ACME, no DNS record, no third party, no expiry."""
        caddyfile = CADDYFILE.read_text(encoding="utf-8")
        assert "tls internal" in caddyfile
        assert "acmedns" not in caddyfile, "the default must not depend on acme-dns"

    def test_the_default_does_not_compile_anything(self, compose):
        """Stock Caddy needs no plugin, so nobody should wait for a Go build
        they did not ask for. BuildKit skips the stage that is not selected."""
        args = compose["services"]["caddy"]["build"]["args"]
        assert "${CADDY_BINARY_SOURCE:-caddy-stock}" in args["CADDY_BINARY_SOURCE"]

    def test_both_build_stages_exist(self):
        dockerfile = CADDY_DOCKERFILE.read_text(encoding="utf-8")
        assert "AS caddy-stock" in dockerfile
        assert "AS caddy-plugins" in dockerfile
        assert "FROM ${CADDY_BINARY_SOURCE}" in dockerfile

    def test_the_stage_selector_is_a_global_arg(self):
        """An ARG used in a FROM line must be declared before the first FROM.

        Declared anywhere later it expands to nothing and the build dies with
        "base name should not be blank" — which says nothing about ARG scope
        and cost a build to work out.
        """
        dockerfile = CADDY_DOCKERFILE.read_text(encoding="utf-8")
        # Match instructions, not the word appearing in a comment above them.
        first_from = re.search(r"^FROM ", dockerfile, re.M)
        declaration = re.search(r"^ARG CADDY_BINARY_SOURCE", dockerfile, re.M)
        assert first_from, "no FROM instruction found"
        assert declaration, "CADDY_BINARY_SOURCE is never declared"
        assert declaration.start() < first_from.start(), (
            "CADDY_BINARY_SOURCE must be declared before the first FROM"
        )

    def test_the_acme_option_still_uses_a_dns_challenge(self):
        """The alternative must also avoid opening a port — DNS-01, not HTTP-01."""
        acme = CADDYFILE_ACME.read_text(encoding="utf-8")
        assert "dns acmedns" in acme

    def test_the_acme_option_compiles_a_dns_plugin_in(self):
        """Stock Caddy has no DNS providers, so DNS-01 silently cannot work.
        This is the single easiest thing to get wrong here."""
        dockerfile = CADDY_DOCKERFILE.read_text(encoding="utf-8")
        assert "xcaddy build --with" in dockerfile
        assert "caddy-dns" in dockerfile

    def test_the_plugin_can_be_swapped_without_editing_files(self, compose, env_example):
        """Anyone whose DNS host has a real API should be able to use it."""
        args = compose["services"]["caddy"]["build"]["args"]
        assert "${CADDY_DNS_PLUGIN:-github.com/caddy-dns/acmedns}" in args["CADDY_DNS_PLUGIN"]
        assert "CADDY_DNS_PLUGIN=" in env_example

    def test_the_caddyfile_can_be_swapped_without_editing_files(self, compose, env_example):
        mounts = compose["services"]["caddy"]["volumes"]
        assert any("${NOBO_CADDYFILE:-./tls/Caddyfile}" in m for m in mounts), mounts
        assert "NOBO_CADDYFILE=" in env_example


class TestCertificatesSurviveARestart:
    def test_caddy_data_is_a_named_volume(self, compose):
        """The CA and the certificates live in /data.

        With Caddy's own CA, losing this regenerates the root — and every
        device the old one was installed on starts warning again. With Let's
        Encrypt, it re-requests on every restart and hits the rate limit of
        five per name per week.
        """
        assert "caddy-data" in compose["volumes"]
        mounts = compose["services"]["caddy"]["volumes"]
        assert any(m.startswith("caddy-data:/data") for m in mounts), mounts

    def test_the_caddyfile_is_mounted_read_only(self, compose):
        mounts = compose["services"]["caddy"]["volumes"]
        assert any("/etc/caddy/Caddyfile:ro" in m for m in mounts), mounts


class TestTheProxyStartsWithTheRestOfTheStack:
    """A reboot must not leave nothing answering.

    The systemd unit runs a plain ``docker compose up``, with no ``--profile``.
    So a profiled service is invisible to it — and to ``scripts/update.sh``, and
    to every command a user types out of habit. Combined with
    ``NOBO_BIND=127.0.0.1`` that is the worst possible pairing: the application
    comes back listening only on loopback, the proxy never starts, and the
    heating is unreachable until somebody SSHes in.

    ``COMPOSE_PROFILES`` in ``.env`` is what makes the profile part of the
    normal stack, so every one of those paths picks it up without being taught
    about profiles individually. Verified by rebooting the Pi: both containers
    returned and HTTPS answered unattended.
    """

    def test_the_env_file_documents_the_profile_switch(self, env_example):
        assert re.search(r"^#?COMPOSE_PROFILES=tls\s*$", env_example, re.M), (
            "COMPOSE_PROFILES is not in .env.example, so turning HTTPS on "
            "leaves the systemd service starting the application alone"
        )

    def test_it_ships_commented_out(self, env_example):
        """It must not be on by default, or a fresh install starts a proxy it
        has no certificate settings for."""
        assert re.search(r"^#COMPOSE_PROFILES=tls\s*$", env_example, re.M), (
            "COMPOSE_PROFILES should ship commented out"
        )

    def test_the_readme_tells_people_to_set_it(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "COMPOSE_PROFILES=tls" in readme

    def test_the_service_does_not_hard_code_a_profile(self):
        """The unit stays profile-agnostic on purpose: .env decides. Putting
        --profile tls here would start a proxy for people who never asked."""
        unit = (ROOT / "deploy" / "systemd" / "nobo-control.service").read_text(encoding="utf-8")
        assert "--profile" not in unit
        assert "docker compose up" in unit


class TestTheBackupCoversWhatTheReadmeClaims:
    def test_the_certificate_store_is_backed_up(self):
        """The README says the CA is included. It has to actually be.

        With Caddy's own CA the private root lives in that volume, and it is
        the one installed on every device in the house. A backup that silently
        omits it is worse than one that never claimed to have it.
        """
        backup = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
        assert "caddy" in backup.lower(), "backup.sh never looks at the Caddy volume"
        assert "caddy-data" in backup

    def test_a_missing_caddy_volume_does_not_fail_the_backup(self):
        """Most installations never turn HTTPS on. Their backup must still
        succeed, and must still contain the heating data."""
        backup = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
        caddy_section = backup[backup.index("CADDY_VOLUME"):]
        assert "exit 1" not in caddy_section, (
            "a missing Caddy volume aborts the backup"
        )


class TestTheApplicationCanHideBehindTheProxy:
    def test_the_container_honours_the_bind_address(self):
        """Hard-coding 0.0.0.0 in the image would leave the plain-HTTP port
        answering the network even with TLS in front of it."""
        dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
        assert "${NOBO_BIND:-0.0.0.0}" in dockerfile
        assert "${NOBO_PORT:-8000}" in dockerfile

    def test_uvicorn_replaces_the_shell(self):
        """Without exec, uvicorn is not PID 1 and "compose down" waits for the
        kill timeout on every stop."""
        dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
        assert "exec uvicorn" in dockerfile

    def test_forwarded_headers_are_trusted_only_from_the_local_proxy(self):
        """X-Forwarded-Proto decides whether the session cookie is marked
        Secure. Honouring it from anywhere would let the network set that."""
        dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
        assert "--proxy-headers" in dockerfile
        assert "--forwarded-allow-ips=127.0.0.1" in dockerfile

    def test_the_health_check_uses_the_loopback_address(self, compose):
        """It has to work whether the app is on 0.0.0.0 or 127.0.0.1, or the
        container is reported unhealthy the moment TLS is switched on."""
        for source in (
            APP_DOCKERFILE.read_text(encoding="utf-8"),
            " ".join(compose["services"]["nobo-web-control"]["healthcheck"]["test"]),
        ):
            assert "127.0.0.1" in source
            assert "localhost:8000" not in source

    def test_the_health_check_follows_the_configured_port(self, compose):
        check = " ".join(compose["services"]["nobo-web-control"]["healthcheck"]["test"])
        assert "NOBO_PORT" in check


class TestTheProxyDoesNotWeakenTheApplication:
    def test_security_headers_are_set(self):
        for path in (CADDYFILE, CADDYFILE_ACME):
            text = path.read_text(encoding="utf-8")
            for header in (
                "Strict-Transport-Security",
                "X-Content-Type-Options",
                "X-Frame-Options",
                "Referrer-Policy",
            ):
                assert header in text, f"{header} missing from {path.name}"

    def test_the_proxy_talks_to_the_loopback_address(self):
        """Proxying to the LAN address would send requests back out over the
        network in plain text, which is what this is meant to stop."""
        for path in (CADDYFILE, CADDYFILE_ACME):
            text = path.read_text(encoding="utf-8")
            assert "reverse_proxy 127.0.0.1:" in text, path.name
            assert "reverse_proxy 0.0.0.0" not in text, path.name

    def test_no_credentials_are_committed(self):
        """The acme-dns password belongs in .env, which is gitignored."""
        acme = CADDYFILE_ACME.read_text(encoding="utf-8")
        for field in ("username", "password", "subdomain"):
            match = re.search(rf"^\s*{field}\s+(\S+)", acme, re.M)
            assert match, f"{field} missing from Caddyfile.acme"
            assert match.group(1).startswith("{$"), (
                f"{field} looks like a literal value, not a placeholder"
            )

    def test_the_env_file_ships_no_real_credentials(self, env_example):
        """The ACME settings ship commented out, and must stay valueless."""
        for key in ("ACMEDNS_USERNAME", "ACMEDNS_PASSWORD", "ACMEDNS_SUBDOMAIN"):
            match = re.search(rf"^#?{key}=(.*)$", env_example, re.M)
            assert match, f"{key} is not mentioned in .env.example"
            assert not match.group(1).strip(), f"{key} ships with a value in it"

    def test_the_gitignore_covers_the_env_file(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert re.search(r"^\.env$", gitignore, re.M), ".env must not be committable"
