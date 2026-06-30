"""Unit tests for app-layer encryption at rest (#553): envelope + static KEK + service."""

import pytest
from cryptography.exceptions import InvalidTag

from terrapod.crypto import envelope
from terrapod.crypto.providers import StaticKEKProvider
from terrapod.crypto.service import EncryptionService

# ── Envelope (local AES-GCM data path) ────────────────────────────────────────


def test_envelope_round_trip():
    dek = envelope.new_dek()
    env = envelope.encrypt("s3cr3t-value", dek, 1)
    assert envelope.is_encrypted(env)
    assert env.startswith("tpenc:1:1:")
    assert envelope.parse_version(env) == 1
    assert envelope.decrypt(env, dek) == "s3cr3t-value"


def test_is_encrypted_rejects_plaintext():
    assert not envelope.is_encrypted("just-a-plain-token")
    assert not envelope.is_encrypted("")


def test_decrypt_wrong_key_fails():
    dek = envelope.new_dek()
    env = envelope.encrypt("x", dek, 1)
    with pytest.raises(InvalidTag):
        envelope.decrypt(env, envelope.new_dek())  # different DEK → GCM tag fail


def test_decrypt_tampered_ciphertext_fails():
    dek = envelope.new_dek()
    env = envelope.encrypt("x", dek, 1)
    tampered = env[:-2] + ("AA" if not env.endswith("AA") else "BB")
    with pytest.raises(InvalidTag):
        envelope.decrypt(tampered, dek)


def test_new_dek_is_32_bytes():
    assert len(envelope.new_dek()) == envelope.DEK_LEN == 32


# ── Static KEK provider ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_static_provider_wrap_unwrap():
    p = StaticKEKProvider("a-strong-master-passphrase")
    dek = envelope.new_dek()
    wrapped = await p.wrap(dek)
    assert wrapped != dek.hex()  # opaque, not the raw key
    assert await p.unwrap(wrapped) == dek


@pytest.mark.asyncio
async def test_static_provider_wrong_master_cannot_unwrap():
    dek = envelope.new_dek()
    wrapped = await StaticKEKProvider("master-A").wrap(dek)
    with pytest.raises(InvalidTag):
        await StaticKEKProvider("master-B").unwrap(wrapped)


def test_static_provider_requires_key():
    with pytest.raises(ValueError):
        StaticKEKProvider("")


# ── EncryptionService (encrypt/decrypt + passthrough + canary semantics) ──────


def _enabled_service() -> EncryptionService:
    svc = EncryptionService()
    svc.enabled = True
    dek = envelope.new_dek()
    svc._deks = {1: dek}
    svc._active_version = 1
    return svc


def test_service_disabled_is_passthrough():
    svc = EncryptionService()  # disabled by default
    assert svc.encrypt("plain") == "plain"
    assert svc.decrypt("plain") == "plain"


def test_service_enabled_round_trip():
    svc = _enabled_service()
    enc = svc.encrypt("token-123")
    assert envelope.is_encrypted(enc)
    assert svc.decrypt(enc) == "token-123"


def test_service_decrypts_legacy_plaintext_passthrough():
    # An enabled service must still read un-prefixed legacy rows unchanged.
    svc = _enabled_service()
    assert svc.decrypt("legacy-plaintext") == "legacy-plaintext"


def test_service_decrypt_unknown_version_fails_loud():
    svc = _enabled_service()
    # Envelope says version 9, which isn't in the cache → must raise, never
    # return ciphertext as if it were plaintext.
    foreign = envelope.encrypt("x", envelope.new_dek(), 9)
    with pytest.raises(RuntimeError):
        svc.decrypt(foreign)


def test_service_disabled_can_still_decrypt_loaded_versions():
    # Disabled-but-keys-loaded (mid disable migration): encrypt passthrough,
    # but marked values still decrypt.
    svc = _enabled_service()
    enc = svc.encrypt("v")
    svc.enabled = False
    assert svc.encrypt("new") == "new"  # no longer encrypts
    assert svc.decrypt(enc) == "v"  # still reads old ciphertext
