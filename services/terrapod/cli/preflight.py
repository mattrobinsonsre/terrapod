"""Cloud-identity + object-store preflight doctor.

Run via: ``python -m terrapod.cli.preflight``

A one-shot check that the ServiceAccount it runs under can actually assume its
cloud role and reach the configured object store — surfacing an OIDC
trust-policy / SA-binding mismatch *before* the first run fails cryptically.

It exercises the **same code path** the platform uses (the real storage backend
init + a list on the bucket) and resolves the cloud identity (AWS STS
GetCallerIdentity / GCP SA email / Azure token claims), printing a clear
pass/fail. Failure hints map to the Troubleshooting section of
``docs/cloud-credentials.md``.

Modes (``TP_PREFLIGHT_MODE``):
  full      (default) storage read + cloud identity + a DB connect — run under
            the API ServiceAccount.
  identity  cloud identity only — run under the runner ServiceAccount (runners
            don't read the app object store; they need a working cloud identity
            for provider auth).

Environment (set by the Helm hook):
  DATABASE_URL          PostgreSQL URL (full mode; from the chart's PG secret)
  TP_DB_AUTH_MODE etc.  Cloud-IAM DB auth (optional; same vars as the API)
  TERRAPOD_CONFIG       config.yaml (storage backend selection)
"""

import asyncio
import logging
import os
import sys

from terrapod.config import StorageBackend, settings
from terrapod.storage import close_storage, get_storage, init_storage

logger = logging.getLogger("terrapod.preflight")
logging.basicConfig(level=logging.INFO, format="%(message)s")

_DOCS = "https://github.com/mattrobinsonsre/terrapod/blob/main/docs/cloud-credentials.md#troubleshooting"
_PROBE_PREFIX = "__terrapod_preflight_probe__/"


class _Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok: bool | None = None
        self.detail = ""

    def passed(self, detail: str) -> "_Check":
        self.ok, self.detail = True, detail
        return self

    def failed(self, detail: str) -> "_Check":
        self.ok, self.detail = False, detail
        return self

    def skipped(self, detail: str) -> "_Check":
        self.ok, self.detail = None, detail
        return self


def _backend_cloud() -> str | None:
    """Map the configured storage backend to its cloud, or None (filesystem)."""
    return {
        StorageBackend.S3: "aws",
        StorageBackend.GCS: "gcp",
        StorageBackend.AZURE: "azure",
    }.get(settings.storage.backend)


async def _check_storage() -> _Check:
    """Init the real storage backend and list a prefix — proves read access."""
    chk = _Check("object store reachable")
    try:
        await init_storage()
    except Exception as exc:  # noqa: BLE001
        return chk.failed(f"storage init failed: {exc}")
    try:
        # A list on a nonsense prefix returns [] when credentials are valid and
        # raises (e.g. 403 AccessDenied) when they are not — the cheapest probe
        # that exercises the SA's bucket permission without mutating anything.
        await get_storage().list_prefix(_PROBE_PREFIX)
        return chk.passed(f"{settings.storage.backend.value}: list succeeded")
    except Exception as exc:  # noqa: BLE001
        return chk.failed(f"{settings.storage.backend.value}: {exc}")
    finally:
        await close_storage()


def _resolve_identity() -> _Check:
    """Best-effort: report the resolved cloud identity for the running SA."""
    chk = _Check("cloud identity resolves")
    cloud = _backend_cloud()
    if cloud is None:
        return chk.skipped("filesystem backend — no cloud identity to resolve")
    try:
        if cloud == "aws":
            import botocore.session

            sts = botocore.session.get_session().create_client("sts")
            ident = sts.get_caller_identity()
            return chk.passed(f"AWS: {ident.get('Arn', ident.get('UserId', '?'))}")
        if cloud == "gcp":
            import google.auth

            creds, project = google.auth.default()
            who = getattr(creds, "service_account_email", None) or "(token-based)"
            return chk.passed(f"GCP: {who} (project {project})")
        if cloud == "azure":
            from azure.identity import DefaultAzureCredential

            token = DefaultAzureCredential().get_token("https://storage.azure.com/.default")
            return chk.passed(f"Azure: token acquired (expires {token.expires_on})")
    except Exception as exc:  # noqa: BLE001
        return chk.failed(f"{cloud}: {exc}")
    return chk.skipped(f"no identity probe for {cloud}")


async def _check_database() -> _Check:
    """Connect to PostgreSQL the same way the API does (incl. cloud-IAM auth)."""
    chk = _Check("database reachable")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return chk.skipped("DATABASE_URL not set")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        from terrapod.db.iam_auth import register_engine_iam_auth

        register_engine_iam_auth(engine.sync_engine, database_url)
    except Exception:  # noqa: BLE001 — IAM wiring is best-effort
        pass
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return chk.passed("SELECT 1 succeeded")
    except Exception as exc:  # noqa: BLE001
        return chk.failed(str(exc))
    finally:
        await engine.dispose()


async def preflight() -> None:
    mode = os.environ.get("TP_PREFLIGHT_MODE", "full").strip() or "full"
    logger.info("Terrapod preflight doctor (mode=%s)", mode)

    checks: list[_Check] = []
    if mode == "identity":
        # Runner SA: only the cloud-identity binding matters.
        checks.append(_resolve_identity())
    else:
        checks.append(await _check_storage())
        checks.append(_resolve_identity())
        checks.append(await _check_database())

    logger.info("")
    hard_fail = False
    for c in checks:
        if c.ok is True:
            logger.info("  ✓ %s — %s", c.name, c.detail)
        elif c.ok is None:
            logger.info("  • %s — %s", c.name, c.detail)
        else:
            logger.info("  ✗ %s — %s", c.name, c.detail)
            hard_fail = True

    if hard_fail:
        logger.error("\nPreflight FAILED. See %s", _DOCS)
        sys.exit(1)
    logger.info("\nPreflight PASSED.")


def main() -> None:
    asyncio.run(preflight())


if __name__ == "__main__":
    main()
