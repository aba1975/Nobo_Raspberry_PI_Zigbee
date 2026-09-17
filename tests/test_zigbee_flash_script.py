"""The guards on ``scripts/zigbee-flash-router.sh``.

This script writes to flash, and the two ways that goes wrong are both
unrecoverable by ordinary means:

* flashing the adapter that is *currently the coordinator* destroys the running
  network — every sensor is paired to it and the network key lives on it;
* flashing the wrong image can lock the bootloader permanently, after which the
  stick cannot be rescued in software at all.

Neither failure announces itself beforehand, and the person running this is by
definition doing it for the first time.  So the refusals are tested rather than
reasoned about, with a fake ``/dev/serial/by-id`` and no hardware anywhere near
it.  The flashing itself is not exercised here: nothing in a test suite can
verify that a radio was written.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "zigbee-flash-router.sh"

COORDINATOR = (
    "usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_9e082b9c37f5ef11bc43a1a29ed47d52"
    "-if00-port0"
)
SPARE = (
    "usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "-if00-port0"
)


@pytest.fixture
def rig(tmp_path):
    """A fake repository and a fake /dev/serial/by-id, wired together.

    The script resolves the adapter through ``readlink -f``, so the by-id entry
    has to be a real symlink to a real file for the comparison it makes to mean
    anything.  Faking that comparison would test nothing.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)

    serial = tmp_path / "by-id"
    serial.mkdir()
    ttys = tmp_path / "tty"
    ttys.mkdir()

    def adapter(name, tty):
        (ttys / tty).write_text("", encoding="utf-8")
        os.symlink(ttys / tty, serial / name)
        return serial / name

    def env(adapter_path):
        (root / ".env").write_text(
            f"COMPOSE_PROFILES=tls,zigbee\nNOBO_ZIGBEE_ADAPTER={adapter_path}\n",
            encoding="utf-8",
        )

    def run(*args):
        return subprocess.run(
            ["bash", str(root / "scripts" / SCRIPT.name), *args],
            capture_output=True, text=True, timeout=60,
            # The adapter directory is overridable so discovery can be
            # exercised without hardware; it defaults to /dev/serial/by-id.
            env={**os.environ, "NOBO_SERIAL_BY_ID": str(serial)},
            cwd=str(root),
        )

    return type("Rig", (), {
        "root": root, "serial": serial, "adapter": staticmethod(adapter),
        "env": staticmethod(env), "run": staticmethod(run),
    })


def test_it_refuses_to_flash_the_adapter_that_is_the_coordinator(rig):
    """The one that would cost a day of re-pairing sensors at windows."""
    path = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.env(path)

    result = rig.run("--device", str(path), "--yes")

    assert result.returncode != 0
    assert "Refusing" in result.stderr
    assert "coordinator" in result.stderr
    assert "re-pairing every device" in result.stderr


def test_the_refusal_survives_the_path_being_written_differently(rig):
    """.env holds a by-id path and the caller may pass the /dev/ttyUSB one, or
    the other way round.  Comparing the strings would miss it; both are
    resolved."""
    path = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.env(path)

    result = rig.run("--device", str(Path(os.path.realpath(path))), "--yes")

    assert result.returncode != 0
    assert "Refusing" in result.stderr


def test_there_is_no_flag_to_override_the_refusal():
    """A flag would be pasted from a forum by somebody in a hurry, which is
    exactly the state of mind this guard exists for."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--force" not in text
    assert "There is no override flag" in text


def test_an_adapter_it_does_not_recognise_is_left_alone(rig):
    """The wrong image can lock a bootloader permanently, and this script knows
    exactly one adapter."""
    path = rig.adapter("usb-Some_Other_Vendor_Widget-if00", "ttyUSB1")
    rig.env("/dev/serial/by-id/" + COORDINATOR)

    result = rig.run("--device", str(path), "--yes")

    assert result.returncode != 0
    assert "does not look like a Sonoff ZBDongle-P" in result.stdout


def test_a_device_that_is_not_there_is_reported_plainly(rig):
    rig.env("/dev/serial/by-id/" + COORDINATOR)

    result = rig.run("--device", "/dev/serial/by-id/nothing-here", "--yes")

    assert result.returncode != 0
    assert "No such device" in result.stderr


def test_the_firmware_is_not_a_parameter():
    """Choosing the image is the dangerous decision, so the caller does not get
    to make it: it is fetched for this adapter and checked against the digest
    GitHub publishes."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--firmware" not in text
    assert "CC1352P2_CC2652P_launchpad_router_" in text
    assert "_coordinator_" not in text.split("# -- the firmware")[1]
    assert "Checksum mismatch" in text


def test_it_writes_with_verification_and_the_sonoff_bootloader_toggle():
    """-v verifies the write; --bootloader-sonoff-usb is what saves opening the
    enclosure to reach the boot button, and is specific to this adapter."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--bootloader-sonoff-usb" in text
    assert "-e -w -v" in text


def test_it_says_the_result_needs_no_computer():
    """The whole reason for choosing a dongle over a smart plug is that it can
    be hidden on a charger, and "USB stick" implies otherwise."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "needs power only, not a computer" in text


def test_it_says_to_pair_the_sensors_afterwards():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "afterwards" in text and "parent" in text


def test_the_flashing_tool_is_pinned_rather_than_tracking_a_branch_silently():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "CC2538_BSL_REF" in text
    assert "Pinned rather than tracking the branch tip" in text
    # A commit, not a branch name: the code that writes to flash is reviewed.
    assert len([c for c in text.split("CC2538_BSL_REF:-")[1][:40] if c in "0123456789abcdef"]) >= 40


def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK), "it is documented as bash scripts/... but"


def test_with_only_the_coordinator_plugged_in_it_explains_rather_than_picking_it(rig):
    """The state somebody is in the first time: one dongle, doing the job."""
    path = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.env(path)

    result = rig.run("--yes")

    assert result.returncode != 0
    assert "coordinator" in result.stdout
    assert "Plug the new dongle in as well" in result.stdout


def test_the_spare_is_found_without_being_named(rig):
    """Two plugged in, one of them working: there is only one right answer and
    the script should not make somebody copy a 70-character path to say it."""
    coordinator = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.adapter(SPARE, "ttyUSB1")
    rig.env(coordinator)

    result = rig.run()  # no --yes: stops at the confirmation prompt

    assert SPARE in result.stdout
    assert "About to flash" in result.stdout
    # And it got there without ever considering the working one.
    assert "Refusing" not in result.stderr


def test_two_spares_are_not_guessed_between(rig):
    second = SPARE.replace("aaaa", "bbbb")
    coordinator = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.adapter(SPARE, "ttyUSB1")
    rig.adapter(second, "ttyUSB2")
    rig.env(coordinator)

    result = rig.run("--yes")

    assert result.returncode != 0
    assert "More than one adapter" in result.stdout
    assert SPARE in result.stdout and second in result.stdout


def test_nothing_is_written_unless_the_word_is_typed(rig):
    """A y/n prompt is answered reflexively; this one needs a word."""
    coordinator = rig.adapter(COORDINATOR, "ttyUSB0")
    rig.adapter(SPARE, "ttyUSB1")
    rig.env(coordinator)

    result = rig.run()

    assert result.returncode != 0
    assert "Nothing was written" in result.stdout
    # And the prompt itself needs a word, not a reflexive keystroke.
    assert 'Type "flash" to continue' in SCRIPT.read_text(encoding="utf-8")


def test_a_plain_tty_path_is_recognised_as_the_stick_it_is(rig):
    """The model name only appears in the by-id form. Refusing a /dev/ttyUSB
    path for saying the same thing differently would be its own puzzle."""
    coordinator = rig.adapter(COORDINATOR, "ttyUSB0")
    spare = rig.adapter(SPARE, "ttyUSB1")
    rig.env(coordinator)

    result = rig.run("--device", str(Path(os.path.realpath(spare))))

    assert "does not look like" not in result.stdout
    assert "About to flash" in result.stdout
