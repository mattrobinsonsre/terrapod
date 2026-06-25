"""Tests for the shared S256 PKCE helper."""

from terrapod.auth.pkce import s256_challenge


def test_s256_challenge_rfc7636_vector():
    # RFC 7636 Appendix B worked example: verifier -> challenge.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert s256_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_s256_challenge_has_no_padding():
    # base64url challenge must be sent without '=' padding (RFC 7636 4.2).
    assert "=" not in s256_challenge("some-verifier-value-12345")


def test_s256_challenge_is_deterministic():
    assert s256_challenge("abc") == s256_challenge("abc")
