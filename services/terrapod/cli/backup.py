"""Logical PostgreSQL backup to object storage.

Run via: ``python -m terrapod.cli.backup``

Runs ``pg_dump`` in custom format and streams the dump to the configured object
store under ``settings.backup.prefix``, then prunes old backups per the
retention policy. Reuses the app's storage backend (S3 / Azure / GCS /
filesystem) and the app's cloud-IAM DB auth, so it needs no new infra or
credentials — a standard deployment already has both halves configured.

This is a logical dump: RPO is the dump interval, not point-in-time. It is the
baseline floor, not a replacement for RDS snapshots / WAL-G / pgBackRest for
serious-RPO deployments. See docs/disaster-recovery.md.

Environment (set by the Helm CronJob):
  DATABASE_URL          PostgreSQL connection URL (from the chart's PG secret)
  TP_DB_AUTH_MODE etc.  Cloud-IAM DB auth (optional; same vars as the API Job)
  TERRAPOD_CONFIG       Path to config.yaml (storage backend + backup settings)
"""

import asyncio
import logging
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from terrapod.config import settings
from terrapod.storage import close_storage, get_storage, init_storage

logger = logging.getLogger("terrapod.backup")
logging.basicConfig(level=logging.INFO, format="%(message)s")

_CHUNK = 1024 * 1024  # 1 MiB stream chunk


def _resolve_tmpdir() -> str | None:
    """Substantial tempfiles land on the CSP-attached PVC, not /tmp (RAM)."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _libpq_dsn(database_url: str) -> str:
    """Strip the SQLAlchemy ``+asyncpg`` driver suffix for the libpq tools."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _pg_dump_env() -> dict[str, str]:
    """Build the pg_dump environment, minting a cloud-IAM token if configured.

    In the default static-password mode the password rides in DATABASE_URL and
    no extra env is needed. For an IAM ``auth_mode`` we mint the same short-lived
    token the API uses and pass it via ``PGPASSWORD`` (+ TLS via ``PGSSLMODE`` /
    ``PGSSLROOTCERT``), so backups work on a password-less IAM database too.
    """
    env = dict(os.environ)
    auth_mode = os.environ.get("TP_DB_AUTH_MODE", "password")
    if auth_mode not in ("aws_iam", "gcp_iam", "azure_ad"):
        return env

    from terrapod.db.iam_auth import _select_minter, parse_pg_target

    host, port, user = parse_pg_target(os.environ.get("DATABASE_URL", ""))
    minter = _select_minter(
        auth_mode,
        host=host,
        port=port,
        user=user,
        region=os.environ.get("TP_DB_AWS_IAM_REGION", ""),
    )
    env["PGPASSWORD"] = minter()
    ssl_mode = os.environ.get("TP_DB_SSL_MODE", "")
    if ssl_mode:
        env["PGSSLMODE"] = ssl_mode
    ssl_root = os.environ.get("TP_DB_SSL_ROOT_CERT", "")
    if ssl_root:
        env["PGSSLROOTCERT"] = ssl_root
    return env


async def _run_pg_dump(dsn: str, out_path: str) -> None:
    """Run ``pg_dump -Fc`` into ``out_path`` (custom format → pg_restore)."""
    proc = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        out_path,
        "--dbname",
        dsn,
        env=_pg_dump_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"pg_dump failed (exit {proc.returncode}): {msg}")


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    """Yield a file's bytes in chunks without blocking the event loop."""
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(fh.read, _CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(fh.close)


def _backup_ts() -> str:
    """UTC timestamp for the backup key (sortable, filename-safe)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def _prune(prefix: str, keep: int, days: int) -> None:
    """Delete old backups beyond ``keep`` newest and older than ``days``."""
    if keep <= 0 and days <= 0:
        return
    store = get_storage()
    entries = [m for m in await store.list_prefix(prefix) if m.key.endswith(".dump")]
    # Keys embed a sortable ISO-ish timestamp → lexical sort == chronological.
    entries.sort(key=lambda m: m.key, reverse=True)

    to_delete: set[str] = set()
    if keep > 0:
        for m in entries[keep:]:
            to_delete.add(m.key)
    if days > 0:
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        for m in entries:
            ts = _parse_key_ts(m.key, prefix)
            if ts is not None and ts < cutoff:
                to_delete.add(m.key)
    for key in sorted(to_delete):
        await store.delete(key)
        logger.info("Pruned old backup: %s", key)


def _parse_key_ts(key: str, prefix: str) -> float | None:
    """Recover the epoch seconds from a ``<prefix><ts>.dump`` key."""
    name = key[len(prefix) :] if key.startswith(prefix) else key
    name = name.rsplit("/", 1)[-1].removesuffix(".dump")
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


async def backup() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error("DATABASE_URL is required")
        sys.exit(1)

    cfg = settings.backup
    prefix = cfg.prefix if cfg.prefix.endswith("/") else cfg.prefix + "/"
    key = f"{prefix}{_backup_ts()}.dump"
    dsn = _libpq_dsn(database_url)

    fd, tmp_path = tempfile.mkstemp(suffix=".dump", dir=_resolve_tmpdir())
    os.close(fd)
    try:
        logger.info("Starting pg_dump → %s", tmp_path)
        await _run_pg_dump(dsn, tmp_path)
        size = os.path.getsize(tmp_path)
        logger.info("pg_dump complete (%d bytes); uploading to %s", size, key)

        await init_storage()
        try:
            await get_storage().put_stream(
                key, _file_chunks(tmp_path), content_type="application/octet-stream"
            )
            logger.info("Backup uploaded: %s", key)
            await _prune(prefix, cfg.retention_keep, cfg.retention_days)
        finally:
            await close_storage()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main() -> None:
    asyncio.run(backup())


if __name__ == "__main__":
    main()
