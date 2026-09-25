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
        """What is sealed opens to the same deployment. It is not byte-for-byte
        the stored form it came from: values are re-sealed byte-safely (#1573)."""
        opened = svc.reveal_secrets(_stored(), _decrypt)
        sealed = svc.seal_secrets(opened, _encrypt, SERVICE)
        assert svc.reveal_secrets(sealed, _decrypt) == opened
        for secret in (sealed["resources"][0]["outputs"]["result"],):
            assert base64.b64decode(secret["ciphertext"]).decode().startswith(svc.BYTES_PREFIX)

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


class TestTheServiceUrlServedToAClient:
    """#1580 -- a stored `service` URL the CLI cannot reach is unrecoverable.

    Pulumi's `NewServiceSecretsManagerFromState` passes the STORED url to
    `getServiceSecretsAccount`, which looks up the credential for that exact
    url. It never compares it with the backend the CLI is logged in to, so a
    stack naming the runner's in-cluster address fails with "could not find
    access token for <url>, have you logged in?" -- and the operator cannot
    satisfy that by logging in, because the address does not exist outside the
    cluster. Hence normalising what is SERVED.
    """

    CANONICAL = "https://terrapod.example.com/api/v1/pulumi"
    STALE = "http://terrapod-api:8000/api/terrapod/v1/pulumi"

    def _deployment(self, url: str) -> dict:
        return {
            "resources": [{"urn": "urn:pulumi:dev::p::x::r"}],
            "secrets_providers": {
                "type": "service",
                "state": {
                    "url": url,
                    "owner": "default",
                    "project": "smoke",
                    "stack": "dev",
                },
            },
        }

    def test_the_in_cluster_url_a_pre_1576_agent_run_wrote_is_replaced(self):
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        out = with_canonical_service_url(self._deployment(self.STALE), self.CANONICAL)
        assert out is not None
        assert out["secrets_providers"]["state"]["url"] == self.CANONICAL

    def test_the_rest_of_the_block_and_the_deployment_are_untouched(self):
        """Only the url moves -- owner/project/stack identify the stack to the
        backend, and resources are the state itself."""
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        src = self._deployment(self.STALE)
        out = with_canonical_service_url(src, self.CANONICAL)
        assert out is not None
        state = out["secrets_providers"]["state"]
        assert state["owner"] == "default"
        assert state["project"] == "smoke"
        assert state["stack"] == "dev"
        assert out["resources"] == src["resources"]

    def test_nothing_stored_is_mutated(self):
        """Normalising on the way out must not edit the caller's document --
        the same dict is what gets re-sealed on other paths."""
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        src = self._deployment(self.STALE)
        with_canonical_service_url(src, self.CANONICAL)
        assert src["secrets_providers"]["state"]["url"] == self.STALE

    def test_an_undeclared_external_url_leaves_the_block_alone(self):
        """No `external_url` is no opinion. Overwriting from a per-request host
        could replace a reachable address with a worse guess."""
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        src = self._deployment(self.STALE)
        assert with_canonical_service_url(src, None) == src

    def test_a_passphrase_stack_is_not_touched(self):
        """Only the service provider carries a URL; a passphrase block has a
        salt, and inventing a url in it would corrupt the stack."""
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        src = {"secrets_providers": {"type": "passphrase", "state": {"salt": "s"}}}
        assert with_canonical_service_url(src, self.CANONICAL) == src

    def test_a_stack_with_no_state_stays_none(self):
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        assert with_canonical_service_url(None, self.CANONICAL) is None

    def test_a_block_already_canonical_is_returned_unchanged(self):
        from terrapod.services.pulumi_state_service import with_canonical_service_url

        src = self._deployment(self.CANONICAL)
        assert with_canonical_service_url(src, self.CANONICAL) is src
