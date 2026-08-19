"""
Unit tests for server.py helper functions.

These tests do not require a real Nobø Hub — they exercise pure-Python
helper code and the schedule validation models.
"""

import os

os.environ.setdefault("NOBO_DEMO", "true")

import pytest

# Import the module under test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server


# ---------------------------------------------------------------------------
# detect_device_type
# ---------------------------------------------------------------------------

class TestDetectDeviceType:
    def test_known_prefix_ntb2r(self):
        name, supports_comfort, supports_eco = server.detect_device_type("210000016247")
        assert name == "NTB-2R"
        assert supports_comfort is True
        assert supports_eco is True

    def test_known_prefix_r80(self):
        name, supports_comfort, supports_eco = server.detect_device_type("160004028112")
        assert isinstance(name, str)
        assert len(name) > 0

    def test_zero_prefix_fallback(self):
        name, supports_comfort, supports_eco = server.detect_device_type("000000016249")
        assert name == "NTB-2R"
        assert supports_comfort is True
        assert supports_eco is True

    def test_unknown_prefix_returns_unknown(self):
        name, supports_comfort, supports_eco = server.detect_device_type("999000000000")
        assert name == "Unknown"
        assert supports_comfort is False
        assert supports_eco is False

    def test_too_short_serial(self):
        name, supports_comfort, supports_eco = server.detect_device_type("12")
        assert name == "Unknown"
        assert supports_comfort is False
        assert supports_eco is False

    def test_serial_with_spaces(self):
        # detect_device_type should handle serials with spaces
        name1, _, _ = server.detect_device_type("210000016247")
        name2, _, _ = server.detect_device_type("210 000 016 247")
        assert name1 == name2


# ---------------------------------------------------------------------------
# format_serial_display
# ---------------------------------------------------------------------------

class TestFormatSerialDisplay:
    def test_formats_12_digit_serial(self):
        result = server.format_serial_display("210000016247")
        assert result == "210 000 016 247"

    def test_already_spaced_input(self):
        result = server.format_serial_display("210 000 016 247")
        assert result == "210 000 016 247"

    def test_short_serial_returned_as_is(self):
        result = server.format_serial_display("123")
        assert result == "123"


# ---------------------------------------------------------------------------
# parse_serial_input
# ---------------------------------------------------------------------------

class TestParseSerialInput:
    def test_strips_spaces(self):
        assert server.parse_serial_input("210 000 016 247") == "210000016247"

    def test_no_spaces_unchanged(self):
        assert server.parse_serial_input("210000016247") == "210000016247"

    def test_strips_leading_trailing_whitespace(self):
        assert server.parse_serial_input("  210000016247  ") == "210000016247"


# ---------------------------------------------------------------------------
# add_log_entry
# ---------------------------------------------------------------------------

class TestAddLogEntry:
    def setup_method(self):
        # Clear the log before each test
        with server.log_lock:
            server.command_log.clear()

    def test_entry_added(self):
        server.add_log_entry("sent", "test description", "cmd", "api")
        with server.log_lock:
            entries = list(server.command_log)
        assert len(entries) == 1
        assert entries[0]["direction"] == "sent"
        assert entries[0]["description"] == "test description"
        assert entries[0]["command"] == "cmd"
        assert entries[0]["source"] == "api"

    def test_timestamp_format(self):
        server.add_log_entry("received", "msg")
        with server.log_lock:
            entry = list(server.command_log)[0]
        # Timestamp should be ISO-like: "YYYY-MM-DDTHH:MM:SS.mmm"
        ts = entry["timestamp"]
        assert "T" in ts
        assert len(ts) == 23  # "2024-01-01T12:00:00.000"

    def test_defaults_source_api(self):
        server.add_log_entry("sent", "desc")
        with server.log_lock:
            entry = list(server.command_log)[0]
        assert entry["source"] == "api"


# ---------------------------------------------------------------------------
# ScheduleBlock validation
# ---------------------------------------------------------------------------

class TestScheduleBlock:
    def _make_block(self, start, end, mode):
        return server.ScheduleBlock(start=start, end=end, mode=mode)

    def test_valid_block(self):
        b = self._make_block("07:00", "22:00", "comfort")
        b.validate_fields()  # Should not raise

    def test_invalid_mode(self):
        b = self._make_block("07:00", "22:00", "sauna")
        with pytest.raises(ValueError, match="Invalid mode"):
            b.validate_fields()

    def test_off_is_a_valid_schedule_mode(self):
        # The hub's week profiles have a genuine "off" state (status digit 4),
        # so a schedule may switch heating off entirely. This is distinct from
        # an override, where "off" is not a legal mode.
        b = self._make_block("07:00", "22:00", "off")
        b.validate_fields()

    def test_invalid_start_time(self):
        b = self._make_block("25:00", "22:00", "eco")
        with pytest.raises(ValueError, match="Invalid start time"):
            b.validate_fields()

    def test_end_before_start(self):
        b = self._make_block("22:00", "07:00", "eco")
        with pytest.raises(ValueError, match="end .* must be after start"):
            b.validate_fields()

    def test_24_00_is_valid_end(self):
        b = self._make_block("22:00", "24:00", "eco")
        b.validate_fields()  # Should not raise

    def test_end_equals_start_invalid(self):
        b = self._make_block("07:00", "07:00", "comfort")
        with pytest.raises(ValueError, match="end .* must be after start"):
            b.validate_fields()


# ---------------------------------------------------------------------------
# ScheduleUpdate validation
# ---------------------------------------------------------------------------

VALID_SCHEDULE = {
    day: [
        {"start": "00:00", "end": "07:00", "mode": "eco"},
        {"start": "07:00", "end": "22:00", "mode": "comfort"},
        {"start": "22:00", "end": "24:00", "mode": "eco"},
    ]
    for day in server.SCHEDULE_DAYS
}


class TestScheduleUpdate:
    def test_valid_schedule_passes(self):
        su = server.ScheduleUpdate(schedule=VALID_SCHEDULE)
        su.validate_schedule()  # Should not raise

    def test_missing_day_raises(self):
        incomplete = {k: v for k, v in VALID_SCHEDULE.items() if k != "friday"}
        su = server.ScheduleUpdate(schedule=incomplete)
        with pytest.raises(ValueError, match="Missing days"):
            su.validate_schedule()

    def test_unknown_day_raises(self):
        extra = dict(VALID_SCHEDULE)
        extra["holiday"] = VALID_SCHEDULE["monday"]
        su = server.ScheduleUpdate(schedule=extra)
        with pytest.raises(ValueError, match="Unknown days"):
            su.validate_schedule()

    def test_gap_between_blocks_raises(self):
        gapped = dict(VALID_SCHEDULE)
        gapped["monday"] = [
            {"start": "00:00", "end": "06:00", "mode": "eco"},
            # Gap from 06:00 to 07:00
            {"start": "07:00", "end": "22:00", "mode": "comfort"},
            {"start": "22:00", "end": "24:00", "mode": "eco"},
        ]
        su = server.ScheduleUpdate(schedule=gapped)
        with pytest.raises(ValueError, match="gap/overlap"):
            su.validate_schedule()

    def test_does_not_start_at_midnight_raises(self):
        bad = dict(VALID_SCHEDULE)
        bad["monday"] = [
            {"start": "01:00", "end": "22:00", "mode": "comfort"},
            {"start": "22:00", "end": "24:00", "mode": "eco"},
        ]
        su = server.ScheduleUpdate(schedule=bad)
        with pytest.raises(ValueError, match="start at 00:00"):
            su.validate_schedule()

    def test_does_not_end_at_midnight_raises(self):
        bad = dict(VALID_SCHEDULE)
        bad["monday"] = [
            {"start": "00:00", "end": "07:00", "mode": "eco"},
            {"start": "07:00", "end": "22:00", "mode": "comfort"},
        ]
        su = server.ScheduleUpdate(schedule=bad)
        with pytest.raises(ValueError, match="end at 24:00"):
            su.validate_schedule()

    def test_empty_day_raises(self):
        bad = dict(VALID_SCHEDULE)
        bad["monday"] = []
        su = server.ScheduleUpdate(schedule=bad)
        with pytest.raises(ValueError, match="no time blocks"):
            su.validate_schedule()
