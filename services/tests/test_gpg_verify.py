"""Tests for the shared OpenPGP verification core (gpg_verify)."""

import inspect

from terrapod import gpg_verify


def test_gpg_verify_installs_pgpy_warning_filter():
    """gpg_verify must suppress pgpy's static UserWarning TODO banners
    (self-sigs / revocation / flags) so they don't flood the API + runner logs
    on every verify. Source-introspection guard (pytest manages warnings.filters
    per-test, so a runtime-filter check is unreliable): if the filterwarnings
    call is removed, this fails. See #640 for the revocation caveat it documents."""
    src = inspect.getsource(gpg_verify)
    assert "filterwarnings" in src, "gpg_verify must install a warnings filter for pgpy"
    assert "category=UserWarning" in src and '"pgpy"' in src, (
        "the filter must be scoped to pgpy UserWarnings, not a blanket silence"
    )


def test_parse_sha256sums_tolerates_formatting():
    out = gpg_verify.parse_sha256sums(
        "ABC123  file.zip\n"  # uppercase digest, two-space sep
        "def456  *other.bin\n"  # binary-mode marker (*) on the filename
        "\n"  # blank line
        "garbage\n"  # short line, skipped
    )
    assert out == {"file.zip": "abc123", "other.bin": "def456"}


# ── Key revocation (#640) ──────────────────────────────────────────────


def _new_signing_key():
    """A fresh Ed25519 signing key (fast to generate)."""
    import pgpy
    from pgpy.constants import (
        CompressionAlgorithm,
        EllipticCurveOID,
        HashAlgorithm,
        KeyFlags,
        PubKeyAlgorithm,
        SymmetricKeyAlgorithm,
    )

    key = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
    key.add_uid(
        pgpy.PGPUID.new("Test <test@example.com>"),
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.ZLIB],
    )
    return key


def _self_revoke(key):
    """Attach a self key-revocation signature and re-parse the public key."""
    import pgpy
    from pgpy.constants import RevocationReason, SignatureType

    rev = key.revoke(
        key,
        sigtype=SignatureType.KeyRevocation,
        reason=RevocationReason.Compromised,
        comment="test",
    )
    key |= rev
    revoked, _ = pgpy.PGPKey.from_blob(str(key.pubkey))
    return revoked


def test_is_revoked_false_for_fresh_key():
    assert gpg_verify.is_revoked(_new_signing_key().pubkey) is False


def test_is_revoked_true_for_self_revoked_key():
    assert gpg_verify.is_revoked(_self_revoke(_new_signing_key())) is True


def test_verify_detached_fails_closed_on_revoked_key():
    """A signature that verifies under a key must STOP verifying once that key
    carries a valid self-revocation (#640) — pgpy alone would still accept it."""
    key = _new_signing_key()
    msg = b"payload-to-sign"
    sig_bytes = str(key.sign(msg)).encode()

    # Sanity: verifies under the live key.
    assert gpg_verify.verify_detached(msg, sig_bytes, key.pubkey) is True

    # After self-revocation, the SAME good signature must fail closed.
    revoked = _self_revoke(key)
    assert gpg_verify.verify_detached(msg, sig_bytes, revoked) is False
