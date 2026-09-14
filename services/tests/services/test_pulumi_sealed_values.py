"""Sealing a Pulumi secret value byte-safely, and reading what came before (#1573).

`seal_bytes` and `open_sealed` are shared by the service surface's encrypt and
decrypt and by the agent path's `seal_secrets` and `reveal_secrets`, so a value
sealed by either side opens on the other.
"""

from __future__ import annotations

import base64

import pytest

from terrapod.crypto import envelope
from terrapod.crypto.service import EncryptionService
from terrapod.services import pulumi_state_service as pss

MODES = pytest.mark.parametrize("enabled", [True, False], ids=["encryption-on", "encryption-off"])
SIG = {pss.SECRET_SIG_KEY: pss.SECRET_SIG}
SERVICE = {"type": pss.SERVICE_PROVIDER, "state": {"url": "https://t/api/v1/pulumi"}}


def _svc(*, enabled: bool) -> EncryptionService:
    svc = EncryptionService()
    if enabled:
        svc.enabled = True
        svc._deks = {1: envelope.new_dek()}
        svc._active_version = 1
    return svc


@MODES
class TestSealAndOpen:
    @pytest.mark.parametrize("raw", [b"text", b"\xff\x00\x80", bytes(range(256)), b""])
    def test_any_value_round_trips(self, enabled: bool, raw: bytes) -> None:
        svc = _svc(enabled=enabled)
        assert pss.open_sealed(svc.decrypt, pss.seal_bytes(svc.encrypt, raw)) == raw

    def test_the_marker_sits_outside_the_envelope(self, enabled: bool) -> None:
        svc = _svc(enabled=enabled)
        sealed = pss.seal_bytes(svc.encrypt, b"\xff")
        assert sealed.startswith(pss.BYTES_PREFIX)
        assert envelope.is_encrypted(sealed[len(pss.BYTES_PREFIX) :]) is enabled

    def test_a_value_sealed_the_old_way_opens(self, enabled: bool) -> None:
        svc = _svc(enabled=enabled)
        assert pss.open_sealed(svc.decrypt, svc.encrypt("pässwörd")) == "pässwörd".encode()


def _deployment(ciphertext: str) -> dict:
    return {
        "secrets_providers": SERVICE,
        "resources": [{"outputs": {"pw": {**SIG, "ciphertext": ciphertext}}}],
    }


@MODES
class TestTheAgentPathReadsBothForms:
    def test_old_and_new_ciphertexts_both_reveal(self, enabled: bool) -> None:
        svc = _svc(enabled=enabled)
        value = '"hunter2"'
        old = base64.b64encode(svc.encrypt(value).encode()).decode()
        new = base64.b64encode(pss.seal_bytes(svc.encrypt, value.encode()).encode()).decode()
        for ciphertext in (old, new):
            opened = pss.reveal_secrets(_deployment(ciphertext), svc.decrypt)
            assert opened["resources"][0]["outputs"]["pw"]["plaintext"] == value

    def test_what_the_agent_seals_is_byte_safe_and_reveals(self, enabled: bool) -> None:
        svc = _svc(enabled=enabled)
        opened = {"resources": [{"outputs": {"pw": {**SIG, "plaintext": '"x"'}}}]}
        sealed = pss.seal_secrets(opened, svc.encrypt, SERVICE)
        ciphertext = sealed["resources"][0]["outputs"]["pw"]["ciphertext"]
        assert base64.b64decode(ciphertext).decode().startswith(pss.BYTES_PREFIX)
        again = pss.reveal_secrets(sealed, svc.decrypt)
        assert again["resources"][0]["outputs"]["pw"]["plaintext"] == '"x"'
