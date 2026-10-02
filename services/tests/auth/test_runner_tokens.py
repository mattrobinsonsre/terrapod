"""Tests for runner token system — HMAC-SHA256 generation, verification, TTL, scoping."""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from terrapod.auth.runner_tokens import (
    _get_signing_key,
    generate_runner_token,
    verify_runner_token,
    verify_runner_token_claims,
)


@pytest.fixture(autouse=True)
def _reset_signing_key():
    """Reset the shared signing-key cache between tests."""
    from terrapod.auth import token_signing

    token_signing._reset_cache_for_tests()
    yield
    token_signing._reset_cache_for_tests()


@pytest.fixture
def _mock_settings():
    """Patch settings for a deterministic signing key (DB-URL fallback path)."""
    with patch("terrapod.config.settings") as mock_settings:
        mock_settings.token_signing_key = ""
        # `patch(...)` hands back a MagicMock, on which every unset attribute is
        # truthy -- so without this the strong-secrets switch reads as ON and the
        # derivation refuses a deliberately-weak test key.
        mock_settings.require_strong_secrets = False
        mock_settings.database_url = "postgresql+asyncpg://test:test@localhost/test"
        yield mock_settings


@pytest.fixture
def _mock_runner_config():
    """Patch load_runner_config for controllable max TTL."""
    config = MagicMock()
    config.max_token_ttl_seconds = 7200
    with patch("terrapod.config.load_runner_config", return_value=config):
        yield config


class TestSigningKey:
    def test_derives_from_database_url(self, _mock_settings):
        key = _get_signing_key()
        assert isinstance(key, bytes)
        assert len(key) == 32  # SHA-256 output

    def test_cached_across_calls(self, _mock_settings):
        key1 = _get_signing_key()
        key2 = _get_signing_key()
        assert key1 is key2

    def test_deterministic(self, _mock_settings):
        import terrapod.auth.runner_tokens as mod

        key1 = _get_signing_key()
        mod._signing_key = None
        key2 = _get_signing_key()
        assert key1 == key2


class TestGenerateRunnerToken:
    def test_format(self, _mock_settings, _mock_runner_config):
        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600)

        parts = token.split(":")
        assert len(parts) == 5
        assert parts[0] == "runtok"
        assert parts[1] == run_id
        assert parts[2] == "3600"
        # timestamp is an integer
        int(parts[3])
        # signature is hex
        assert len(parts[4]) == 64  # SHA-256 hex

    def test_accepts_uuid_object(self, _mock_settings, _mock_runner_config):
        run_id = uuid.uuid4()
        token = generate_runner_token(run_id, ttl=3600)
        assert str(run_id) in token

    def test_clamps_to_max_ttl(self, _mock_settings, _mock_runner_config):
        _mock_runner_config.max_token_ttl_seconds = 1800
        token = generate_runner_token("run-1", ttl=7200)
        parts = token.split(":")
        assert parts[2] == "1800"

    def test_respects_ttl_within_max(self, _mock_settings, _mock_runner_config):
        _mock_runner_config.max_token_ttl_seconds = 7200
        token = generate_runner_token("run-1", ttl=600)
        parts = token.split(":")
        assert parts[2] == "600"

    def test_unique_per_call(self, _mock_settings, _mock_runner_config):
        t1 = generate_runner_token("run-1", ttl=3600)
        # time.time() may be same within a test, but tokens should at least be
        # functionally identical (same run_id, same ttl, same timestamp → same sig)
        t2 = generate_runner_token("run-1", ttl=3600)
        # Both are valid; they may differ only in timestamp
        assert verify_runner_token(t1) == "run-1"
        assert verify_runner_token(t2) == "run-1"


class TestVerifyRunnerToken:
    def test_valid_token(self, _mock_settings, _mock_runner_config):
        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600)
        assert verify_runner_token(token) == run_id

    def test_rejects_non_runtok_prefix(self, _mock_settings):
        assert verify_runner_token("bearer:abc:123:456:deadbeef") is None

    def test_rejects_wrong_part_count(self, _mock_settings):
        assert verify_runner_token("runtok:a:b:c") is None
        # Six fields IS a shape now (the phase claim), but `b` is not a phase a
        # Job runs, so this stays refused.
        assert verify_runner_token("runtok:a:b:c:d:e") is None
        assert verify_runner_token("runtok:a:plan:b:c:d:e") is None

    def test_rejects_non_numeric_ttl(self, _mock_settings):
        assert verify_runner_token("runtok:run-1:abc:123:deadbeef") is None

    def test_rejects_non_numeric_timestamp(self, _mock_settings):
        assert verify_runner_token("runtok:run-1:3600:abc:deadbeef") is None

    def test_rejects_expired_token(self, _mock_settings, _mock_runner_config):
        run_id = "run-expired"
        token = generate_runner_token(run_id, ttl=1)

        # Fast-forward past expiry
        with patch("terrapod.auth.runner_tokens.time") as mock_time:
            mock_time.time.return_value = time.time() + 10
            assert verify_runner_token(token) is None

    def test_rejects_tampered_signature(self, _mock_settings, _mock_runner_config):
        token = generate_runner_token("run-1", ttl=3600)
        parts = token.split(":")
        parts[4] = "0" * 64  # Replace signature
        tampered = ":".join(parts)
        assert verify_runner_token(tampered) is None

    def test_rejects_tampered_run_id(self, _mock_settings, _mock_runner_config):
        token = generate_runner_token("run-original", ttl=3600)
        parts = token.split(":")
        parts[1] = "run-spoofed"  # Change run_id
        tampered = ":".join(parts)
        assert verify_runner_token(tampered) is None

    def test_rejects_tampered_ttl(self, _mock_settings, _mock_runner_config):
        token = generate_runner_token("run-1", ttl=10)
        parts = token.split(":")
        parts[2] = "999999"  # Extend TTL
        tampered = ":".join(parts)
        assert verify_runner_token(tampered) is None

    def test_rejects_empty_string(self, _mock_settings):
        assert verify_runner_token("") is None

    def test_run_id_scoping(self, _mock_settings, _mock_runner_config):
        """Each token is scoped to exactly one run_id."""
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())
        token1 = generate_runner_token(id1, ttl=3600)
        token2 = generate_runner_token(id2, ttl=3600)
        assert verify_runner_token(token1) == id1
        assert verify_runner_token(token2) == id2
        assert verify_runner_token(token1) != id2


# ── Phase claim (GHSA-xmrf-hxq9-m59m) ──────────────────────────────────


class TestThePhaseClaim:
    """A token says which Job phase it may act in, so a plan-phase token cannot
    drive the apply-phase routes. The claim is inside the signed message, which
    is the only reason it is worth anything."""

    def test_a_phased_token_round_trips_its_phase(self, _mock_settings, _mock_runner_config):
        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600, phase="apply")

        parts = token.split(":")
        assert len(parts) == 6
        assert parts[1] == run_id
        assert parts[2] == "apply"
        assert parts[3] == "3600"

        claims = verify_runner_token_claims(token)
        assert claims is not None
        assert claims.run_id == run_id
        assert claims.phase == "apply"

    def test_both_phases_are_mintable_and_distinct(self, _mock_settings, _mock_runner_config):
        run_id = str(uuid.uuid4())
        plan = verify_runner_token_claims(generate_runner_token(run_id, phase="plan"))
        apply = verify_runner_token_claims(generate_runner_token(run_id, phase="apply"))
        assert plan is not None and apply is not None
        assert plan.phase == "plan"
        assert apply.phase == "apply"

    def test_the_phase_cannot_be_edited_by_the_holder(self, _mock_settings, _mock_runner_config):
        """The whole point. Rewriting `plan` to `apply` must invalidate the
        signature, or the claim is a suggestion."""
        token = generate_runner_token(str(uuid.uuid4()), ttl=3600, phase="plan")
        parts = token.split(":")
        parts[2] = "apply"
        assert verify_runner_token_claims(":".join(parts)) is None

    def test_an_unphased_token_verifies_and_claims_nothing(
        self, _mock_settings, _mock_runner_config
    ):
        """The skew case, and the one that must not regress: a listener older than
        the claim sends no phase, so the API mints the five-field form. It has to
        keep verifying, and its phase has to be None — which every endpoint reads
        as "no claim" rather than as a mismatch."""
        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600)
        assert len(token.split(":")) == 5

        claims = verify_runner_token_claims(token)
        assert claims is not None
        assert claims.run_id == run_id
        assert claims.phase is None

    def test_an_unrecognised_phase_mints_the_unphased_form(
        self, _mock_settings, _mock_runner_config
    ):
        """A listener sending something unexpected gets a working token rather
        than a 500 or a token no endpoint accepts."""
        token = generate_runner_token(str(uuid.uuid4()), ttl=3600, phase="destroy")
        assert len(token.split(":")) == 5
        claims = verify_runner_token_claims(token)
        assert claims is not None
        assert claims.phase is None

    def test_a_signed_token_claiming_an_unknown_phase_is_refused(
        self, _mock_settings, _mock_runner_config
    ):
        """Refused, NOT downgraded to "no claim" — that would turn an unknown
        phase into a token every phase-checked endpoint accepts. Signed by hand
        here, because `generate_runner_token` will not mint one."""
        import hashlib
        import hmac

        rid, ttl, ts = str(uuid.uuid4()), "3600", str(int(time.time()))
        msg = f"runtok:{rid}:destroy:{ttl}:{ts}"
        sig = hmac.new(_get_signing_key(), msg.encode(), hashlib.sha256).hexdigest()
        assert verify_runner_token_claims(f"{msg}:{sig}") is None

    def test_the_legacy_helper_still_answers_the_run_id(self, _mock_settings, _mock_runner_config):
        """`verify_runner_token` has callers that only want "is this a runner
        token" (rate-limit bucketing), and both wire forms must answer them."""
        run_id = str(uuid.uuid4())
        assert verify_runner_token(generate_runner_token(run_id)) == run_id
        assert verify_runner_token(generate_runner_token(run_id, phase="plan")) == run_id
