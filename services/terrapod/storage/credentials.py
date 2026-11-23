"""Federated credentials for a storage location (#2590).

A location configured `credentials.type: federated` does not use the API pod's own
workload identity. It presents a token this deployment's OIDC issuer minted for
that location (`auth/storage_identity.py`) and lets the target cloud exchange it
for the cloud's own short-lived credentials. That is what crosses a partition --
registering an issuer in the target partition's account rather than a different
code path -- and what reaches another cloud, which role assumption cannot do at
all.

**All three exchanges take the token FROM MEMORY.** This was the one open question
worth measuring before building, because the alternative is writing a JWT to disk
per location, which on the API pod is a PVC-tempfile question (architecture rule
14) and a credential at rest besides. Measured against the installed SDKs:

* botocore's `AssumeRoleWithWebIdentityCredentialFetcher` takes a
  `web_identity_token_loader` -- any callable returning the token string -- and
  builds its own UNSIGNED STS client, so the exchange needs no credentials of its
  own;
* `azure.identity.aio.ClientAssertionCredential(tenant, client, func)` takes the
  assertion as a callable;
* `gcloud.aio.auth.Token` resolves an external-account subject token through
  `_get_subject_token`, which a subclass can answer from memory, and
  `google.auth.identity_pool.Credentials` takes a `SubjectTokenSupplier`.

So no location ever writes a token anywhere.

**Every credential here is REFRESHABLE, and the token is minted per refresh.** The
issuer's tokens are deliberately short-lived and its signing key rotates, so a
loader that captured one token would work until the first rotation and then fail in
the cloud. Each loader below calls the mint, so a refresh after a rotation signs
with the current key.

**A failure here makes one LOCATION unreachable, never the pod.** The exchange
happens inside the SDK on the first operation, so it surfaces through
`location_resolver`'s per-location reachability probe: the location is recorded
unreachable with the reason, the gauge drops, an error is logged once, and every
other location and every non-storage route goes on serving (#2621). The
configurations that cannot work at all -- a federated location with no audience, no
role, or an audience a run token also carries -- are refused in `config.py` instead,
where an operator meets them at `helm upgrade` rather than at first upload.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

from terrapod.config import StorageBackend, StorageLocationConfig, settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: The scope Google's STS exchange and the impersonation both ask for. Not
#: configurable: `gcloud.aio.auth.Token` refuses to impersonate with no scope, and
#: a narrower storage-only scope cannot sign a URL through IAM.
_GCP_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

#: Google's own subject-token type for a JWT, and the impersonation endpoint
#: template its SDKs use.
_GCP_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
_GCP_IMPERSONATION_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken"
)


@dataclass(frozen=True)
class FederatedLocation:
    """Everything one location needs to obtain its own cloud credentials.

    Flattened off the config at startup rather than carried as the config object,
    so a store holds a value it can act on and nothing reaches back into
    `settings` on a credential refresh -- a refresh runs inside an SDK, on
    whatever thread or task it chose, long after the location was built.
    """

    name: str
    backend: StorageBackend
    audience: str
    session_duration_seconds: int
    aws_role_arn: str = ""
    aws_role_session_name: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    gcp_token_url: str = ""
    #: `gcs.service_account_email`, not a credential field: the exchange
    #: impersonates the account that signs this location's URLs, and they have to
    #: be the same account, so there is one field rather than two that can
    #: disagree.
    gcp_service_account_email: str = ""

    def token(self) -> str:
        """Mint a fresh token for this location. Called on every credential refresh."""
        from terrapod.auth.storage_identity import mint_storage_identity_token

        return mint_storage_identity_token(
            self.name,
            audience=self.audience,
            # The CLOUD session's length is `session_duration_seconds`; the token
            # that buys it needs only to survive the exchange, so it keeps the
            # issuer's own short TTL. Conflating the two would publish a
            # long-lived bearer token to buy a long-lived session.
            ttl_seconds=settings.auth.oidc_issuer.token_ttl_seconds,
        )


def federated_location(name: str, cfg: StorageLocationConfig) -> FederatedLocation | None:
    """A location's federated identity, or None when it uses the pod's own.

    One producer, called from `_build_cloud_store`, so no store decides for itself
    what `credentials.type` means.
    """
    creds = cfg.credentials
    if creds.type != "federated":
        return None
    return FederatedLocation(
        name=name,
        backend=cfg.backend,
        audience=creds.audience.strip(),
        session_duration_seconds=creds.session_duration_seconds,
        aws_role_arn=creds.aws_role_arn.strip(),
        aws_role_session_name=creds.aws_role_session_name.strip(),
        azure_tenant_id=creds.azure_tenant_id.strip(),
        azure_client_id=creds.azure_client_id.strip(),
        gcp_token_url=creds.gcp_token_url.strip(),
        gcp_service_account_email=cfg.gcs.service_account_email.strip(),
    )


# ---------------------------------------------------------------- AWS


class _StaticCredentialProvider:
    """Hands botocore's resolver one credentials object we built ourselves.

    botocore's own `assume-role-with-web-identity` provider reads a role ARN and a
    token FILE out of the shared config, so it cannot be pointed at a token in
    memory; this is the documented extension point instead
    (`CredentialResolver.insert_before`). Inserted before `env` -- the front of the
    chain -- because a federated location must not silently fall back to the pod's
    ambient identity: that identity usually CAN reach something, so a fallback
    would write a workspace's objects to the wrong account rather than failing.
    """

    #: `insert_before` matches on METHOD; CANONICAL_NAME is what botocore reports
    #: in its own diagnostics.
    METHOD = "terrapod-federated-storage"
    CANONICAL_NAME = "TerrapodFederatedStorage"

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials

    async def load(self) -> Any:
        return self._credentials


def install_aws_credentials(session: Any, fed: FederatedLocation) -> None:
    """Point an aiobotocore session at this location's federated credentials.

    `AioDeferredRefreshableCredentials` means nothing happens until the first
    operation and the exchange is redone before expiry, so a location configured
    but never used costs no STS call and a long-lived process does not have to be
    restarted when its session ends.
    """
    from aiobotocore.credentials import (
        AioAssumeRoleWithWebIdentityCredentialFetcher,
        AioDeferredRefreshableCredentials,
    )

    fetcher = AioAssumeRoleWithWebIdentityCredentialFetcher(
        client_creator=session.create_client,
        # The callable is the whole point: botocore calls it per exchange, so each
        # refresh mints a token signed by whatever key is current.
        web_identity_token_loader=fed.token,
        role_arn=fed.aws_role_arn,
        extra_args={
            "RoleSessionName": fed.aws_role_session_name,
            "DurationSeconds": fed.session_duration_seconds,
        },
    )
    credentials = AioDeferredRefreshableCredentials(
        refresh_using=fetcher.fetch_credentials,
        method="assume-role-with-web-identity",
    )
    session.get_component("credential_provider").insert_before(
        "env", _StaticCredentialProvider(credentials)
    )
    logger.info(
        "Storage location federates to AWS",
        location=fed.name,
        role_arn=fed.aws_role_arn,
        session_duration_seconds=fed.session_duration_seconds,
    )


# ---------------------------------------------------------------- Azure


def azure_credential(fed: FederatedLocation) -> Any:
    """An async Azure credential that presents this location's token as a client assertion.

    `ClientAssertionCredential` is Azure's own name for exactly this flow -- the
    app registration holds a federated identity credential naming our issuer,
    subject and audience, and the SDK posts our JWT where a client secret would go.
    Azure fixes the resulting token's lifetime at about an hour and ignores any
    duration we ask for, which is why `config.py` refuses a longer
    `session_duration_seconds` for an azure location rather than accepting one it
    cannot deliver.
    """
    from azure.identity.aio import ClientAssertionCredential

    logger.info(
        "Storage location federates to Azure",
        location=fed.name,
        tenant_id=fed.azure_tenant_id,
        client_id=fed.azure_client_id,
    )
    return ClientAssertionCredential(
        tenant_id=fed.azure_tenant_id,
        client_id=fed.azure_client_id,
        func=fed.token,
    )


# ---------------------------------------------------------------- GCP


def _gcp_external_account_config(fed: FederatedLocation) -> dict:
    """The external-account document both Google clients are configured from.

    One producer for the async and the sync client, because the audience, the
    subject-token type, the STS endpoint and the impersonated account all have to
    match or the two halves of one location authenticate as different principals.

    `credential_source` names a type neither client can resolve, deliberately: the
    async side overrides `_get_subject_token` and the sync side supplies a
    `SubjectTokenSupplier`, so anything reaching the stock resolution has lost the
    in-memory token and should fail loudly rather than read a file nobody wrote.
    """
    cfg: dict[str, Any] = {
        "type": "external_account",
        "audience": fed.audience,
        "subject_token_type": _GCP_SUBJECT_TOKEN_TYPE,
        "token_url": fed.gcp_token_url,
        "credential_source": {"type": "terrapod-in-memory"},
    }
    if fed.gcp_service_account_email:
        cfg["service_account_impersonation_url"] = _GCP_IMPERSONATION_URL.format(
            sa=fed.gcp_service_account_email
        )
    return cfg


def gcs_aio_token(fed: FederatedLocation) -> Any:
    """A `gcloud.aio.auth.Token` whose subject token comes from the mint.

    Subclassed rather than configured: `_get_subject_token` supports a file, a URL
    and an environment variable, and a token in memory is none of those. Everything
    below it -- the STS exchange, the optional impersonation, the refresh clock --
    is the library's own and is left alone.
    """
    from gcloud.aio.auth import Token

    config = _gcp_external_account_config(fed)

    class _InMemorySubjectToken(Token):  # type: ignore[misc]
        async def _get_subject_token(self, credential_source: dict, timeout: int) -> str:  # noqa: ARG002
            return fed.token()

    logger.info(
        "Storage location federates to Google",
        location=fed.name,
        audience=fed.audience,
        service_account=fed.gcp_service_account_email or None,
    )
    return _InMemorySubjectToken(
        service_file=io.StringIO(json.dumps(config)),
        scopes=list(_GCP_SCOPES),
    )


def gcs_sync_credentials(fed: FederatedLocation) -> Any:
    """google-auth credentials for the SIGNING client.

    Separate from the async token because signing a URL is a different operation
    from reading an object: `generate_signed_url` goes through IAM's signBlob as a
    SERVICE ACCOUNT, which a federated identity is not, so these credentials
    impersonate the account named on the location and the signed URL is that
    account's. `config.py` requires `gcs.service_account_email` on a federated gcs
    location for this reason.
    """
    from google.auth import identity_pool

    config = _gcp_external_account_config(fed)

    class _Supplier(identity_pool.SubjectTokenSupplier):  # type: ignore[misc]
        def get_subject_token(self, context, request) -> str:  # noqa: ANN001, ARG002
            return fed.token()

    return identity_pool.Credentials(
        audience=config["audience"],
        subject_token_type=config["subject_token_type"],
        token_url=config["token_url"],
        subject_token_supplier=_Supplier(),
        service_account_impersonation_url=config.get("service_account_impersonation_url"),
        scopes=list(_GCP_SCOPES),
    )
