"""Tests for GPG key revocation (#640) — real pgpy crypto, no gpg binary."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pgpy
from pgpy.constants import (
    CompressionAlgorithm,
    EllipticCurveOID,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    RevocationReason,
    SignatureType,
    SymmetricKeyAlgorithm,
)

from terrapod.gpg_verify import is_revoked
from terrapod.services import gpg_key_service


def _new_key():
    """A fresh Ed25519 signing key (private material available)."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
    key.add_uid(
        pgpy.PGPUID.new("Publisher <pub@example.com>"),
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.ZLIB],
    )
    return key


def _revocation_cert(key) -> str:
    """The armored self key-revocation certificate (gpg --gen-revoke output)."""
    rev = key.revoke(
        key,
        sigtype=SignatureType.KeyRevocation,
        reason=RevocationReason.Compromised,
        comment="test",
    )
    return str(rev)


def test_apply_revocation_produces_revoked_armor():
    key = _new_key()
    new_armor = gpg_key_service._apply_revocation_sync(str(key.pubkey), _revocation_cert(key))
    assert new_armor is not None
    reloaded, _ = pgpy.PGPKey.from_blob(new_armor)
    assert is_revoked(reloaded) is True


def test_apply_revocation_rejects_a_cert_for_a_different_key():
    stored = _new_key()
    other = _new_key()  # a revocation cert issued by a DIFFERENT key
    assert (
        gpg_key_service._apply_revocation_sync(str(stored.pubkey), _revocation_cert(other)) is None
    )


def test_apply_revocation_rejects_a_plain_signature():
    key = _new_key()
    plain_sig = str(key.sign(pgpy.PGPMessage.new("not a revocation")))
    assert gpg_key_service._apply_revocation_sync(str(key.pubkey), plain_sig) is None


async def test_revoke_gpg_key_persists_revoked_armor():
    """The service updates the stored armor to the revoked one so every verify
    path (which re-parses ascii_armor) fails closed afterward."""
    key = _new_key()
    row = MagicMock()
    row.ascii_armor = str(key.pubkey)
    row.key_id = "ABCD1234ABCD1234"

    with patch.object(gpg_key_service, "get_gpg_key", new=AsyncMock(return_value=row)):
        result = await gpg_key_service.revoke_gpg_key(
            AsyncMock(), uuid.uuid4(), _revocation_cert(key)
        )

    assert result is row
    reloaded, _ = pgpy.PGPKey.from_blob(row.ascii_armor)
    assert is_revoked(reloaded) is True


async def test_revoke_gpg_key_bad_cert_raises():
    key = _new_key()
    other = _new_key()
    row = MagicMock()
    row.ascii_armor = str(key.pubkey)

    with patch.object(gpg_key_service, "get_gpg_key", new=AsyncMock(return_value=row)):
        try:
            await gpg_key_service.revoke_gpg_key(AsyncMock(), uuid.uuid4(), _revocation_cert(other))
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


async def test_revoke_gpg_key_missing_returns_none():
    with patch.object(gpg_key_service, "get_gpg_key", new=AsyncMock(return_value=None)):
        assert await gpg_key_service.revoke_gpg_key(AsyncMock(), uuid.uuid4(), "x") is None
