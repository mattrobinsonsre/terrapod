"""Tests for the auth router's PKCE verification.

The auth router shares the same timing-safe S256 derivation
(``terrapod.auth.pkce.s256_challenge`` + ``hmac.compare_digest``) as the
oauth router, rather than carrying its own inline comparison.
"""

from terrapod.api.routers import auth
from terrapod.api.routers.auth import _verify_pkce
from terrapod.auth.pkce import s256_challenge


class TestVerifyPKCE:
    def test_valid_s256_challenge(self):
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        code_challenge = s256_challenge(code_verifier)

        assert _verify_pkce(code_verifier, code_challenge, "S256") is True

    def test_invalid_verifier(self):
        code_verifier = "correct-verifier"
        code_challenge = s256_challenge(code_verifier)

        assert _verify_pkce("wrong-verifier", code_challenge, "S256") is False

    def test_unsupported_method(self):
        # plain method is rejected outright, even when the challenge matches.
        assert _verify_pkce("verifier", "verifier", "plain") is False

    def test_uses_shared_helper(self):
        # The router must derive the challenge via the shared helper so the
        # two PKCE call sites cannot drift.
        assert auth.s256_challenge is s256_challenge
