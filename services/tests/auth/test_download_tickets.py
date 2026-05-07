"""Tests for download tickets — stateless HMAC cap-tokens for CV downloads.

The ticket layer is the auth gate for browser-native streaming
downloads (where plain navigation can't carry an Authorization
header). Mirrors the runner-token test shape: deterministic signing
key, mint/verify roundtrip, TTL clamping, tampering detection,
expiry handling.
"""

import time
from unittest.mock import patch

import pytest

from terrapod.auth import download_tickets
from terrapod.auth.download_tickets import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    mint_ticket,
    verify_ticket,
)


@pytest.fixture(autouse=True)
def _reset_signing_key():
    """Reset the module-level signing key cache between tests."""
    download_tickets._signing_key = None
    yield
    download_tickets._signing_key = None


@pytest.fixture
def _mock_settings():
    """Deterministic database_url so the derived key is stable across tests."""
    with patch("terrapod.config.settings") as mock_settings:
        mock_settings.database_url = "postgresql+asyncpg://test:test@localhost/test"
        yield mock_settings


class TestMintAndVerifyRoundtrip:
    def test_basic_roundtrip(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "user@example.com", ttl_seconds=300)
        payload = verify_ticket(ticket)
        assert payload is not None
        assert payload.resource_kind == "cv"
        assert payload.resource_id == "abc-123"
        assert payload.user_email == "user@example.com"
        # expires_at is roughly now + 300s
        assert payload.expires_at - int(time.time()) <= 300
        assert payload.expires_at - int(time.time()) >= 295

    def test_ticket_starts_with_dlticket_prefix(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "user@example.com")
        assert ticket.startswith("dlticket:")

    def test_email_with_at_sign_roundtrips(self, _mock_settings):
        # The `@` would otherwise collide with the `:` field separator
        # if the email weren't base64-encoded.
        email = "user.name+tag@sub.example.com"
        ticket = mint_ticket("cv", "abc-123", email)
        payload = verify_ticket(ticket)
        assert payload is not None
        assert payload.user_email == email

    def test_empty_email_roundtrips(self, _mock_settings):
        # Edge case: minter could be a service-account session with no
        # email. Ticket should still be verifiable.
        ticket = mint_ticket("cv", "abc-123", "")
        payload = verify_ticket(ticket)
        assert payload is not None
        assert payload.user_email == ""


class TestTTLClamping:
    def test_zero_ttl_falls_back_to_default(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com", ttl_seconds=0)
        payload = verify_ticket(ticket)
        assert payload is not None
        # Within ~5s of DEFAULT_TTL
        assert abs((payload.expires_at - int(time.time())) - DEFAULT_TTL_SECONDS) <= 5

    def test_negative_ttl_falls_back_to_default(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com", ttl_seconds=-100)
        payload = verify_ticket(ticket)
        assert payload is not None
        assert abs((payload.expires_at - int(time.time())) - DEFAULT_TTL_SECONDS) <= 5

    def test_oversized_ttl_clamped_to_max(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com", ttl_seconds=999_999)
        payload = verify_ticket(ticket)
        assert payload is not None
        assert abs((payload.expires_at - int(time.time())) - MAX_TTL_SECONDS) <= 5


class TestVerificationFailures:
    def test_rejects_non_dlticket_string(self, _mock_settings):
        assert verify_ticket("not-a-ticket") is None
        assert verify_ticket("runtok:something:else") is None
        assert verify_ticket("") is None

    def test_rejects_wrong_part_count(self, _mock_settings):
        # Too few colons
        assert verify_ticket("dlticket:cv:abc") is None
        # Too many colons (extra field)
        assert verify_ticket("dlticket:cv:abc:e:300:1234567890:sig:extra") is None

    def test_rejects_non_integer_ttl(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com", ttl_seconds=300)
        # Replace the ttl field with garbage. Field index 4 in a 7-field token.
        parts = ticket.split(":")
        parts[4] = "notanumber"
        assert verify_ticket(":".join(parts)) is None

    def test_rejects_tampered_resource_id(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com")
        # Change the id — HMAC was computed over the original id.
        tampered = ticket.replace(":abc-123:", ":xyz-999:")
        assert verify_ticket(tampered) is None

    def test_rejects_tampered_kind(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com")
        tampered = ticket.replace(":cv:", ":sv:", 1)
        assert verify_ticket(tampered) is None

    def test_rejects_tampered_signature(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com")
        # Flip the last hex char of the signature.
        tampered = ticket[:-1] + ("0" if ticket[-1] != "0" else "1")
        assert verify_ticket(tampered) is None

    def test_rejects_expired_ticket(self, _mock_settings):
        # Mint with the smallest TTL that survives the clamp (1s).
        ticket = mint_ticket("cv", "abc-123", "u@example.com", ttl_seconds=1)
        time.sleep(1.5)
        assert verify_ticket(ticket) is None

    def test_signing_key_change_invalidates_old_tickets(self, _mock_settings):
        ticket = mint_ticket("cv", "abc-123", "u@example.com")
        # Pretend the database password rotated — derived key changes.
        download_tickets._signing_key = None
        _mock_settings.database_url = "postgresql+asyncpg://test:OTHER@localhost/test"
        assert verify_ticket(ticket) is None


class TestResourceScoping:
    def test_ticket_for_one_resource_cannot_be_used_for_another(self, _mock_settings):
        # Two tickets for different CVs — neither accepts the other's id.
        t1 = mint_ticket("cv", "cv-aaa", "u@example.com")
        t2 = mint_ticket("cv", "cv-bbb", "u@example.com")
        p1 = verify_ticket(t1)
        p2 = verify_ticket(t2)
        assert p1 is not None and p1.resource_id == "cv-aaa"
        assert p2 is not None and p2.resource_id == "cv-bbb"
        # Cross-substituting the id components breaks the HMAC.
        # (Simulates an attacker who tries to retarget by string surgery.)
        forged = t1.replace(":cv-aaa:", ":cv-bbb:")
        assert verify_ticket(forged) is None
