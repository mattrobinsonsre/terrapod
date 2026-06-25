"""Cloud-IAM authentication for the Redis/Valkey connection (#579).

The Redis analogue of ``db/iam_auth.py``. Opt-in via ``redis.auth_mode`` in
``{aws_iam, gcp_iam, azure_ad}``: each new connection authenticates with a
freshly-minted, short-lived token (used as the Redis ``AUTH`` password) under
the API pod's **workload identity** — so there is no static Redis auth string:

- ``aws_iam``  — AWS ElastiCache (Redis OSS 7+/Valkey) IAM auth. The token is a
  SigV4-presigned ``connect`` request (botocore ``RequestSigner``, local
  signing). The signing identifier is the **cache name** (replication-group /
  serverless cache id), not the endpoint host; the Redis user must be an
  ElastiCache User in IAM mode and the IRSA role needs ``elasticache:Connect``.
- ``gcp_iam``  — GCP Memorystore (Valkey / Redis Cluster) IAM auth: the service
  account's OAuth2 access token (google-auth ADC / Workload Identity Federation,
  ``cloud-platform`` scope).
- ``azure_ad`` — Azure Cache for Redis Microsoft Entra auth: an Entra access
  token for the ``https://redis.azure.com`` scope (azure-identity
  ``DefaultAzureCredential``); the username is the Entra principal's object id.

The token is supplied per connection via a redis-py ``CredentialProvider``.
redis-py awaits ``get_credentials_async`` on (re)connect, so we offload the
(possibly blocking) mint with ``asyncio.to_thread`` — the event loop is never
blocked (rule 13). The credential libraries cache + refresh tokens near expiry,
so steady-state is a cheap cached read. TLS is required for IAM Redis auth (use a
``rediss://`` URL).

The default ``auth_mode = "password"`` (static auth string from ``redis_url``)
is unchanged and remains fully supported; nothing here runs unless an IAM mode
is explicitly selected.
"""

from __future__ import annotations

import asyncio
import threading
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog

from redis.credentials import CredentialProvider

logger = structlog.get_logger(__name__)

_TOKEN_TTL_SECONDS = 900  # ElastiCache presigned-token validity (15 min)
_GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_AZURE_REDIS_SCOPE = "https://redis.azure.com/.default"

# One lock serialises every token mint/refresh so concurrent (re)connects can't
# race a shared credential object (google-auth Credentials.refresh mutates in
# place and is not thread-safe).
_lock = threading.Lock()
_aws_signers: dict[str, object] = {}
_gcp_state: dict[str, object] = {}
_azure_state: dict[str, object] = {}


def strip_url_credentials(redis_url: str) -> str:
    """Drop any userinfo from a redis URL.

    redis-py rejects passing both a URL password and a ``credential_provider``;
    in IAM mode the provider supplies the username + token, so we strip the
    URL's userinfo (host/port/path/query/scheme — incl. ``rediss://`` TLS —
    are preserved).
    """
    parts = urlsplit(str(redis_url))
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ── AWS ElastiCache IAM ───────────────────────────────────────────────


def _aws_signer(region: str):
    key = region or "default"
    signer = _aws_signers.get(key)
    if signer is None:
        import botocore.session
        from botocore.model import ServiceId
        from botocore.signers import RequestSigner

        session = botocore.session.get_session()
        signer = RequestSigner(
            ServiceId("elasticache"),
            region or session.get_config_variable("region"),
            "elasticache",
            "v4",
            session.get_credentials(),
            session.get_component("event_emitter"),
        )
        _aws_signers[key] = signer
    return signer


def mint_aws_elasticache_token(*, cache_name: str, user: str, region: str) -> str:
    """SigV4-presigned ElastiCache ``connect`` token (local signing, no I/O)."""
    with _lock:
        signer = _aws_signer(region)
        url = f"https://{cache_name}/?{urlencode({'Action': 'connect', 'User': user})}"
        signed = signer.generate_presigned_url(  # type: ignore[attr-defined]
            {"method": "GET", "url": url, "body": {}, "headers": {}, "context": {}},
            operation_name="connect",
            expires_in=_TOKEN_TTL_SECONDS,
            region_name=region or None,
        )
        # The IAM auth token is the presigned URL without the scheme.
        return signed.removeprefix("https://")


# ── GCP Memorystore IAM ───────────────────────────────────────────────


def mint_gcp_access_token() -> str:
    """OAuth2 access token for Memorystore IAM auth; cached + refreshed."""
    import google.auth
    import google.auth.transport.requests

    with _lock:
        creds = _gcp_state.get("creds")
        if creds is None:
            creds, _ = google.auth.default(scopes=[_GCP_SCOPE])
            _gcp_state["creds"] = creds
            _gcp_state["request"] = google.auth.transport.requests.Request()
        if not creds.valid:  # type: ignore[union-attr]
            creds.refresh(_gcp_state["request"])  # type: ignore[union-attr]
        return creds.token  # type: ignore[union-attr]


# ── Azure Cache for Redis (Entra) ─────────────────────────────────────


def mint_azure_redis_token() -> str:
    """Microsoft Entra access token for Azure Cache for Redis."""
    with _lock:
        cred = _azure_state.get("cred")
        if cred is None:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
            _azure_state["cred"] = cred
        return cred.get_token(_AZURE_REDIS_SCOPE).token  # type: ignore[union-attr]


# ── Credential provider ───────────────────────────────────────────────


class _IAMCredentialProvider(CredentialProvider):
    """redis-py credential provider that mints a per-connection IAM token.

    ``get_credentials_async`` offloads the mint to a worker thread so it never
    blocks the event loop; ``get_credentials`` (sync) is provided for the rare
    sync code path.
    """

    def __init__(self, username: str, mint) -> None:  # type: ignore[no-untyped-def]
        self._username = username
        self._mint = mint

    def get_credentials(self) -> tuple[str, str]:
        return (self._username, self._mint())

    async def get_credentials_async(self) -> tuple[str, str]:
        return (self._username, await asyncio.to_thread(self._mint))


def make_credential_provider(
    *, auth_mode: str, username: str, cache_name: str, region: str
) -> CredentialProvider:
    """Build the redis-py credential provider for the chosen IAM mode."""
    if auth_mode == "aws_iam":

        def _mint() -> str:
            return mint_aws_elasticache_token(cache_name=cache_name, user=username, region=region)

    elif auth_mode == "gcp_iam":
        _mint = mint_gcp_access_token
    elif auth_mode == "azure_ad":
        _mint = mint_azure_redis_token
    else:
        raise ValueError(f"unsupported IAM redis auth_mode: {auth_mode!r}")

    return _IAMCredentialProvider(username, _mint)
