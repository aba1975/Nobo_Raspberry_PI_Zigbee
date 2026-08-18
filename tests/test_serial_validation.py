"""
Unit tests for the validate_serial() helper function.

These tests do not require a real Nobø Hub.
"""

import os

os.environ.setdefault("NOBO_DEMO", "true")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server


class TestValidateSerial:
    def test_valid_12_digit_serial(self):
        is_valid, result = server.validate_serial("210000016247")
        assert is_valid is True
        assert result == "210000016247"

    def test_valid_serial_with_spaces(self):
        is_valid, result = server.validate_serial("210 000 016 247")
        assert is_valid is True
        assert result == "210000016247"

    def test_valid_serial_with_leading_trailing_whitespace(self):
        is_valid, result = server.validate_serial("  120000012345  ")
        assert is_valid is True
        assert result == "120000012345"

    def test_serial_with_letters_is_invalid(self):
        is_valid, result = server.validate_serial("ABCDEFGHIJKL")
        assert is_valid is False
        assert "12 digits" in result

    def test_serial_with_symbols_is_invalid(self):
        is_valid, result = server.validate_serial("2100-0016-247!")
        assert is_valid is False
        assert "12 digits" in result

    def test_too_short_serial_is_invalid(self):
        is_valid, result = server.validate_serial("12345")
        assert is_valid is False
        assert "12 digits" in result

    def test_too_long_serial_is_invalid(self):
        is_valid, result = server.validate_serial("2100000162471")
        assert is_valid is False
        assert "12 digits" in result

    def test_empty_string_is_invalid(self):
        is_valid, result = server.validate_serial("")
        assert is_valid is False
        assert "12 digits" in result

    def test_mixed_digits_and_letters_is_invalid(self):
        is_valid, result = server.validate_serial("21000001624A")
        assert is_valid is False
        assert "12 digits" in result

    def test_spaces_only_is_invalid(self):
        is_valid, result = server.validate_serial("            ")
        assert is_valid is False
        assert "12 digits" in result
