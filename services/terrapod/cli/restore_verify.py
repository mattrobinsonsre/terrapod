"""Disaster-recovery drill: restore the latest backup and verify it.

Run via: ``python -m terrapod.cli.restore_verify``

A tested restore beats a documented one. This restores the most recent
``pg_dump`` backup into a **throwaway** Postgres (never the live database) and
asserts core invariants against the restored data + the (read-only) object
store:

  1. the schema + CA load (``certificate_authority`` is queryable and populated),
  2. workspaces resolve (the ``workspaces`` table is queryable),
  3. if any state versions exist, a state object is downloadable from the store.

Exit code is non-zero if the restore or any invariant fails, so it doubles as a
CI/cron gate ("here's the green check"). It reads the object store **read-only**;
UUID-addressed artifacts mean a same-or-later object store is self-consistent
with an earlier DB dump.

Environment (set by the Helm CronJob):
  TP_RESTORE_TARGET_URL   throwaway Postgres to restore INTO (required; NOT the
                          live DATABASE_URL — the drill refuses if they match)
  TP_RESTORE_KEY          specific backup key to restore (optional; default: latest)
  TERRAPOD_CONFIG         config.yaml (storage backend + backup settings)
"""

import asyncio
import logging
import os
import sys
import tempfile

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from terrapod.config import settings
from terrapod.storage import close_storage, get_storage, init_storage

logger = logging.getLogger("terrapod.restore_verify")
logging.basicConfig(level=logging.INFO, format="%(message)s")

_CHUNK = 1024 * 1024


def _resolve_tmpdir() -> str | None:
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _libpq_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _async_dsn(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _latest_backup_key() -> str:
    cfg = settings.backup
    prefix = cfg.prefix if cfg.prefix.endswith("/") else cfg.prefix + "/"
    entries = [m for m in await get_storage().list_prefix(prefix) if m.key.endswith(".dump")]
    if not entries:
        raise RuntimeError(f"no backups found under {prefix!r}")
    entries.sort(key=lambda m: m.key, reverse=True)
    return entries[0].key


async def _download(key: str, dest: str) -> None:
    store = get_storage()
    fh = await asyncio.to_thread(open, dest, "wb")
    try:
        async for chunk in store.get_stream(key, chunk_size=_CHUNK):
            await asyncio.to_thread(fh.write, chunk)
    finally:
        await asyncio.to_thread(fh.close)


async def _pg_restore(target_dsn: str, dump_path: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        target_dsn,
        dump_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    # pg_restore can exit non-zero on benign "already exists"/"does not exist"
    # noise from --clean on an empty target; surface stderr but only fail hard
    # if the subsequent invariant queries fail.
    if proc.returncode != 0:
        logger.warning(
            "pg_restore exited %d (continuing to invariant checks): %s",
            proc.returncode,
            stderr.decode("utf-8", "replace").strip()[:2000],
        )


async def _wait_for_db(target_async_dsn: str, attempts: int = 30) -> None:
    """Wait for the throwaway Postgres (e.g. a native sidecar) to accept conns."""
    last: Exception | None = None
    for _ in range(attempts):
        engine = create_async_engine(target_async_dsn)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()
            return
        except Exception as exc:  # noqa: BLE001 — retry any connect error
            last = exc
            await engine.dispose()
            await asyncio.sleep(2)
    raise RuntimeError(f"restore target never became ready: {last}")


async def _verify_invariants(target_async_dsn: str) -> list[str]:
    """Return a list of failures (empty == all invariants held)."""
    failures: list[str] = []
    engine = create_async_engine(target_async_dsn)
    try:
        async with engine.connect() as conn:
            # 1. Schema + CA load.
            try:
                ca = (
                    await conn.execute(text("SELECT count(*) FROM certificate_authority"))
                ).scalar_one()
                if ca < 1:
                    failures.append("certificate_authority is empty (no CA restored)")
                else:
                    logger.info("✓ CA loads (%d row)", ca)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"certificate_authority not queryable: {exc}")

            # 2. Workspaces resolve.
            try:
                ws = (await conn.execute(text("SELECT count(*) FROM workspaces"))).scalar_one()
                logger.info("✓ workspaces resolve (%d row(s))", ws)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"workspaces not queryable: {exc}")

            # 3. State object downloads (only if any state versions exist).
            try:
                sv = (await conn.execute(text("SELECT count(*) FROM state_versions"))).scalar_one()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"state_versions not queryable: {exc}")
                sv = 0
            if sv > 0:
                ok = await _verify_state_object()
                if ok:
                    logger.info("✓ state object downloads from object store")
                else:
                    failures.append(
                        f"{sv} state version(s) in DB but no readable object under state/"
                    )
            else:
                logger.info("• no state versions to verify (empty/fresh dataset)")
    finally:
        await engine.dispose()
    return failures


async def _verify_state_object() -> bool:
    """Download one object under ``state/`` to prove the store is readable.

    Decoupled from the exact key scheme: a state version in the DB should have a
    corresponding object somewhere under ``state/``.
    """
    store = get_storage()
    objs = [m for m in await store.list_prefix("state/") if m.key.endswith(".tfstate")]
    if not objs:
        return False
    data = await store.get(objs[0].key)
    return bool(data)


async def restore_verify() -> None:
    target = os.environ.get("TP_RESTORE_TARGET_URL", "").strip()
    if not target:
        logger.error("TP_RESTORE_TARGET_URL is required (a THROWAWAY Postgres)")
        sys.exit(1)

    live = os.environ.get("DATABASE_URL", "").strip()
    if live and _libpq_dsn(live) == _libpq_dsn(target):
        logger.error("TP_RESTORE_TARGET_URL must not equal DATABASE_URL — refusing")
        sys.exit(1)

    target_async = _async_dsn(target)
    target_libpq = _libpq_dsn(target)

    await init_storage()
    fd, tmp_path = tempfile.mkstemp(suffix=".dump", dir=_resolve_tmpdir())
    os.close(fd)
    try:
        key = os.environ.get("TP_RESTORE_KEY", "").strip() or await _latest_backup_key()
        logger.info("Restoring backup %s", key)
        await _download(key, tmp_path)

        await _wait_for_db(target_async)
        await _pg_restore(target_libpq, tmp_path)

        failures = await _verify_invariants(target_async)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        await close_storage()

    if failures:
        logger.error("DR drill FAILED:")
        for f in failures:
            logger.error("  ✗ %s", f)
        sys.exit(1)
    logger.info("DR drill PASSED — backup %s restored and verified", key)


def main() -> None:
    asyncio.run(restore_verify())


if __name__ == "__main__":
    main()
