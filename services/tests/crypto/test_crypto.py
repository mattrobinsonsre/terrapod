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


# ── Durability safety net (#553 Phase 2) ──────────────────────────────────────


def test_status_reports_decryptable():
    svc = _enabled_service()
    svc._provider_id = "static"
    st = svc.status()
    assert st["enabled"] is True
    assert st["provider"] == "static"
    assert st["active_version"] == 1
    assert st["dek_versions"] == [1]
    assert st["decryptable"] is True


def test_status_disabled_is_decryptable():
    svc = EncryptionService()  # disabled
    st = svc.status()
    assert st["enabled"] is False
    assert st["decryptable"] is True  # nothing encrypted → nothing at risk


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Minimal AsyncSession stand-in for init/verify_live tests."""

    def __init__(self, rows):
        self._rows = rows
        self.added = []

    async def execute(self, _stmt):
        return _FakeResult(list(self._rows))

    def add(self, obj):
        self.added.append(obj)
        self._rows.append(obj)

    async def commit(self):
        pass


class _GoodProvider:
    id = "static"

    def __init__(self):
        self._dek = envelope.new_dek()

    async def wrap(self, dek):
        self._dek = dek
        return "wrapped"

    async def unwrap(self, wrapped):
        return self._dek


class _BrokenUnwrapProvider:
    """Wraps fine but unwrap returns the WRONG key — the dangerous case."""

    id = "static"

    async def wrap(self, dek):
        return "wrapped"

    async def unwrap(self, wrapped):
        return envelope.new_dek()  # never equals the wrapped DEK


@pytest.mark.asyncio
async def test_enable_aborts_if_provider_cannot_unwrap(monkeypatch):
    """Death-by-encryption guard: enabling must abort (and write nothing) when
    the provider can't round-trip — never encrypt under an unusable key."""
    from terrapod.crypto import service as svc_mod

    monkeypatch.setattr(svc_mod.settings.encryption, "enabled", True)
    monkeypatch.setattr(svc_mod, "build_provider", lambda: _BrokenUnwrapProvider())
    db = _FakeDB(rows=[])
    with pytest.raises(RuntimeError, match="round-trip"):
        await svc_mod.init_encryption(db)
    assert db.added == []  # no DEK row was ever persisted


@pytest.mark.asyncio
async def test_enable_succeeds_with_good_provider(monkeypatch):
    from terrapod.crypto import service as svc_mod

    monkeypatch.setattr(svc_mod.settings.encryption, "enabled", True)
    monkeypatch.setattr(svc_mod, "build_provider", lambda: _GoodProvider())
    db = _FakeDB(rows=[])
    await svc_mod.init_encryption(db)
    svc = svc_mod.get_encryption()
    assert svc.enabled is True
    assert svc.status()["decryptable"] is True
    svc_mod.reset_encryption_for_tests()


@pytest.mark.asyncio
async def test_verify_live_ok_and_failure(monkeypatch):
    from terrapod.crypto import service as svc_mod
    from terrapod.db.models import CryptoKey

    good = _GoodProvider()
    dek = envelope.new_dek()
    good._dek = dek
    canary = envelope.encrypt(svc_mod._CANARY, dek, 1)
    row = CryptoKey(version=1, wrapped_dek="wrapped", provider="static", canary=canary, active=True)

    monkeypatch.setattr(svc_mod, "build_provider", lambda: good)
    res = await svc_mod.verify_live(_FakeDB(rows=[row]))
    assert res["ok"] is True
    assert res["checked_versions"] == [1]

    # A provider that returns the wrong key → canary won't decrypt → not ok.
    monkeypatch.setattr(svc_mod, "build_provider", lambda: _BrokenUnwrapProvider())
    res2 = await svc_mod.verify_live(_FakeDB(rows=[row]))
    assert res2["ok"] is False
    assert res2["failures"]
