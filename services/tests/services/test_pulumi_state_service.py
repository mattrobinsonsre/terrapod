"""Opening and sealing a Pulumi deployment's secrets for an agent run (#1576).

An agent run imports the stack into a file backend keyed by a passphrase that
exists only for the life of its Job, so it must receive the secrets in
plaintext and hands them back the same way. These are the two conversions, and
the provider block that has to travel with what is stored.
"""

from __future__ import annotations

import base64
import copy

import pytest

from terrapod.services import pulumi_state_service as svc

SIG = {svc.SECRET_SIG_KEY: svc.SECRET_SIG}


def _encrypt(value: str) -> str:
    return f"sealed({value})"


def _decrypt(value: str) -> str:
    assert value.startswith("sealed(") and value.endswith(")")
    return value[len("sealed(") : -1]


def _cipher(plaintext: str) -> dict:
    return {**SIG, "ciphertext": base64.b64encode(_encrypt(plaintext).encode()).decode()}


def _plain(plaintext: str) -> dict:
    return {**SIG, "plaintext": plaintext}


SERVICE = {"type": "service", "state": {"url": "https://tp/api/v1/pulumi", "owner": "default"}}


def _stored() -> dict:
    return {
        "manifest": {"time": "2026-09-10T00:00:00Z"},
        "secrets_providers": SERVICE,
        "resources": [
            {
                "urn": "urn:a",
                "outputs": {
                    "result": _cipher('"hunter2"'),
                    "nested": {"list": [1, _cipher('{"k":"v"}')]},
                    "open": "not secret",
                },
            }
        ],
    }


class TestReveal:
    def test_every_secret_is_opened_wherever_it_sits(self) -> None:
        out = svc.reveal_secrets(_stored(), _decrypt)
        outputs = out["resources"][0]["outputs"]
        assert outputs["result"] == _plain('"hunter2"')
        assert outputs["nested"]["list"][1] == _plain('{"k":"v"}')
        assert outputs["open"] == "not secret"

    def test_the_provider_block_is_removed(self) -> None:
        """The runner supplies its own; the stored one names Terrapod's key."""
        assert "secrets_providers" not in svc.reveal_secrets(_stored(), _decrypt)

    def test_the_stored_deployment_is_not_mutated(self) -> None:
        stored = _stored()
        before = copy.deepcopy(stored)
        svc.reveal_secrets(stored, _decrypt)
        assert stored == before

    def test_foreign_ciphertext_is_refused_by_name(self) -> None:
        stored = _stored()
        stored["secrets_providers"] = {"type": "passphrase", "state": {"salt": "v1:x"}}
        with pytest.raises(svc.UnreadableSecretsError) as exc:
            svc.reveal_secrets(stored, _decrypt)
        assert exc.value.provider == "passphrase"

    def test_a_foreign_provider_with_no_secrets_is_fine(self) -> None:
        """Nothing is sealed, so there is nothing Terrapod needs a key for."""
        stored = {
            "secrets_providers": {"type": "passphrase", "state": {}},
            "resources": [{"urn": "urn:a", "outputs": {"x": 1}}],
        }
        assert svc.reveal_secrets(stored, _decrypt)["resources"] == stored["resources"]


class TestSeal:
    def test_round_trip(self) -> None:
        opened = svc.reveal_secrets(_stored(), _decrypt)
        sealed = svc.seal_secrets(opened, _encrypt, SERVICE)
        assert sealed == _stored()

    def test_the_uploaded_provider_block_is_replaced(self) -> None:
        opened = svc.reveal_secrets(_stored(), _decrypt)
        opened["secrets_providers"] = {"type": "passphrase", "state": {"salt": "v1:runner"}}
        assert svc.seal_secrets(opened, _encrypt, SERVICE)["secrets_providers"] == SERVICE

    def test_no_plaintext_survives(self) -> None:
        sealed = svc.seal_secrets(svc.reveal_secrets(_stored(), _decrypt), _encrypt, SERVICE)
        assert "hunter2" not in repr(sealed)
        assert '"plaintext"' not in repr(sealed).replace("'", '"')

    def test_ciphertext_in_an_upload_is_refused(self) -> None:
        """Sealed under the runner's passphrase, which died with its Pod —
        storing it would destroy the secret."""
        opened = svc.reveal_secrets(_stored(), _decrypt)
        opened["resources"][0]["outputs"]["result"] = {**SIG, "ciphertext": "v1:gone"}
        with pytest.raises(svc.SealedSecretInUploadError):
            svc.seal_secrets(opened, _encrypt, SERVICE)


class TestTheProviderToStore:
    def test_an_existing_service_block_is_kept(self) -> None:
        """A local CLI reads it to find the backend that opens the secrets."""
        prior = {"type": "service", "state": {"url": "https://old/api/terrapod/v1/pulumi"}}
        assert svc.service_provider(prior, url="https://new", project="p", stack="s") is prior

    @pytest.mark.parametrize("prior", [None, {"type": "passphrase", "state": {}}])
    def test_otherwise_one_is_made_for_this_deployment(self, prior) -> None:
        made = svc.service_provider(prior, url="https://tp/api/v1/pulumi", project="p", stack="s")
        assert made == {
            "type": "service",
            "state": {
                "url": "https://tp/api/v1/pulumi",
                "owner": "default",
                "project": "p",
                "stack": "s",
            },
        }
