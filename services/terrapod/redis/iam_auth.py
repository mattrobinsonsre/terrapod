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
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog

from redis.credentials import CredentialProvider

if TYPE_CHECKING:  # imported lazily at runtime — the cloud SDKs are optional
    from botocore.session import Session
    from botocore.signers import RequestSigner

logger = structlog.get_logger(__name__)

_TOKEN_TTL_SECONDS = 900  # ElastiCache presigned-token validity (15 min)
_GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_AZURE_REDIS_SCOPE = "https://redis.azure.com/.default"

# One lock per cloud, and each covers only what its SDK actually requires
# (#1510). This was a single global lock held across the whole mint, which meant
# every concurrent (re)connect serialised behind whichever call happened to be
# refreshing credentials — a refresh that, for a workload identity, is a
# blocking STS or IMDS round trip under botocore's retry policy. Since each mint
# runs on an `asyncio.to_thread` worker, a reconnect storm could tie up the
# loop's shared executor waiting on one refresh.
#
# What each SDK needs was checked rather than assumed:
#   AWS   — botocore's RefreshableCredentials takes its own `_refresh_lock` in
#           `get_frozen_credentials`, so signing is already thread-safe and
#           needs no lock from us. Ours now covers the cache only.
#   GCP   — google-auth has no internal lock and `refresh()` mutates the
#           credentials in place, so the refresh must stay inside the lock.
#   Azure — azure-identity's GetTokenMixin has no internal lock either, so
#           `get_token` stays inside the lock too.
#
# Only one auth_mode is ever active in a deployment, so splitting them is not
# about cross-cloud contention; it is so each cloud's locking says what that
# cloud requires, instead of all three inheriting a rule that describes GCP.
_aws_lock = threading.Lock()
_gcp_lock = threading.Lock()
_azure_lock = threading.Lock()

#: Per-region ``(session, signer)``. Both halves are required: see
#: ``_aws_signer`` for why the session must outlive the call that built it. They
#: are stored as one entry so the coupling is structural — holding the session
#: in a second dict would leave a mapping nothing ever reads, which is an
#: invitation to delete it and silently reintroduce #1509.
_aws_signers: dict[str, tuple[Session, RequestSigner]] = {}
_gcp_state: dict[str, object] = {}
_azure_state: dict[str, object] = {}


def strip_url_credentials(redis_url: str) -> str:
    """Drop any userinfo from a redis URL.

    In IAM mode the credential provider supplies the username + token, so we
    strip the URL's userinfo to avoid it competing with the provider. The
    scheme (incl. ``rediss://`` TLS), host, port, db-path, and query are
    preserved; IPv6 host literals stay bracketed.
    """
    parts = urlsplit(str(redis_url))
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — re-bracket so the port stays parseable
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ── AWS ElastiCache IAM ───────────────────────────────────────────────


def _aws_signer(region: str) -> RequestSigner:
    # The RequestSigner holds the session's *refreshable* credentials object
    # (botocore freezes them at sign time via get_frozen_credentials), so the
    # cached per-region signer auto-renews across credential rotation — do NOT
    # "fix" this by rebuilding the signer each call.
    #
    # The session must be cached alongside it (#1509). RequestSigner keeps the
    # event emitter as a `weakref.proxy`, and the session is what holds the
    # emitter strongly — so a session left to go out of scope here is freed, and
    # every subsequent signing raises
    # `ReferenceError: weakly-referenced object no longer exists`.
    #
    # It is freed by the *cyclic* collector rather than by refcounting, because
    # a botocore Session contains reference cycles. That is precisely why the
    # symptom read as intermittent: the session outlives this function, the
    # first connection signs fine, and auth only dies once a collection happens
    # to run.
    key = region or "default"
    # The lock covers the cache, not the signing (#1510). Building the session
    # resolves the credential chain, which can itself do I/O, so it is
    # serialised — once per region — rather than raced by every connection.
    with _aws_lock:
        cached = _aws_signers.get(key)
        if cached is None:
            import botocore.session
            from botocore.exceptions import NoCredentialsError
            from botocore.model import ServiceId
            from botocore.signers import RequestSigner

            session = botocore.session.get_session()
            credentials = session.get_credentials()
            if credentials is None:
                # The chain yielded nothing. botocore returns None here rather
                # than raising, so caching this entry would poison the cache for
                # the life of the process: every later mint would fail with
                # NoCredentialsError even once credentials became available.
                # Fail now instead, and leave the cache empty so a later attempt
                # can succeed. (A provider that *raises* never reached the store
                # anyway; only the silent-empty-chain case could poison it.)
                raise NoCredentialsError()

            signer = RequestSigner(
                ServiceId("elasticache"),
                region or session.get_config_variable("region"),
                "elasticache",
                "v4",
                credentials,
                session.get_component("event_emitter"),
            )
            cached = (session, signer)
            _aws_signers[key] = cached
        return cached[1]


def mint_aws_elasticache_token(*, cache_name: str, user: str, region: str) -> str:
    """SigV4-presigned ElastiCache ``connect`` token (local signing, no I/O)."""
    # Deliberately outside any lock of ours (#1510): botocore's
    # RefreshableCredentials guards its own refresh, so concurrent mints do not
    # need to queue behind one another — and if a refresh is needed, botocore
    # coordinates it without stalling every other connection.
    signer = _aws_signer(region)
    url = f"https://{cache_name}/?{urlencode({'Action': 'connect', 'User': user})}"
    signed = signer.generate_presigned_url(
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

    # The refresh stays inside the lock: google-auth has no internal locking and
    # `refresh()` mutates the credentials in place, so concurrent refreshes of
    # one shared object would race (#1510).
    with _gcp_lock:
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
    # `get_token` stays inside the lock: azure-identity's GetTokenMixin carries
    # no internal locking, so its cached-token refresh is not guaranteed safe
    # under concurrent callers (#1510).
    with _azure_lock:
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
