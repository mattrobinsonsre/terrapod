"""Weak key material is reported, and the DSN fallback is reported on identity.

GHSA-hc47-q72v-4vcm. Two secrets: the token signing key (which signs four
stateless token families) and the static KEK (which wraps every DEK).
"""

import base64
import os
from unittest.mock import patch

import pytest

from terrapod import secret_strength as ss


class TestTheBarAcceptsGeneratedMaterial:
    """A 128-bit bar would reject real keys — zxcvbn under-estimates them."""

    @pytest.mark.parametrize("nbytes", [16, 32, 64])
    def test_random_base64_of_any_usable_size_passes(self, nbytes):
        secret = base64.b64encode(os.urandom(nbytes)).decode()
        assert ss.describe_weakness(secret, name="k") is None

    def test_random_hex_passes(self):
        assert ss.describe_weakness(os.urandom(32).hex(), name="k") is None

    def test_the_documented_generator_passes(self):
        # The message tells operators to run `openssl rand -base64 32`; if that
        # output did not pass, the advice would be wrong.
        secret = base64.b64encode(os.urandom(32)).decode()
        assert ss.describe_weakness(secret, name="k") is None


class TestTheBarRejectsTypedPassphrases:
    @pytest.mark.parametrize(
        "secret",
        [
            "changeme",
            "terrapod",
            "hunter2",
            "password",
            "a" * 32,
            "passwordpasswordpassword",
            "MyTerrapodMasterKey2026",
            "correct-horse-battery-staple",
        ],
    )
    def test_a_passphrase_is_refused(self, secret):
        problem = ss.describe_weakness(secret, name="encryption.static_key")
        assert problem is not None
        # The message has to be actionable in a pod log.
        assert "encryption.static_key" in problem
        assert "openssl rand" in problem

    def test_a_long_repeated_string_is_not_rescued_by_its_length(self):
        # `a` * 32 clears the length floor, so only the score can catch it.
        assert len("a" * 32) >= ss.MINIMUM_LENGTH
        assert ss.describe_weakness("a" * 32, name="k") is not None

    def test_empty_is_refused(self):
        assert ss.describe_weakness("", name="k") is not None


class TestTheDsnIsNotCaughtByScoring:
    """The finding that shaped the design: entropy cannot detect this one."""

    def test_the_shipped_default_dsn_scores_as_strong(self):
        dsn = "postgresql+asyncpg://terrapod:terrapod@localhost:5432/terrapod"
        # It is long and structured, so a strength estimator likes it. If this
        # ever starts failing, the fallback check must STILL not be rewritten to
        # depend on scoring — the DSN is weak because it is shared with every
        # database client, not because it is guessable.
        assert ss.describe_weakness(dsn, name="k") is None
        assert ss.estimate_bits(dsn) > ss.MINIMUM_BITS


class TestTheSigningKeyReportsTheFallback:
    def _reset(self):
        from terrapod.auth import token_signing

        token_signing._reset_cache_for_tests()

    def test_an_unset_key_is_reported_although_the_dsn_scores_well(self):
        from terrapod.auth.token_signing import report_key_strength

        self._reset()
        problem = report_key_strength("", strict=False)
        assert problem is not None
        assert "database URL" in problem

    def test_a_generated_key_is_not_reported(self):
        from terrapod.auth.token_signing import report_key_strength

        self._reset()
        strong = base64.b64encode(os.urandom(32)).decode()
        assert report_key_strength(strong, strict=False) is None

    def test_a_weak_configured_key_is_reported(self):
        from terrapod.auth.token_signing import report_key_strength

        self._reset()
        assert report_key_strength("changeme", strict=False) is not None

    def test_strict_mode_raises_on_the_fallback(self):
        from terrapod.auth.token_signing import report_key_strength

        self._reset()
        with pytest.raises(ValueError, match="database URL"):
            report_key_strength("", strict=True)

    def test_strict_mode_raises_on_a_weak_key(self):
        from terrapod.auth.token_signing import report_key_strength

        self._reset()
        with pytest.raises(ValueError):
            report_key_strength("changeme", strict=True)


class TestTheFallbackStillDerivesTheSameKey:
    """The whole reason the weak path is reported rather than removed: changing
    the derivation would invalidate every token in flight, which on a patch means
    killing running plans and applies."""

    def test_an_unset_key_still_derives_from_the_database_url(self):
        import hashlib

        from terrapod.auth import token_signing

        token_signing._reset_cache_for_tests()
        with patch("terrapod.config.settings") as st:
            st.token_signing_key = ""
            st.database_url = "postgresql+asyncpg://u:p@h:5432/d"
            st.require_strong_secrets = False
            key = token_signing.get_token_signing_key()
        token_signing._reset_cache_for_tests()
        assert key == hashlib.sha256(b"postgresql+asyncpg://u:p@h:5432/d").digest()

    def test_strict_mode_refuses_to_derive_at_all(self):
        from terrapod.auth import token_signing

        token_signing._reset_cache_for_tests()
        with patch("terrapod.config.settings") as st:
            st.token_signing_key = ""
            st.database_url = "postgresql+asyncpg://u:p@h:5432/d"
            st.require_strong_secrets = True
            with pytest.raises(ValueError):
                token_signing.get_token_signing_key()
        token_signing._reset_cache_for_tests()


class TestTheStaticKekReportsItsMasterSecret:
    def test_a_passphrase_is_refused_under_strict(self):
        from terrapod.crypto.providers import StaticKEKProvider

        with pytest.raises(ValueError, match="encryption.static_key"):
            StaticKEKProvider("correct-horse-battery-staple", strict=True)

    def test_a_passphrase_only_warns_by_default(self):
        # Default-off is deliberate: a deployment already running on a passphrase
        # has its data encrypted under it, so refusing to boot on a patch upgrade
        # is worse than the weakness.
        from terrapod.crypto.providers import StaticKEKProvider

        provider = StaticKEKProvider("correct-horse-battery-staple", strict=False)
        assert provider.id == "static"

    def test_generated_material_is_accepted_under_strict(self):
        from terrapod.crypto.providers import StaticKEKProvider

        secret = base64.b64encode(os.urandom(32)).decode()
        assert StaticKEKProvider(secret, strict=True).id == "static"

    async def test_the_derivation_is_unchanged_so_old_wraps_still_open(self):
        """Salting or stretching would make every existing DEK unrecoverable."""
        import hashlib

        from terrapod.crypto.providers import StaticKEKProvider

        secret = base64.b64encode(os.urandom(32)).decode()
        provider = StaticKEKProvider(secret, strict=True)
        assert provider._kek == hashlib.sha256(secret.encode("utf-8")).digest()

        dek = os.urandom(32)
        assert await provider.unwrap(await provider.wrap(dek)) == dek

    def test_an_empty_master_secret_is_still_refused(self):
        from terrapod.crypto.providers import StaticKEKProvider

        with pytest.raises(ValueError):
            StaticKEKProvider("", strict=False)


class TestAWeakSecretStillWorksWhenNotStrict:
    """The patch-safety property, asserted rather than assumed.

    A deployment already running on a weak signing key must keep working across
    the upgrade that adds this check. If it did not, the fix for an advisory
    about guessable keys would itself be an outage — and worse, the tokens it
    stopped minting are the ones running plans and applies depend on.
    """

    def test_a_weak_configured_key_still_derives_a_usable_key(self):
        import hashlib

        from terrapod.auth import token_signing

        token_signing._reset_cache_for_tests()
        with patch("terrapod.config.settings") as st:
            st.token_signing_key = "changeme"
            st.database_url = "postgresql+asyncpg://u:p@h:5432/d"
            st.require_strong_secrets = False
            key = token_signing.get_token_signing_key()
        token_signing._reset_cache_for_tests()
        assert key == hashlib.sha256(b"changeme").digest()

    def test_a_runner_token_still_round_trips_on_the_fallback(self):
        # The end that matters: an in-flight run's token must still verify.
        import uuid

        from terrapod.auth import runner_tokens, token_signing

        token_signing._reset_cache_for_tests()
        run_id = uuid.uuid4()
        with patch("terrapod.config.settings") as st:
            st.token_signing_key = ""
            st.database_url = "postgresql+asyncpg://u:p@h:5432/d"
            st.require_strong_secrets = False
            st.runners.token_ttl_seconds = 3600
            st.runners.max_token_ttl_seconds = 7200
            token = runner_tokens.generate_runner_token(run_id)
            assert runner_tokens.verify_runner_token(token) == str(run_id)
        token_signing._reset_cache_for_tests()
