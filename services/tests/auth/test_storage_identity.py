"""A storage location's identity token, and that it is not a run's (#2590).

The property this file exists for is the one the issue calls the thing to write a
test against before writing the minting code: **a token minted for a storage
location must not be presentable as a run identity token, nor the reverse.** Both
come from the same issuer and are signed by the same key, deliberately, so the
discriminator is the audience -- and the tests below check it the way a cloud
would, by decoding with the other side's audience through the same library every
cloud's verifier is built on, rather than by comparing our own dictionaries.

The configuration half of that guarantee is in
`tests/config/test_storage_federated_config.py`: `Settings` refuses a deployment in
which a location's audience is also a run audience, or in which two locations share
one. Without it the discriminator would be the operator's to get right.
"""

import jwt
import pytest

from terrapod.auth import oidc_signing, storage_identity

_ISSUER = "https://terrapod.example.com"
_STORAGE_AUDIENCE = "terrapod-storage-eu"
_RUN_AUDIENCE = "sts.amazonaws.com"


@pytest.fixture(autouse=True)
def _reset():
    oidc_signing._reset_for_tests()
    yield
    oidc_signing._reset_for_tests()


@pytest.fixture
def published_key():
    """A loaded signing key, returning the PUBLIC half as a cloud would fetch it."""
    key = oidc_signing.generate_private_key()
    kid = oidc_signing.compute_kid(key)
    oidc_signing._keys = [
        oidc_signing.SigningKey(kid=kid, private_key_pem=oidc_signing.serialize_private_key(key))
    ]
    oidc_signing._signing_kid = kid
    return jwt.PyJWK(oidc_signing.get_jwks()["keys"][0]).key


def _run_token(**overrides) -> str:
    """A run identity token in the shape `api/routers/cloud_identity.py` mints.

    Hand-built rather than driven through that route, which needs a run, a
    workspace, a pool and a runner token. What matters here is the CLAIM SET, and
    `test_cloud_identity.py` is what pins that the route produces this shape.
    """
    claims = {
        "iss": _ISSUER,
        "sub": "workspace:dns@0199f0f0-0000-7000-8000-000000000000",
        "aud": [_RUN_AUDIENCE],
        "workspace": "dns",
        "workspace_id": "0199f0f0-0000-7000-8000-000000000000",
        "run_id": "0199f0f0-0000-7000-8000-000000000001",
        "phase": "apply",
        "terrapod_organization": "default",
    }
    claims.update(overrides)
    return oidc_signing.sign_identity_token(claims, ttl_seconds=900)


def _storage_token(location: str = "eu", audience: str = _STORAGE_AUDIENCE) -> str:
    claims = storage_identity.storage_identity_claims(location, audience=audience, issuer=_ISSUER)
    return oidc_signing.sign_identity_token(claims, ttl_seconds=900)


class TestTheClaimShape:
    def test_the_subject_names_the_location_and_nothing_else(self):
        claims = storage_identity.storage_identity_claims(
            "eu", audience=_STORAGE_AUDIENCE, issuer=_ISSUER
        )

        assert claims["sub"] == "storage:eu"
        assert claims["terrapod_storage_location"] == "eu"
        assert claims["aud"] == [_STORAGE_AUDIENCE]
        assert claims["iss"] == _ISSUER

    def test_it_carries_no_claim_a_run_policy_could_bind(self):
        """Derived from the mint, not from a second copy of the claim set.

        Each name in `RUN_ONLY_CLAIMS` is something a cloud-side policy may
        condition on, so a storage token carrying one could satisfy a policy
        written for a run -- a trust policy keyed on `phase: apply` would then
        accept a token the API mints for itself on every retention sweep.
        """
        claims = storage_identity.storage_identity_claims(
            "eu", audience=_STORAGE_AUDIENCE, issuer=_ISSUER
        )

        assert set(claims) & storage_identity.RUN_ONLY_CLAIMS == set()

    def test_the_audience_is_a_single_element_list(self):
        """A token audienced for several targets is replayable between them, and
        AWS refuses a multi-valued `aud` outright."""
        claims = storage_identity.storage_identity_claims(
            "eu", audience=_STORAGE_AUDIENCE, issuer=_ISSUER
        )

        assert claims["aud"] == [_STORAGE_AUDIENCE]

    def test_the_two_subject_prefixes_cannot_collide(self):
        """A location name cannot forge a workspace subject, or the reverse.

        The storage-location grammar is `[a-z0-9-]{1,32}`, which admits no colon,
        so `storage:<name>` can never produce a `workspace:` subject however the
        location is named -- and a workspace name cannot begin a storage subject
        for the same reason.
        """
        for name in ("eu", "workspace", "a" * 32):
            assert storage_identity.storage_subject(name).startswith(
                storage_identity.STORAGE_SUBJECT_PREFIX
            )
            assert not storage_identity.storage_subject(name).startswith("workspace:")


class TestNeitherTokenIsPresentableAsTheOther:
    """The property the issue asks for, checked through the real verifier.

    `jwt.decode` with an `audience` is what every cloud's OIDC verification does,
    so a refusal here is the refusal the cloud would give. Asserting on our own
    claim dictionaries instead would prove the two dicts differ and nothing about
    whether one is accepted where the other belongs.
    """

    def test_a_storage_token_is_refused_for_a_run_audience(self, published_key):
        token = _storage_token()

        with pytest.raises(jwt.InvalidAudienceError):
            jwt.decode(token, published_key, algorithms=["RS256"], audience=_RUN_AUDIENCE)

    def test_a_run_token_is_refused_for_a_storage_audience(self, published_key):
        token = _run_token()

        with pytest.raises(jwt.InvalidAudienceError):
            jwt.decode(token, published_key, algorithms=["RS256"], audience=_STORAGE_AUDIENCE)

    def test_each_is_accepted_for_its_own_audience(self, published_key):
        """The pair. Without it both refusals above would pass on a token that
        verifies for nothing at all."""
        storage = jwt.decode(
            _storage_token(), published_key, algorithms=["RS256"], audience=_STORAGE_AUDIENCE
        )
        run = jwt.decode(_run_token(), published_key, algorithms=["RS256"], audience=_RUN_AUDIENCE)

        assert storage["sub"] == "storage:eu"
        assert run["sub"].startswith("workspace:")

    def test_two_locations_do_not_accept_each_others_tokens(self, published_key):
        """Why the audience is per LOCATION rather than per deployment: a trust
        policy can then be scoped to one location's bucket."""
        eu = _storage_token("eu", "terrapod-storage-eu")

        with pytest.raises(jwt.InvalidAudienceError):
            jwt.decode(eu, published_key, algorithms=["RS256"], audience="terrapod-storage-us")


class TestTheMint:
    def test_it_signs_with_the_current_key_and_the_claims_round_trip(
        self, published_key, monkeypatch
    ):
        monkeypatch.setattr(
            "terrapod.api.routers.oidc_issuer.issuer_url", lambda: _ISSUER, raising=True
        )

        token = storage_identity.mint_storage_identity_token(
            "eu", audience=_STORAGE_AUDIENCE, ttl_seconds=300
        )
        claims = jwt.decode(token, published_key, algorithms=["RS256"], audience=_STORAGE_AUDIENCE)

        assert claims["sub"] == "storage:eu"
        assert claims["iss"] == _ISSUER
        assert claims["exp"] - claims["iat"] == 300
        assert jwt.get_unverified_header(token)["alg"] == "RS256"

    def test_minting_with_no_loaded_key_raises(self, monkeypatch):
        """The honest outcome: a federated location with no issuer key cannot
        authenticate, and the caller is a credential refresh inside an SDK -- so
        this surfaces as that location being unreachable rather than as a
        silently unauthenticated request."""
        monkeypatch.setattr(
            "terrapod.api.routers.oidc_issuer.issuer_url", lambda: _ISSUER, raising=True
        )

        with pytest.raises(RuntimeError, match="not initialised"):
            storage_identity.mint_storage_identity_token(
                "eu", audience=_STORAGE_AUDIENCE, ttl_seconds=300
            )
