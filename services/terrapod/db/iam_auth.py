"""Cloud-IAM database authentication (#573).

Opt-in via ``database.auth_mode`` in ``{aws_iam, gcp_iam, azure_ad}``. Each new
pooled connection authenticates with a freshly-minted, short-lived token instead
of a static password, under the API pod's **workload identity** (AWS IRSA / GCP
WIF / Azure WI) — so there is no static database password and no static cloud
credential anywhere:

- ``aws_iam``  — AWS RDS IAM auth token (botocore ``generate_db_auth_token``,
  local SigV4 signing).
- ``gcp_iam``  — GCP Cloud SQL IAM: the service account's OAuth2 access token
  (google-auth Application Default Credentials / Workload Identity Federation).
- ``azure_ad`` — Azure Database for PostgreSQL Microsoft Entra auth: an Entra
  access token for the ``ossrdbms`` scope (azure-identity
  ``DefaultAzureCredential``).

In every case the token is the database **password**, supplied per connection
via a SQLAlchemy ``do_connect`` event (which also configures TLS). RDS/Cloud
SQL/Entra tokens authenticate at connect time and existing connections persist
after the token expires, so a fresh token per *new* connection is sufficient —
no pool-recycle clamp is needed.

**Event-loop safety (HARD REQUIREMENT — rule 13):** ``do_connect`` runs
synchronously on the asyncio event-loop thread (SQLAlchemy drives the async
connect via greenlet). The GCP/Azure token mints can do blocking network I/O
(``creds.refresh`` / ``cred.get_token``). To avoid stalling the loop we never
mint inline: ``do_connect`` sets the asyncpg ``password`` to an **awaitable
callable** that offloads the mint with ``asyncio.to_thread`` (asyncpg awaits a
callable/awaitable password), keeping the loop free while the token is fetched
in a worker thread. The token libraries cache and refresh only near expiry, so
steady-state is a cheap cached read in that thread. The mints serialise on a
module lock so a concurrent refresh can't corrupt shared credential state.

The default ``auth_mode = "password"`` (static credentials from
``database_url``) is unchanged and remains fully supported; nothing in this
module runs unless an IAM mode is explicitly selected.
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from collections.abc import Awaitable, Callable
from urllib.parse import unquote, urlsplit

import structlog

logger = structlog.get_logger(__name__)

# Cloud IAM DB auth always uses TLS; this is the minimum if none was configured.
_DEFAULT_SSL_MODE = "require"
_VALID_SSL_MODES = ("require", "verify-ca", "verify-full")

# GCP OAuth2 scope for Cloud SQL IAM login; Azure Entra scope for OSS RDBMS.
_GCP_LOGIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"
_AZURE_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# A single lock serialises every token mint/refresh so concurrent pooled
# connections can't race a shared credential object (e.g. google-auth
# Credentials.refresh mutates in place and is not thread-safe).
_lock = threading.Lock()
_aws_clients: dict[str, object] = {}
_gcp_state: dict[str, object] = {}
_azure_state: dict[str, object] = {}


def parse_pg_target(database_url: str) -> tuple[str, int, str]:
    """Extract ``(host, port, user)`` from a ``postgresql[+asyncpg]://`` URL.

    The username is percent-decoded (matching how SQLAlchemy/asyncpg decode the
    connect user) so a URL-encoded IAM user like ``sa%40project.iam`` signs/binds
    as ``sa@project.iam``.
    """
    parts = urlsplit(str(database_url))
    return (
        (parts.hostname or ""),
        (parts.port or 5432),
        unquote(parts.username or ""),
    )


def build_ssl_context(ssl_mode: str, ssl_root_cert: str) -> ssl.SSLContext:
    """Build the asyncpg SSL context for a cloud-IAM connection.

    We construct the context ourselves (rather than passing asyncpg a bare
    sslmode string) so 'verify-ca'/'verify-full' actually have CA material:
    asyncpg's string path requires ``~/.postgresql/root.crt`` and does NOT fall
    back to the system trust store. ``ssl_root_cert`` (e.g. the RDS/Cloud SQL CA
    bundle) is loaded when set; otherwise the system CAs are used.
    """
    mode = (ssl_mode or _DEFAULT_SSL_MODE).lower()
    if mode not in _VALID_SSL_MODES:
        raise ValueError(f"unsupported ssl_mode {ssl_mode!r}; expected one of {_VALID_SSL_MODES}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ssl_root_cert:
        ctx.load_verify_locations(cafile=ssl_root_cert)
    else:
        ctx.load_default_certs()

    if mode == "require":
        # Encrypt without verifying the server cert. check_hostname must be
        # cleared before lowering verify_mode or SSLContext raises.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:  # verify-full
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


# ── AWS RDS IAM ───────────────────────────────────────────────────────


def _aws_rds_client(region: str):
    key = region or "default"
    client = _aws_clients.get(key)
    if client is None:
        import botocore.session

        client = botocore.session.get_session().create_client("rds", region_name=region or None)
        _aws_clients[key] = client
    return client


def mint_aws_rds_token(*, host: str, port: int, user: str, region: str) -> str:
    """Short-lived AWS RDS IAM auth token (local SigV4 signing, no network I/O)."""
    with _lock:
        return _aws_rds_client(region).generate_db_auth_token(  # type: ignore[attr-defined]
            DBHostname=host, Port=port, DBUsername=user, Region=region or None
        )


# ── GCP Cloud SQL IAM ─────────────────────────────────────────────────


def mint_gcp_access_token() -> str:
    """OAuth2 access token for the SA (Cloud SQL IAM login); cached + refreshed.

    The whole get-or-refresh runs under ``_lock`` because google-auth
    ``Credentials.refresh`` mutates the shared credential in place.
    """
    import google.auth
    import google.auth.transport.requests

    with _lock:
        creds = _gcp_state.get("creds")
        if creds is None:
            creds, _ = google.auth.default(scopes=[_GCP_LOGIN_SCOPE])
            _gcp_state["creds"] = creds
            _gcp_state["request"] = google.auth.transport.requests.Request()
        if not creds.valid:  # type: ignore[union-attr]
            creds.refresh(_gcp_state["request"])  # type: ignore[union-attr]
        return creds.token  # type: ignore[union-attr]


# ── Azure Database for PostgreSQL (Entra) ─────────────────────────────


def mint_azure_ad_token() -> str:
    """Microsoft Entra access token for Azure Database for PostgreSQL."""
    with _lock:
        cred = _azure_state.get("cred")
        if cred is None:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
            _azure_state["cred"] = cred
        return cred.get_token(_AZURE_PG_SCOPE).token  # type: ignore[union-attr]


# ── do_connect dispatch ───────────────────────────────────────────────


def _select_minter(
    auth_mode: str, *, host: str, port: int, user: str, region: str
) -> Callable[[], str]:
    """Return the synchronous token-minting callable for the chosen IAM mode."""
    if auth_mode == "aws_iam":
        return lambda: mint_aws_rds_token(host=host, port=port, user=user, region=region)
    if auth_mode == "gcp_iam":
        return mint_gcp_access_token
    if auth_mode == "azure_ad":
        return mint_azure_ad_token
    raise ValueError(f"unsupported IAM database auth_mode: {auth_mode!r}")


def make_do_connect_handler(
    *,
    auth_mode: str,
    host: str,
    port: int,
    user: str,
    region: str,
    ssl_mode: str,
    ssl_root_cert: str = "",
) -> Callable[..., None]:
    """Build the SQLAlchemy ``do_connect`` listener for the chosen IAM mode.

    The handler sets the asyncpg ``password`` to an awaitable callable that mints
    a fresh token off the event loop (``asyncio.to_thread``), and attaches a
    pre-built TLS context. asyncpg invokes the callable and awaits its result on
    each new connection, so every connection gets a fresh token without blocking
    the loop.
    """
    mint = _select_minter(auth_mode, host=host, port=port, user=user, region=region)
    ssl_ctx = build_ssl_context(ssl_mode, ssl_root_cert)

    def _password() -> Awaitable[str]:
        # Offload the (possibly blocking) mint to a worker thread; asyncpg awaits
        # the returned coroutine, keeping the event loop free.
        return asyncio.to_thread(mint)

    def _do_connect(_dialect, _conn_rec, _cargs, cparams: dict) -> None:
        cparams["password"] = _password
        cparams["ssl"] = ssl_ctx

    return _do_connect
