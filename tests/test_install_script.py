"""The installer has to work on a Pi nobody has logged into before.

It is the one piece of this system that runs before anything else exists, on a
machine whose owner is following written instructions rather than reasoning
about shell scripts. So the things that would waste their evening — a wrong
repository, a clobbered configuration, a duplicated key — are pinned here.

These do not install anything. They read the script, and run its riskier
helpers in a sandbox.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
SOURCE = INSTALL.read_text(encoding="utf-8")


def test_the_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_the_script_is_executable():
    assert INSTALL.stat().st_mode & 0o111


def test_the_env_editor_is_portable():
    """`sed -i` takes a mandatory argument on BSD and none on GNU, and any
    substitution delimiter can turn up inside a value."""
    code = [
        line for line in SOURCE.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if "sed -i" in line]
    assert "awk -v k=" in SOURCE


def test_it_clones_this_fork_and_not_the_one_it_came_from():
    """This repository is a fork. The installer used to point at the original,
    which would have installed a build with no sensor support at all."""
    assert "Nobo_Raspberry_PI_Zigbee.git" in SOURCE
    assert "Nobo_Raspberry_PI.git" not in SOURCE


def test_it_refuses_to_run_without_root():
    assert 'id -u' in SOURCE and "sudo bash scripts/install.sh" in SOURCE


def test_it_reads_answers_from_the_terminal_not_stdin():
    """`curl ... | sudo bash` leaves stdin pointing at the download, so every
    question would read end-of-file and silently take the default."""
    assert "exec 3</dev/tty" in SOURCE
    assert "<&3" in SOURCE


def test_it_defaults_to_demo_mode():
    """A first-time installation must come up showing something, without a hub
    serial to hand."""
    assert 'ask_yes_no "  Connect a real Nobo hub now?" "n"' in SOURCE
    assert "set_env NOBO_DEMO true" in SOURCE


def test_zigbee_is_optional_and_detected_rather_than_typed():
    assert "/dev/serial/by-id" in SOURCE
    # Never /dev/ttyUSB0: USB numbering is not stable across reboots.
    assert "ttyUSB0" not in SOURCE
    assert "set_env COMPOSE_PROFILES zigbee" in SOURCE
    # And skipping it must leave the profile empty, not unset-and-inherited.
    assert 'set_env COMPOSE_PROFILES ""' in SOURCE


def test_it_asks_for_a_password_rather_than_shipping_a_known_one():
    assert "at least 8 characters" in SOURCE
    assert "read -rs ADMIN_PASSWORD" in SOURCE
    # Never on the command line, where the process list would show it.
    assert "-e ADMIN_PASSWORD" in SOURCE
    assert 'os.environ["ADMIN_PASSWORD"]' in SOURCE


def test_it_hashes_with_the_application_own_code():
    """A hash produced any other way is a password that cannot be used."""
    assert "auth.hash_password" in SOURCE
    assert "auth.save_users" in SOURCE


def test_it_waits_for_the_service_before_declaring_success():
    assert "/api/health" in SOURCE
    assert "journalctl" in SOURCE


def test_re_running_keeps_an_existing_configuration():
    assert "--reconfigure" in SOURCE
    assert "Keeping the existing configuration" in SOURCE


def test_it_tells_the_user_where_to_go_at_the_end():
    assert "http://${IP_ADDRESS:-<this-pi>}:${PORT}" in SOURCE
    assert "update.sh" in SOURCE and "backup.sh" in SOURCE


# -- the helper that edits .env, actually run -------------------------------


def _set_env(tmp_path: Path, initial: str, pairs: list[tuple[str, str]]) -> str:
    """Run the installer's own set_env against a throwaway .env.

    Lifted from the script rather than copied into the test, so this cannot
    drift away from what actually runs on the Pi.
    """
    start = SOURCE.index("set_env() {")
    function = SOURCE[start:SOURCE.index("\n}\n", start) + 3]

    (tmp_path / ".env").write_text(initial, encoding="utf-8")
    body = "\n".join(f'set_env {key} "{value}"' for key, value in pairs)
    script = "\n".join([
        "set -euo pipefail",
        f'INSTALL_DIR="{tmp_path}"',
        function,
        body,
        'cat "$INSTALL_DIR/.env"',
    ])
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed")
def test_setting_a_value_replaces_rather_than_appends(tmp_path):
    out = _set_env(
        tmp_path, "NOBO_DEMO=true\nNOBO_PORT=8000\n", [("NOBO_DEMO", "false")]
    )
    assert out.count("NOBO_DEMO=") == 1
    assert "NOBO_DEMO=false" in out
    # And it leaves everything else alone.
    assert "NOBO_PORT=8000" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed")
def test_setting_a_new_value_appends_once(tmp_path):
    out = _set_env(
        tmp_path,
        "NOBO_DEMO=true\n",
        [("NOBO_ZIGBEE_ADAPTER", "/dev/serial/by-id/usb-x"), ("NOBO_TZ", "Europe/Oslo")],
    )
    assert out.count("NOBO_ZIGBEE_ADAPTER=") == 1
    assert "NOBO_ZIGBEE_ADAPTER=/dev/serial/by-id/usb-x" in out
    assert "NOBO_TZ=Europe/Oslo" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed")
def test_a_device_path_survives_intact(tmp_path):
    """Slashes in the value must not be read as sed delimiters."""
    path = "/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_9e08-if00-port0"
    out = _set_env(tmp_path, "NOBO_ZIGBEE_ADAPTER=\n", [("NOBO_ZIGBEE_ADAPTER", path)])
    assert f"NOBO_ZIGBEE_ADAPTER={path}" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed")
def test_a_commented_example_is_not_mistaken_for_the_setting(tmp_path):
    """.env.example ships these keys commented out, and a commented line must
    not be edited in place — that would leave the real setting missing."""
    out = _set_env(
        tmp_path, "#NOBO_TZ=Europe/Oslo\n", [("NOBO_TZ", "Europe/Stockholm")]
    )
    assert "#NOBO_TZ=Europe/Oslo" in out
    assert "NOBO_TZ=Europe/Stockholm" in out


# -- the written instructions -----------------------------------------------


GUIDE = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_repository_link_points_at_this_fork():
    """Cloning the upstream project installs a build with no sensors at all."""
    for name, text in (("INSTALL.md", GUIDE), ("README.md", README)):
        assert "Nobo_Raspberry_PI.git" not in text, name


def test_the_guide_names_scripts_that_exist():
    import re

    for script in set(re.findall(r"scripts/([a-z_]+\.sh)", GUIDE)):
        assert (ROOT / "scripts" / script).exists(), script


def test_the_readme_points_at_the_guide():
    assert "docs/INSTALL.md" in README


def test_the_guide_starts_in_demo_mode():
    """Somebody installing for the first time will not have a hub serial to
    hand, and should still end up with something to look at."""
    assert "answer **n** the first time" in GUIDE
    assert "demo mode" in GUIDE


def test_the_guide_warns_about_the_things_that_actually_go_wrong():
    # Each of these cost real time during commissioning.
    assert "where it will actually live" in GUIDE      # pairing at the Pi
    assert "an hour late" in GUIDE                     # wrong time zone
    assert "It looks like" in GUIDE                    # the long silent build
    assert "blank at first" in GUIDE                   # battery reporting
    assert "re-pairing every sensor" in GUIDE          # backing up the mesh


def test_the_guide_does_not_ask_anyone_to_edit_a_file():
    """The old installer ended at "now edit .env with nano", which is exactly
    the step a first-time installer gets wrong."""
    assert "nano" not in GUIDE


def test_a_private_repository_fails_with_an_explanation_not_a_prompt():
    """`git clone` of a private repo sits waiting for a username that whoever
    is following the instructions does not know they need."""
    assert "GIT_TERMINAL_PROMPT=0" in SOURCE
    assert "settings/tokens" in SOURCE
    assert "Could not download the software" in SOURCE


def test_the_guide_leads_with_a_plain_clone():
    """The repository is public, so the ordinary path needs no credentials and
    must not be buried under a detour for the case that no longer applies."""
    step = GUIDE[GUIDE.index("## Step 4"):GUIDE.index("## Step 5")]
    plain = step.index("git clone https://github.com/aba1975/Nobo_Raspberry_PI_Zigbee.git")
    assert "YOUR_TOKEN" not in step[:plain]
    # The token path survives for anyone working from a private fork, but as
    # troubleshooting rather than as a step.
    assert "<details>" in step
    assert "personal-access-tokens" in step
    assert "Contents: Read-only" in step


def test_the_readme_does_not_still_call_the_sensors_unbuilt():
    """It described Zigbee2MQTT as "a future provider ... not been tested
    against hardware" long after one was paired and reporting."""
    assert "is a future" not in README
    assert "has not been tested against hardware" not in README


def test_the_readme_describes_the_sensors_it_ships_with():
    """This is the public front page. A headline feature that appears only in
    the .env reference is a feature nobody finds."""
    assert "Door and window sensors (optional)" in README
    assert "COMPOSE_PROFILES=zigbee" in README
    # The two facts that otherwise cost an afternoon each.
    assert "`contact: true` means closed" in README
    assert "cannot be asked for" in README
    # And it stays honest about what is still unproved.
    assert "Still unproved" in README
