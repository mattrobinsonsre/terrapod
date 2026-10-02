"""SAML assertion validation.

Every assertion here is real and really signed (see `saml_fixtures`), and every
test drives `SAMLConnector.handle_callback` end to end. That is deliberate: all
five of the checks below live inside python3-saml's validation, so a test built
on a mocked `OneLogin_Saml2_Auth` would pass against the unfixed connector just
as happily as against the fixed one and prove nothing at all.

Each check is pinned twice — strict refuses the bad assertion, strict still
accepts the good one — because a check that refuses everything is not a fix.
And each switch is pinned in both positions, because the permissive position is
what the 1.x release lines ship and what an incompatible IDP needs here.

`_config` below sets all five explicitly, so no test in this module would notice
a default reverting to `False`. The defaults are therefore pinned separately —
see `TestTheAssertionChecksAreStrictByDefaultOnThisLine`, which also drives one
foreign assertion through a provider configured the way an operator who set
nothing gets it.
"""

from __future__ import annotations

import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from onelogin.saml2.constants import OneLogin_Saml2_Constants as K
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from terrapod.auth.connectors import saml as saml_module
from terrapod.auth.connectors.saml import SAMLConnector
from terrapod.config import SAMLProviderConfig

from .saml_fixtures import (
    ACS_URL,
    OTHER_SP_ACS_URL,
    SP_ENTITY_ID,
    idp_metadata,
    make_idp_keypair,
    saml_response,
)

REQUEST_ID = "ONELOGIN_request1"


@pytest.fixture(scope="module")
def idp_keys() -> tuple[str, str]:
    # Module-scoped: generating an RSA key per test dominates the runtime and
    # the key is not what any of these tests are about.
    return make_idp_keypair()


class _ReplayStore:
    """The one Redis behaviour the replay check depends on: SET NX EX.

    Shared between connector instances in a test, which is how it models the
    real thing — the point of putting this in Redis rather than in process
    memory is that a second API replica sees the first one's record.
    """

    def __init__(self) -> None:
        self.entries: dict[str, float] = {}

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool | None:
        now = time.time()
        live = self.entries.get(key)
        if live is not None and live <= now:
            del self.entries[key]
            live = None
        if nx and live is not None:
            return None
        self.entries[key] = now + ex
        return True


def _config(**overrides) -> SAMLProviderConfig:
    base = {
        "name": "idp",
        "metadata_url": "https://idp.test/metadata",
        "entity_id": SP_ENTITY_ID,
        "validate_destination": True,
        "validate_in_response_to": True,
        "reject_replayed_assertions": True,
        "want_assertions_signed": True,
        "reject_deprecated_algorithm": True,
    }
    base.update(overrides)
    return SAMLProviderConfig(**base)


def _connector(cert_pem: str, **overrides) -> SAMLConnector:
    connector = SAMLConnector(_config(**overrides))
    connector._idp_metadata = idp_metadata(cert_pem)
    return connector


async def _login(connector: SAMLConnector, response_b64: str, *, request_id=REQUEST_ID, store=None):
    with patch.object(saml_module, "get_redis_client", return_value=store or _ReplayStore()):
        return await connector.handle_callback(
            callback_url=ACS_URL,
            saml_response=response_b64,
            relay_state="relay",
            expected_request_id=request_id,
        )


class TestAGoodAssertionIsStillAccepted:
    """The control. Every strict check on, a well-formed assertion, a login."""

    async def test_strict_everything_accepts_a_well_formed_assertion(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        identity = await _login(_connector(cert), response)
        assert identity.email == "user@example.com"
        assert identity.display_name == "Test User"


class TestDestinationIsChecked:
    """An assertion the IDP minted for somebody else's service provider.

    This is the one that makes the others matter. python3-saml reconstructs the
    current URL from `http_host`, and the connector passed an empty one — so the
    reconstruction was the literal `https://`, which every https Destination
    starts with and every https Recipient contains. Both checks ran. Both passed.
    """

    async def test_an_assertion_addressed_to_another_sp_is_refused(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(
            key, cert, destination=OTHER_SP_ACS_URL, recipient=OTHER_SP_ACS_URL
        )
        with pytest.raises(ValueError) as exc:
            await _login(_connector(cert), response)
        # The CAUSE, not just the wrapper. `"validation failed"` alone passes for
        # any refusal at all, so it would keep passing if the destination check
        # stopped running and something unrelated refused the assertion instead —
        # which is what happens if `http_host` goes back to being empty while an
        # absolute ACS URL stays in the settings.
        message = str(exc.value)
        assert "instead of" in message, message
        assert OTHER_SP_ACS_URL in message, message

    async def test_a_foreign_recipient_alone_is_refused(self, idp_keys):
        # Destination right, Recipient wrong: the assertion was issued for
        # another SP and merely posted at us.
        key, cert = idp_keys
        response, _ = saml_response(key, cert, destination=ACS_URL, recipient=OTHER_SP_ACS_URL)
        with pytest.raises(ValueError):
            await _login(_connector(cert), response)

    async def test_our_own_acs_url_is_accepted(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, destination=ACS_URL, recipient=ACS_URL)
        identity = await _login(_connector(cert), response)
        assert identity.email == "user@example.com"

    async def test_switched_off_the_foreign_assertion_is_accepted_again(self, idp_keys):
        """What the 1.x release lines ship, and what this must not change."""
        key, cert = idp_keys
        response, _ = saml_response(
            key, cert, destination=OTHER_SP_ACS_URL, recipient=OTHER_SP_ACS_URL
        )
        identity = await _login(_connector(cert, validate_destination=False), response)
        assert identity.email == "user@example.com"

    async def test_no_absolute_acs_url_refuses_rather_than_waving_through(self, idp_keys):
        """Fail closed, and say which keys to set.

        A relative ACS URL cannot be checked against. Treating that as "no
        opinion" would silently restore the vulnerability on exactly the
        deployments that have not configured their external URL.
        """
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        connector = _connector(cert)
        with pytest.raises(ValueError) as exc:
            with patch.object(saml_module, "get_redis_client", return_value=_ReplayStore()):
                await connector.handle_callback(
                    callback_url="/api/terrapod/v1/auth/saml/acs",
                    saml_response=response,
                    relay_state="relay",
                    expected_request_id=REQUEST_ID,
                )
        assert "callback_base_url" in str(exc.value)

    async def test_an_absolute_acs_url_was_already_required_either_way(self, idp_keys):
        """So the strict default asks for no configuration SAML did not already need.

        python3-saml refuses to build settings at all from a relative
        `assertionConsumerService.url` (`sp_acs_url_invalid`), with the check off
        just as much as on. Every deployment where SAML works today therefore
        already has the absolute URL the Destination check is made against —
        which is what makes turning this on by default safe.
        """
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        connector = _connector(cert, validate_destination=False)
        with pytest.raises(Exception) as exc:  # noqa: B017 — the library's own error type
            with patch.object(saml_module, "get_redis_client", return_value=_ReplayStore()):
                await connector.handle_callback(
                    callback_url="/api/terrapod/v1/auth/saml/acs",
                    saml_response=response,
                    relay_state="relay",
                    expected_request_id=REQUEST_ID,
                )
        assert "sp_acs_url_invalid" in str(exc.value)


class TestInResponseTo:
    """The assertion has to answer the request this login actually sent."""

    async def test_an_assertion_answering_a_different_request_is_refused(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, in_response_to="ONELOGIN_somebody_elses")
        with pytest.raises(ValueError):
            await _login(_connector(cert), response)

    async def test_an_assertion_with_no_in_response_to_is_refused(self, idp_keys):
        """python3-saml skips its own check when the attribute is absent.

        `if in_response_to is not None and request_id is not None` — so an
        unsolicited assertion sails past the library untouched, and the
        presence requirement has to be ours.
        """
        key, cert = idp_keys
        response, _ = saml_response(key, cert, in_response_to="", response_in_response_to=None)
        with pytest.raises(ValueError) as exc:
            await _login(_connector(cert), response)
        assert "InResponseTo" in str(exc.value)

    async def test_an_in_response_to_only_in_the_assertion_is_refused(self, idp_keys):
        """Signed only at the assertion level, the Response attribute is free.

        An attacker holding an assertion whose signature covers the assertion
        alone can rewrite the Response element around it, so dropping
        InResponseTo there costs them nothing — unless we require it.
        """
        key, cert = idp_keys
        response, _ = saml_response(
            key, cert, in_response_to=REQUEST_ID, response_in_response_to=None
        )
        with pytest.raises(ValueError) as exc:
            await _login(_connector(cert), response)
        assert "InResponseTo" in str(exc.value)

    async def test_the_matching_assertion_is_accepted(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, in_response_to=REQUEST_ID)
        identity = await _login(_connector(cert), response)
        assert identity.email == "user@example.com"

    async def test_a_login_with_no_recorded_request_id_is_refused(self, idp_keys):
        """Nothing to match against is not the same as a match."""
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        with pytest.raises(ValueError) as exc:
            await _login(_connector(cert), response, request_id=None)
        assert "AuthnRequest id" in str(exc.value)

    async def test_switched_off_a_mismatched_request_is_accepted_again(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, in_response_to="ONELOGIN_somebody_elses")
        identity = await _login(_connector(cert, validate_in_response_to=False), response)
        assert identity.email == "user@example.com"

    async def test_switched_off_an_unsolicited_assertion_is_accepted_again(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, in_response_to="", response_in_response_to=None)
        identity = await _login(
            _connector(cert, validate_in_response_to=False), response, request_id=None
        )
        assert identity.email == "user@example.com"


class TestReplay:
    """One assertion, one login."""

    async def test_the_same_assertion_cannot_be_used_twice(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        connector = _connector(cert)
        store = _ReplayStore()

        first = await _login(connector, response, store=store)
        assert first.email == "user@example.com"

        with pytest.raises(ValueError) as exc:
            await _login(connector, response, store=store)
        assert "already been used" in str(exc.value)

    async def test_a_second_replica_refuses_it_too(self, idp_keys):
        """The record is in Redis precisely so this holds.

        An in-process set would let the captured assertion through once per API
        pod, which under several replicas is not replay protection.
        """
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        store = _ReplayStore()

        await _login(_connector(cert), response, store=store)
        with pytest.raises(ValueError):
            await _login(_connector(cert), response, store=store)

    async def test_two_different_assertions_both_log_in(self, idp_keys):
        key, cert = idp_keys
        store = _ReplayStore()
        connector = _connector(cert)
        for _ in range(2):
            response, _ = saml_response(key, cert)
            identity = await _login(connector, response, store=store)
            assert identity.email == "user@example.com"

    async def test_switched_off_the_replay_is_accepted_again(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert)
        connector = _connector(cert, reject_replayed_assertions=False)
        store = _ReplayStore()
        await _login(connector, response, store=store)
        identity = await _login(connector, response, store=store)
        assert identity.email == "user@example.com"
        assert store.entries == {}, "nothing should be recorded when the check is off"

    async def test_the_record_outlives_the_assertion_window(self, idp_keys):
        """A record that expires first would reopen the window it closes."""
        key, cert = idp_keys
        response, _ = saml_response(key, cert, lifetime=4000)
        store = _ReplayStore()
        await _login(_connector(cert), response, store=store)
        (expiry,) = store.entries.values()
        assert expiry - time.time() > 3600

    async def test_a_short_window_still_gets_a_floor(self, idp_keys):
        """Clock skew between us and the IDP must not shrink the record away."""
        key, cert = idp_keys
        response, _ = saml_response(key, cert, lifetime=1)
        store = _ReplayStore()
        await _login(_connector(cert), response, store=store)
        (expiry,) = store.entries.values()
        assert expiry - time.time() >= saml_module.REPLAY_TTL_FLOOR_SECONDS - 1

    def test_the_key_is_namespaced_per_provider(self, idp_keys):
        """Assertion ids are only unique within one IDP."""
        _, cert = idp_keys
        a = _connector(cert, name="idp-a")._replay_key("_shared")
        b = _connector(cert, name="idp-b")._replay_key("_shared")
        assert a != b
        assert a.startswith(saml_module.ASSERTION_REPLAY_PREFIX)

    def test_the_assertion_id_is_not_embedded_verbatim(self, idp_keys):
        """It is an opaque, unbounded, IDP-supplied string."""
        _, cert = idp_keys
        key = _connector(cert)._replay_key("_id:with:colons" + "x" * 500)
        assert "x" * 500 not in key
        assert len(key) < 120


class TestSignatureRequirements:
    async def test_an_unsigned_assertion_inside_a_signed_response_is_refused(self, idp_keys):
        """A message-level signature leaves the assertion itself unprotected."""
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_assertion=False, sign_response=True)
        with pytest.raises(ValueError):
            await _login(_connector(cert), response)

    async def test_a_signed_assertion_is_accepted(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_assertion=True, sign_response=False)
        identity = await _login(_connector(cert), response)
        assert identity.email == "user@example.com"

    async def test_switched_off_a_message_only_signature_is_accepted_again(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_assertion=False, sign_response=True)
        identity = await _login(_connector(cert, want_assertions_signed=False), response)
        assert identity.email == "user@example.com"

    async def test_a_sha1_signature_is_refused(self, idp_keys):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_algorithm=K.RSA_SHA1, digest_algorithm=K.SHA1)
        with pytest.raises(ValueError):
            await _login(_connector(cert), response)

    async def test_switched_off_a_sha1_signature_is_accepted_again(self, idp_keys):
        """Separate from want_assertions_signed on purpose.

        An IDP stuck on SHA-1 and an IDP that signs only the message are
        different problems; one knob would make an operator give up both
        protections to solve either.
        """
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_algorithm=K.RSA_SHA1, digest_algorithm=K.SHA1)
        identity = await _login(_connector(cert, reject_deprecated_algorithm=False), response)
        assert identity.email == "user@example.com"

    async def test_sha1_is_still_refused_when_only_the_signing_requirement_is_relaxed(
        self, idp_keys
    ):
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_algorithm=K.RSA_SHA1, digest_algorithm=K.SHA1)
        with pytest.raises(ValueError):
            await _login(_connector(cert, want_assertions_signed=False), response)

    async def test_an_entirely_unsigned_response_was_always_refused(self, idp_keys):
        """Stated so the scope of the signing defect is not overclaimed."""
        key, cert = idp_keys
        response, _ = saml_response(key, cert, sign_assertion=False, sign_response=False)
        with pytest.raises(ValueError):
            await _login(_connector(cert, want_assertions_signed=False), response)


class TestTheAuthnRequestIdIsCarried:
    """Without this there is nothing for `InResponseTo` to be checked against."""

    async def test_build_authorization_request_returns_the_request_id(self, idp_keys):
        _, cert = idp_keys
        request = await _connector(cert).build_authorization_request(
            callback_url=ACS_URL, state="relay-state"
        )
        assert request.request_id
        assert request.state == "relay-state"

        # The id we store must be the id we actually sent, so decode the
        # AuthnRequest out of the redirect rather than trusting the accessor.
        query = parse_qs(urlsplit(request.authorize_url).query)
        authn_request = OneLogin_Saml2_Utils.decode_base64_and_inflate(query["SAMLRequest"][0])
        assert f'ID="{request.request_id}"'.encode() in authn_request

    async def test_each_login_gets_its_own_request_id(self, idp_keys):
        _, cert = idp_keys
        connector = _connector(cert)
        a = await connector.build_authorization_request(callback_url=ACS_URL, state="s1")
        b = await connector.build_authorization_request(callback_url=ACS_URL, state="s2")
        assert a.request_id != b.request_id

    async def test_the_request_id_round_trips_into_a_matching_assertion(self, idp_keys):
        """The whole loop: issue a request, answer it, be let in."""
        key, cert = idp_keys
        connector = _connector(cert)
        request = await connector.build_authorization_request(
            callback_url=ACS_URL, state="relay-state"
        )
        response, _ = saml_response(key, cert, in_response_to=request.request_id)
        identity = await _login(connector, response, request_id=request.request_id)
        assert identity.email == "user@example.com"


class TestTheAssertionChecksAreStrictByDefaultOnThisLine:
    """One implementation, two defaults — and this line is the strict one.

    All five are on by default here, which is the 2.0 posture
    (GHSA-hgx9-xwfp-5qcr): a deployment that configures nothing is protected,
    and an operator whose IDP cannot satisfy one check relaxes that one rather
    than inheriting a permissive default nobody chose. The 1.x release lines
    default all five OFF, because a patch release must not change what a running
    deployment does.

    This is pinned rather than left to the field declarations because the
    failure is silent in the dangerous direction: a flag that quietly reverts to
    `False` leaves every strict-path test below still passing — each one passes
    its own `True` explicitly — while an unconfigured deployment accepts an
    assertion minted for somebody else's service provider. Nothing else looks
    wrong. A carry from a 1.x branch is the likely way it happens.
    """

    #: field name -> the compatibility problem that justifies turning it off
    STRICT = {
        "validate_destination": "an IDP whose Destination differs from the registered URL",
        "validate_in_response_to": "an IDP that does not echo InResponseTo",
        "reject_replayed_assertions": "a deployment that cannot rely on Redis",
        "want_assertions_signed": "an IDP that signs the message only",
        "reject_deprecated_algorithm": "an IDP still on SHA-1",
    }

    def test_all_five_default_to_true(self) -> None:
        from terrapod.config import SAMLProviderConfig

        provider = SAMLProviderConfig(name="p", metadata_url="https://idp.example.com/md")
        for field, why in self.STRICT.items():
            assert getattr(provider, field) is True, (
                f"{field} defaults to False on this line. 2.0 is where these flip "
                "to strict, so an unconfigured deployment must be protected; "
                f"relaxing it is the operator's deliberate step for {why}."
            )

    def test_the_set_is_exactly_the_five(self) -> None:
        """A sixth check must be placed deliberately, not inherited."""
        from terrapod.config import SAMLProviderConfig

        bools = {n for n, f in SAMLProviderConfig.model_fields.items() if f.annotation is bool}
        assert bools == set(self.STRICT), (
            "the boolean switches on SAMLProviderConfig changed. Decide this "
            f"line's default for each and record it here: {bools ^ set(self.STRICT)}"
        )

    def test_each_one_can_still_be_turned_off(self) -> None:
        """The relax path is what an incompatible IDP needs, so it has to work."""
        from terrapod.config import SAMLProviderConfig

        for field in self.STRICT:
            provider = SAMLProviderConfig(
                name="p", metadata_url="https://idp.example.com/md", **{field: False}
            )
            assert getattr(provider, field) is False

    async def test_an_unconfigured_provider_refuses_a_foreign_assertion(self, idp_keys) -> None:
        """The property the defaults exist for, driven end to end.

        `_config` in this module sets all five explicitly, so every other test
        here would pass with the defaults reverted. This one builds the provider
        the way an operator who configured nothing gets it.
        """
        from terrapod.config import SAMLProviderConfig

        key, cert = idp_keys
        response, _ = saml_response(
            key, cert, destination=OTHER_SP_ACS_URL, recipient=OTHER_SP_ACS_URL
        )
        connector = SAMLConnector(
            SAMLProviderConfig(
                name="idp",
                metadata_url="https://idp.test/metadata",
                entity_id=SP_ENTITY_ID,
            )
        )
        connector._idp_metadata = idp_metadata(cert)
        with pytest.raises(ValueError) as exc:
            await _login(connector, response)
        # The cause, not just any refusal — `validation failed` alone would keep
        # passing if the destination check stopped running and something else
        # refused the assertion instead.
        message = str(exc.value)
        assert "instead of" in message, message
        assert OTHER_SP_ACS_URL in message, message
