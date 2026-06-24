"""Tests for the shared RFC3339 serializer (Rule 10 / go-tfe compat)."""

from datetime import UTC, datetime, timedelta, timezone

from terrapod.api.serialization import rfc3339


def test_none_returns_none():
    assert rfc3339(None) is None


def test_utc_emits_trailing_z_not_offset():
    dt = datetime(2026, 6, 24, 12, 30, 5, tzinfo=UTC)
    out = rfc3339(dt)
    assert out == "2026-06-24T12:30:05Z"
    assert "+00:00" not in out


def test_non_utc_is_converted_to_utc_z():
    # A +02:00 wall-clock time normalises to UTC with a Z suffix.
    dt = datetime(2026, 6, 24, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    assert rfc3339(dt) == "2026-06-24T12:30:05Z"
