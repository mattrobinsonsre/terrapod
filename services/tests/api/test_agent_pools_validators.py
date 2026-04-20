"""Tests for pure validators in the agent_pools router.

Focus: the `_validate_owner_email` regex was flagged by CodeQL
(py/polynomial-redos) because the old pattern `[^@\\s]+@[^@\\s]+\\.[^@\\s]+`
had overlapping quantifiers that allowed catastrophic backtracking on
pathological inputs. The tightened pattern is linear.
"""

import time

import pytest
from fastapi import HTTPException

from terrapod.api.routers.agent_pools import _MAX_EMAIL_LEN, _validate_owner_email


class TestValidateOwnerEmail:
    @pytest.mark.parametrize(
        "email",
        [
            "user@example.com",
            "matt.robinson@acrolinx.com",
            "first.last+tag@subdomain.example.co.uk",
            "a@b.c",
            "MixedCase@Example.ORG",
        ],
    )
    def test_accepts_valid(self, email: str) -> None:
        assert _validate_owner_email(email) == email

    @pytest.mark.parametrize(
        "email",
        [
            "",  # empty → None, handled separately
        ],
    )
    def test_empty_returns_none(self, email: str) -> None:
        assert _validate_owner_email(email) is None

    def test_none_returns_none(self) -> None:
        assert _validate_owner_email(None) is None

    @pytest.mark.parametrize(
        "email",
        [
            "no-at-sign.example.com",
            "a@b",  # no TLD segment
            "a@.b",  # empty segment before dot
            "a@b.",  # empty segment after dot
            "a@b..c",  # empty middle segment
            "a@b c.com",  # whitespace in domain
            "a b@c.com",  # whitespace in local part
            "two@at@signs.com",
            "@leading.com",
        ],
    )
    def test_rejects_malformed(self, email: str) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_owner_email(email)
        assert exc.value.status_code == 422

    def test_rejects_overlength(self) -> None:
        email = "a" * (_MAX_EMAIL_LEN - 6) + "@b.com"
        assert len(email) == _MAX_EMAIL_LEN
        # At the limit is fine
        assert _validate_owner_email(email) == email

        over = email + "x"
        with pytest.raises(HTTPException) as exc:
            _validate_owner_email(over)
        assert exc.value.status_code == 422
        assert "cannot exceed" in exc.value.detail

    def test_redos_payload_completes_quickly(self) -> None:
        """The old pattern would hang on this shape; the new one must be linear.

        Known slow input from CodeQL: strings starting with '!@!.' and many
        repetitions of '!.'. Without the fix, this took seconds; with the
        fix, validation should return (with a reject) in milliseconds.
        """
        # Build a pathological input within the length cap so the cap itself
        # doesn't short-circuit the check — we want to exercise the regex.
        payload = "!@!." + "!." * 100
        assert len(payload) < _MAX_EMAIL_LEN

        start = time.perf_counter()
        with pytest.raises(HTTPException):
            _validate_owner_email(payload)
        elapsed = time.perf_counter() - start

        # A linear regex should handle this in well under 100ms; the old
        # polynomial-backtracking version would take many seconds.
        assert elapsed < 0.5, f"validation took {elapsed:.3f}s — possible ReDoS regression"
