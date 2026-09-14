"""The Pulumi service's secrets are byte-safe (#1573).

The CLI encrypts binary values as well as text. Sealing a value that was not
valid UTF-8 used to fail with a 500 `UnicodeEncodeError`, so every `pulumi up`
opened with failed `encrypt` calls and `change-secrets-provider default` broke.
Decrypt could not hand back bytes that were not text either.

These use the real encryption service, on and off, rather than a stand-in:
the failure was in the envelope layer, which a fake would not have reached.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.crypto import envelope
from terrapod.crypto.service import EncryptionService

pytestmark = pytest.mark.asyncio

MOD = "terrapod.api.routers.pulumi_service"

VALUES = [
    pytest.param(b"hunter2", id="text"),
    pytest.param("pässwörd ✓".encode(), id="non-ascii-text"),
    pytest.param(b"\xff\xfe\x00\x80", id="invalid-utf8"),
    pytest.param(bytes(range(256)), id="every-byte"),
    pytest.param(os.urandom(64), id="random"),
    pytest.param(b"", id="empty"),
]

MODES = pytest.mark.parametrize("enabled", [True, False], ids=["encryption-on", "encryption-off"])


def _svc(*, enabled: bool) -> EncryptionService:
    svc = EncryptionService()
    if enabled:
        svc.enabled = True
        svc._deks = {1: envelope.new_dek()}
        svc._active_version = 1
    return svc


def _request(body: dict) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    return request


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


async def _call(fn, body: dict, svc: EncryptionService) -> dict:
    with (
        patch("terrapod.crypto.service.get_encryption", return_value=svc),
        patch(f"{MOD}._authorized_stack", AsyncMock(return_value=MagicMock(id=uuid.uuid4()))),
    ):
        return await fn(
            "default", "proj", "dev", _request(body), MagicMock(email="a@b.c"), AsyncMock()
        )


async def _seal(raw: bytes, svc: EncryptionService) -> str:
    from terrapod.api.routers.pulumi_service import encrypt_secret

    return (await _call(encrypt_secret, {"plaintext": _b64(raw)}, svc))["ciphertext"]


@MODES
class TestAnyValueRoundTrips:
    @pytest.mark.parametrize("raw", VALUES)
    async def test_through_decrypt(self, enabled: bool, raw: bytes) -> None:
        from terrapod.api.routers.pulumi_service import decrypt_secret

        svc = _svc(enabled=enabled)
        ciphertext = await _seal(raw, svc)
        back = await _call(decrypt_secret, {"ciphertext": ciphertext}, svc)
        assert base64.b64decode(back["plaintext"]) == raw

    @pytest.mark.parametrize("raw", VALUES)
    async def test_through_batch_decrypt(self, enabled: bool, raw: bytes) -> None:
        from terrapod.api.routers.pulumi_service import batch_decrypt

        svc = _svc(enabled=enabled)
        ciphertext = await _seal(raw, svc)
        out = await _call(batch_decrypt, {"ciphertexts": [ciphertext]}, svc)
        assert base64.b64decode(out["plaintexts"][ciphertext]) == raw

    async def test_a_batch_of_mixed_values(self, enabled: bool) -> None:
        from terrapod.api.routers.pulumi_service import batch_decrypt

        svc = _svc(enabled=enabled)
        values = [b"text", b"\xff\x00", os.urandom(32)]
        ciphertexts = [await _seal(v, svc) for v in values]
        out = await _call(batch_decrypt, {"ciphertexts": ciphertexts}, svc)
        assert [base64.b64decode(out["plaintexts"][c]) for c in ciphertexts] == values

    async def test_the_envelope_only_ever_sees_ascii(self, enabled: bool) -> None:
        svc = _svc(enabled=enabled)
        seen: list[str] = []
        real = svc.encrypt
        svc.encrypt = lambda p: seen.append(p) or real(p)  # type: ignore[method-assign]
        await _seal(b"\xff\x00\x80", svc)
        assert seen
        assert all(s.isascii() for s in seen)


class TestEncryptionOnHidesTheValue:
    async def test_the_ciphertext_does_not_carry_the_value(self) -> None:
        raw = b"a-recognisable-secret-value"
        ciphertext = base64.b64decode(await _seal(raw, _svc(enabled=True)))
        assert raw not in ciphertext
        assert base64.b64encode(raw) not in ciphertext


@MODES
class TestValuesSealedBeforeThisStillOpen:
    """Every secret written before #1573 was text, sealed as text."""

    @pytest.mark.parametrize("text", ["hunter2", "pässwörd ✓", ""])
    async def test_an_old_ciphertext_decrypts_to_the_same_text(
        self, enabled: bool, text: str
    ) -> None:
        from terrapod.api.routers.pulumi_service import batch_decrypt, decrypt_secret

        svc = _svc(enabled=enabled)
        # Exactly what `encrypt` produced before #1573.
        old = base64.b64encode(svc.encrypt(text).encode()).decode()

        back = await _call(decrypt_secret, {"ciphertext": old}, svc)
        assert base64.b64decode(back["plaintext"]) == text.encode()
        batch = await _call(batch_decrypt, {"ciphertexts": [old]}, svc)
        assert base64.b64decode(batch["plaintexts"][old]) == text.encode()
