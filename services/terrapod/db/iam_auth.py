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
via a SQLAlchemy ``do_connect`` event (which also forces TLS). RDS/Cloud SQL/Entra
tokens authenticate at connect time and existing connections persist after the
token expires, so a fresh token per *new* connection is sufficient — no
pool-recycle clamp is needed.

The credential libraries cache tokens and refresh only near expiry, so the
handler is a fast cached read in steady state (the periodic refresh is the only
network call, ~hourly). The default ``auth_mode = "password"`` (static
credentials from ``database_url``) is unchanged and remains fully supported;
nothing in this module runs unless an IAM mode is explicitly selected.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)

# All cloud IAM DB auth mandates TLS; this is the minimum if none was configured.
_DEFAULT_SSL_MODE = "require"

# GCP OAuth2 scope for Cloud SQL IAM login; Azure Entra scope for OSS RDBMS.
_GCP_LOGIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"
_AZURE_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

_lock = threading.Lock()
_aws_clients: dict[str, object] = {}
_gcp_state: dict[str, object] = {}
_azure_state: dict[str, object] = {}


def parse_pg_target(database_url: str) -> tuple[str, int, str]:
    """Extract ``(host, port, user)`` from a ``postgresql[+asyncpg]://`` URL."""
    parts = urlsplit(str(database_url))
    return (parts.hostname or ""), (parts.port or 5432), (parts.username or "")


# ── AWS RDS IAM ───────────────────────────────────────────────────────


def _aws_rds_client(region: str):
    key = region or "default"
    client = _aws_clients.get(key)
    if client is None:
        with _lock:
            client = _aws_clients.get(key)
            if client is None:
                import botocore.session

                client = botocore.session.get_session().create_client(
                    "rds", region_name=region or None
                )
                _aws_clients[key] = client
    return client


def mint_aws_rds_token(*, host: str, port: int, user: str, region: str) -> str:
    """Short-lived AWS RDS IAM auth token (local SigV4 signing, no I/O)."""
    return _aws_rds_client(region).generate_db_auth_token(  # type: ignore[attr-defined]
        DBHostname=host, Port=port, DBUsername=user, Region=region or None
    )


# ── GCP Cloud SQL IAM ─────────────────────────────────────────────────


def mint_gcp_access_token() -> str:
    """OAuth2 access token for the SA (Cloud SQL IAM login); cached + refreshed."""
    import google.auth
    import google.auth.transport.requests

    creds = _gcp_state.get("creds")
    if creds is None:
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
    cred = _azure_state.get("cred")
    if cred is None:
        with _lock:
            cred = _azure_state.get("cred")
            if cred is None:
                from azure.identity import DefaultAzureCredential

                cred = DefaultAzureCredential()
                _azure_state["cred"] = cred
    return cred.get_token(_AZURE_PG_SCOPE).token  # type: ignore[union-attr]


# ── do_connect dispatch ───────────────────────────────────────────────


def make_do_connect_handler(
    *, auth_mode: str, host: str, port: int, user: str, region: str, ssl_mode: str
) -> Callable[..., None]:
    """Build the SQLAlchemy ``do_connect`` listener for the chosen IAM mode.

    The handler mints a fresh token and sets it as the asyncpg ``password``
    (overriding whatever the URL had), and forces TLS.
    """
    ssl_value = ssl_mode or _DEFAULT_SSL_MODE

    if auth_mode == "aws_iam":

        def _mint() -> str:
            return mint_aws_rds_token(host=host, port=port, user=user, region=region)

    elif auth_mode == "gcp_iam":
        _mint = mint_gcp_access_token
    elif auth_mode == "azure_ad":
        _mint = mint_azure_ad_token
    else:
        raise ValueError(f"unsupported IAM database auth_mode: {auth_mode!r}")

    def _do_connect(_dialect, _conn_rec, _cargs, cparams: dict) -> None:
        cparams["password"] = _mint()
        cparams["ssl"] = ssl_value

    return _do_connect
