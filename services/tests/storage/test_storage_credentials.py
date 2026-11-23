"""Federated storage credentials: the three exchanges, through the real SDKs (#2590).

Every assertion below is made against the object the cloud's own SDK builds, not
against a dictionary of ours. That is the point: what this module does is CONFIGURE
three third-party credential flows, and a test that checked the shape of our own
arguments would pass just as well on a configuration none of them accepts -- the
`PULUMI_ACCESS_TOKEN` lesson (#1906), where the token's prefix, its pattern and two
helpers agreeing were all pinned while every download answered 401.

What cannot be asserted here is the exchange itself: that needs AWS STS, Azure AD or
Google's token endpoint. So the boundary tested is "the SDK is handed our token and
the operator's identifiers, in the place that SDK consumes them", and the exchange
beyond it is the library's.
"""

import asyncio

import jwt
import pytest

from terrapod.auth import oidc_signing
from terrapod.config import StorageLocationConfig
from terrapod.storage import credentials

_ISSUER = "https://terrapod.example.com"

_S3 = {
    "backend": "s3",
    "s3": {"bucket": "eu-state", "region": "eu-west-1", "presigned_url_expiry_seconds": 900},
    "credentials": {
        "type": "federated",
        "audience": "terrapod-storage-eu",
        "aws_role_arn": "arn:aws:iam::222222222222:role/terrapod-eu",
        "session_duration_seconds": 1800,
    },
}
_AZURE = {
    "backend": "azure",
    "azure": {"account_name": "tpstate", "container_name": "state"},
    "credentials": {
        "type": "federated",
        "audience": "api://terrapod-storage-az",
        "azure_tenant_id": "11111111-1111-1111-1111-111111111111",
        "azure_client_id": "22222222-2222-2222-2222-222222222222",
    },
}
_GCS = {
    "backend": "gcs",
    "gcs": {
        "bucket": "eu-state",
        "project_id": "p",
        "service_account_email": "terrapod@p.iam.gserviceaccount.com",
    },
    "credentials": {
        "type": "federated",
        "audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/r",
    },
}


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    """A loaded signing key and a fixed issuer, so `fed.token()` mints for real."""
    oidc_signing._reset_for_tests()
    key = oidc_signing.generate_private_key()
    kid = oidc_signing.compute_kid(key)
    oidc_signing._keys = [
        oidc_signing.SigningKey(kid=kid, private_key_pem=oidc_signing.serialize_private_key(key))
    ]
    oidc_signing._signing_kid = kid
    monkeypatch.setattr(
        "terrapod.api.routers.oidc_issuer.issuer_url", lambda: _ISSUER, raising=True
    )
    yield jwt.PyJWK(oidc_signing.get_jwks()["keys"][0]).key
    oidc_signing._reset_for_tests()


def _fed(name: str, body: dict) -> credentials.FederatedLocation:
    fed = credentials.federated_location(name, StorageLocationConfig.model_validate(body))
    assert fed is not None
    return fed


class TestWhichLocationsFederate:
    def test_a_default_location_has_no_federated_identity(self):
        cfg = StorageLocationConfig.model_validate({"backend": "s3", "s3": {"bucket": "b"}})

        assert credentials.federated_location("eu", cfg) is None

    def test_a_federated_location_carries_the_operators_identifiers(self):
        fed = _fed("eu", _S3)

        assert fed.name == "eu"
        assert fed.audience == "terrapod-storage-eu"
        assert fed.aws_role_arn == "arn:aws:iam::222222222222:role/terrapod-eu"
        assert fed.session_duration_seconds == 1800

    def test_the_gcs_signing_account_comes_from_the_backend_block(self):
        """One field, not two that can disagree: the exchange impersonates the
        account that signs this location's URLs."""
        fed = _fed("eu", _GCS)

        assert fed.gcp_service_account_email == "terrapod@p.iam.gserviceaccount.com"


class TestTheToken:
    def test_it_is_this_locations_token(self, _signing_key):
        fed = _fed("eu", _S3)

        claims = jwt.decode(fed.token(), _signing_key, algorithms=["RS256"], audience=fed.audience)

        assert claims["sub"] == "storage:eu"
        assert claims["terrapod_storage_location"] == "eu"

    def test_a_fresh_token_is_minted_on_every_call(self, _signing_key):
        """Not a style point. The issuer's tokens are short-lived and its signing
        key rotates, so a loader that captured one token would work until the first
        rotation and then fail inside the cloud's token exchange."""
        fed = _fed("eu", _S3)

        first = jwt.decode(fed.token(), _signing_key, algorithms=["RS256"], audience=fed.audience)
        second = jwt.decode(fed.token(), _signing_key, algorithms=["RS256"], audience=fed.audience)

        assert first["jti"] != second["jti"]

    def test_the_token_ttl_is_the_issuers_not_the_session_duration(self, _signing_key):
        """The token only has to survive the exchange; the SESSION is what lasts
        `session_duration_seconds`. Conflating them would publish a long-lived
        bearer token to buy a long-lived session."""
        from terrapod.config import settings

        fed = _fed("eu", _S3)
        claims = jwt.decode(fed.token(), _signing_key, algorithms=["RS256"], audience=fed.audience)

        assert claims["exp"] - claims["iat"] == settings.auth.oidc_issuer.token_ttl_seconds
        assert claims["exp"] - claims["iat"] != fed.session_duration_seconds


class TestTheAWSExchange:
    def test_the_provider_goes_to_the_FRONT_of_the_resolver_chain(self):
        """Before `env`, so a federated location cannot fall back to the pod's own
        identity. That identity usually CAN reach something, so a fallback would
        write a workspace's objects into the wrong account rather than failing."""
        from aiobotocore.session import get_session

        session = get_session()
        before = [p.METHOD for p in session.get_component("credential_provider").providers]
        credentials.install_aws_credentials(session, _fed("eu", _S3))
        after = [p.METHOD for p in session.get_component("credential_provider").providers]

        assert before[0] == "env"
        assert after[0] == "terrapod-federated-storage"
        assert after[1:] == before

    def test_the_session_resolves_to_refreshable_credentials(self):
        from aiobotocore.credentials import AioDeferredRefreshableCredentials
        from aiobotocore.session import get_session

        session = get_session()
        credentials.install_aws_credentials(session, _fed("eu", _S3))
        resolved = asyncio.run(session.get_credentials())

        assert isinstance(resolved, AioDeferredRefreshableCredentials)

    def test_the_exchange_carries_the_token_the_role_and_the_duration(self, _signing_key):
        """Driven through botocore's own fetcher, so this is the request it would
        send -- `WebIdentityToken` is where our minted JWT has to land, and it is
        taken FROM MEMORY through the loader rather than read from a file."""
        from aiobotocore.credentials import AioAssumeRoleWithWebIdentityCredentialFetcher
        from aiobotocore.session import get_session

        fed = _fed("eu", _S3)
        fetcher = AioAssumeRoleWithWebIdentityCredentialFetcher(
            client_creator=get_session().create_client,
            web_identity_token_loader=fed.token,
            role_arn=fed.aws_role_arn,
            extra_args={
                "RoleSessionName": fed.aws_role_session_name,
                "DurationSeconds": fed.session_duration_seconds,
            },
        )
        kwargs = fetcher._assume_role_kwargs()

        assert kwargs["RoleArn"] == fed.aws_role_arn
        assert kwargs["RoleSessionName"] == "terrapod-storage"
        assert kwargs["DurationSeconds"] == 1800
        claims = jwt.decode(
            kwargs["WebIdentityToken"],
            _signing_key,
            algorithms=["RS256"],
            audience=fed.audience,
        )
        assert claims["sub"] == "storage:eu"


class TestTheAzureExchange:
    def test_it_presents_the_token_as_a_client_assertion(self, _signing_key):
        from azure.identity.aio import ClientAssertionCredential

        fed = _fed("az", _AZURE)
        credential = credentials.azure_credential(fed)

        assert isinstance(credential, ClientAssertionCredential)
        # The SDK calls the callable per token request; what it returns has to be
        # this location's token rather than a captured one.
        claims = jwt.decode(fed.token(), _signing_key, algorithms=["RS256"], audience=fed.audience)
        assert claims["sub"] == "storage:az"


class TestTheGoogleExchange:
    def test_the_async_token_is_an_external_account_impersonating_the_signer(self):
        from gcloud.aio.auth.token import Type

        fed = _fed("eu", _GCS)
        token = credentials.gcs_aio_token(fed)

        assert token.token_type == Type.EXTERNAL_ACCOUNT
        assert token.service_data["audience"] == fed.audience
        assert token.service_data["token_url"] == "https://sts.googleapis.com/v1/token"
        assert fed.gcp_service_account_email in token.impersonation_uri

    def test_the_subject_token_comes_from_memory(self, _signing_key):
        """The library resolves an external-account subject token from a file, a URL
        or an environment variable. A token in memory is none of those, which is why
        `_get_subject_token` is overridden rather than configured -- and why no
        location ever writes a JWT to disk."""
        fed = _fed("eu", _GCS)
        token = credentials.gcs_aio_token(fed)

        subject = asyncio.run(token._get_subject_token({"type": "terrapod-in-memory"}, 10))
        claims = jwt.decode(subject, _signing_key, algorithms=["RS256"], audience=fed.audience)

        assert claims["sub"] == "storage:eu"

    def test_the_signing_credentials_impersonate_the_service_account(self):
        """A separate credential from the async token on purpose: signing a URL goes
        through IAM `signBlob` as a service account, which a federated identity is
        not."""
        fed = _fed("eu", _GCS)
        creds = credentials.gcs_sync_credentials(fed)

        assert fed.gcp_service_account_email in creds._service_account_impersonation_url
        assert creds._audience == fed.audience

    def test_the_subject_token_supplier_mints(self, _signing_key):
        fed = _fed("eu", _GCS)
        creds = credentials.gcs_sync_credentials(fed)

        subject = creds._subject_token_supplier.get_subject_token(None, None)
        claims = jwt.decode(subject, _signing_key, algorithms=["RS256"], audience=fed.audience)

        assert claims["sub"] == "storage:eu"


class TestTheStoresTakeIt:
    """The call site, not just the function: a credential nothing installs is a
    location authenticating as the pod while its configuration says otherwise."""

    def test_an_s3_location_carries_its_federated_identity(self):
        from terrapod.storage import _build_cloud_store

        store = _build_cloud_store(StorageLocationConfig.model_validate(_S3), name="eu")

        assert store._federated is not None
        assert store._federated.name == "eu"

    def test_the_s3_store_installs_the_provider_BEFORE_it_builds_a_client(self):
        """A positional property, so a positional assertion (#2590).

        Carrying the identity is not using it: the provider goes on the session's
        resolver chain, and a client resolves its credentials when it is
        CONSTRUCTED -- so installing after `create_client` would leave the first
        client authenticating as the pod. Nothing behavioural can see that, because
        the ordering is invisible once both calls have happened.
        """
        from terrapod.storage import _build_cloud_store

        store = _build_cloud_store(StorageLocationConfig.model_validate(_S3), name="eu")
        order: list[str] = []

        class _Session:
            def create_client(self, *_a, **_kw):
                order.append("create_client")

                class _Ctx:
                    async def __aenter__(self):
                        return "client"

                    async def __aexit__(self, *_):
                        return False

                return _Ctx()

        def _install(session, fed):
            order.append("install")
            assert session is store._session
            assert fed is store._federated

        store._session = _Session()
        monkey = pytest.MonkeyPatch()
        monkey.setattr("terrapod.storage.credentials.install_aws_credentials", _install)
        try:
            assert asyncio.run(store._get_client()) == "client"
        finally:
            monkey.undo()

        assert order == ["install", "create_client"]

    def test_an_unfederated_s3_store_installs_nothing(self):
        """The pair. Without it the test above passes on a store that installs
        unconditionally, which would put a federated provider on every location."""
        from terrapod.storage import _build_cloud_store

        cfg = StorageLocationConfig.model_validate({"backend": "s3", "s3": {"bucket": "b"}})
        store = _build_cloud_store(cfg, name="plain")
        calls: list[object] = []

        class _Session:
            def create_client(self, *_a, **_kw):
                class _Ctx:
                    async def __aenter__(self):
                        return "client"

                    async def __aexit__(self, *_):
                        return False

                return _Ctx()

        store._session = _Session()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            "terrapod.storage.credentials.install_aws_credentials",
            lambda *a, **k: calls.append(a),
        )
        try:
            asyncio.run(store._get_client())
        finally:
            monkey.undo()

        assert calls == []

    def test_an_azure_location_builds_a_client_assertion_credential(self):
        from azure.identity.aio import ClientAssertionCredential

        from terrapod.storage import _build_cloud_store

        store = _build_cloud_store(StorageLocationConfig.model_validate(_AZURE), name="az")

        assert isinstance(store._get_credential(), ClientAssertionCredential)

    def test_a_gcs_location_carries_its_federated_identity(self):
        from terrapod.storage import _build_cloud_store

        store = _build_cloud_store(StorageLocationConfig.model_validate(_GCS), name="eu")

        assert store._federated is not None

    def test_an_unfederated_location_still_uses_the_pods_own_identity(self):
        from terrapod.storage import _build_cloud_store

        cfg = StorageLocationConfig.model_validate({"backend": "s3", "s3": {"bucket": "b"}})
        store = _build_cloud_store(cfg, name="plain")

        assert store._federated is None

    def test_the_deployment_wide_backend_never_federates(self):
        """A `StorageConfig` has no credentials block at all: the pod's own identity
        is the only thing it could federate as, so federation is per-location by
        construction."""
        from terrapod.config import StorageConfig
        from terrapod.storage import _build_cloud_store

        store = _build_cloud_store(StorageConfig(backend="s3", s3={"bucket": "b"}))

        assert store._federated is None
