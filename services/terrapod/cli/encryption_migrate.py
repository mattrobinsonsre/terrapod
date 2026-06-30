"""Resumable encrypt-existing / decrypt-on-disable migration (#553).

Run via:
  python -m terrapod.cli.encryption_migrate encrypt   # encrypt legacy plaintext + re-key to active DEK
  python -m terrapod.cli.encryption_migrate decrypt   # decrypt back to plaintext (before disabling)

Death-by-encryption discipline:
  - VERIFY-READBACK per row: after writing, the value is read back and decrypted
    and compared to the original plaintext BEFORE moving on. A mismatch aborts the
    run immediately — we never leave a row we can't read back.
  - RESUMABLE + idempotent: `encrypt` skips rows already at the active DEK version;
    re-running after an interruption simply continues. `decrypt` skips plaintext
    rows. Safe to run repeatedly.
  - Plaintext is only overwritten by ciphertext that has just been proven
    decryptable (and vice-versa).

Environment: the same config the API uses (DATABASE_URL + encryption config + KEK
secret env). `encrypt` requires encryption enabled; `decrypt` requires the keys to
still be loadable (run it before removing the key / disabling the provider).
"""

import asyncio
import logging
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.crypto import envelope
from terrapod.crypto.columns import ENCRYPTED_COLUMNS
from terrapod.crypto.service import get_encryption, init_encryption
from terrapod.db.session import close_db, get_db_session, init_db

logger = logging.getLogger("terrapod.encryption_migrate")
logging.basicConfig(level=logging.INFO, format="%(message)s")

_BATCH = 200


def _plan_row(stored: str, mode: str, active: int | None, svc) -> tuple[str | None, str | None]:  # type: ignore[no-untyped-def]
    """Decide the rewrite for one stored value (pure — unit-tested).

    Returns ``(new_value, plaintext)`` where ``new_value is None`` means SKIP
    (already in the desired state). ``plaintext`` is the recovered cleartext used
    for verify-readback.
    """
    encrypted = envelope.is_encrypted(stored)
    if mode == "encrypt":
        if encrypted and envelope.parse_version(stored) == active:
            return None, None  # already at active DEK → skip
        plaintext = svc.decrypt(stored)  # legacy plaintext passes through; old versions decrypt
        return svc.encrypt(plaintext), plaintext
    # decrypt
    if not encrypted:
        return None, None  # already plaintext → skip
    plaintext = svc.decrypt(stored)
    return plaintext, plaintext


async def _migrate_column(db: AsyncSession, table: str, col: str, mode: str) -> tuple[int, int]:
    """Return (rewritten, skipped) for one column."""
    svc = get_encryption()
    active = svc._active_version
    rewritten = skipped = 0

    rows = (
        await db.execute(
            text(f"SELECT id, {col} AS v FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")  # noqa: S608
        )
    ).all()

    for row in rows:
        rid, stored = row.id, row.v
        new_value, plaintext = _plan_row(stored, mode, active, svc)
        if new_value is None:
            skipped += 1
            continue

        await db.execute(
            text(f"UPDATE {table} SET {col} = :v WHERE id = :id"),  # noqa: S608
            {"v": new_value, "id": rid},
        )

        # Verify-readback BEFORE trusting the write.
        check = (
            await db.execute(
                text(f"SELECT {col} AS v FROM {table} WHERE id = :id"),  # noqa: S608
                {"id": rid},
            )
        ).scalar_one()
        readback = svc.decrypt(check) if envelope.is_encrypted(check) else check
        if readback != plaintext:
            await db.rollback()
            raise RuntimeError(
                f"verify-readback FAILED for {table}.{col} id={rid} — aborting, no further rows touched"
            )

        rewritten += 1
        if rewritten % _BATCH == 0:
            await db.commit()

    await db.commit()
    return rewritten, skipped


async def migrate(mode: str) -> None:
    if mode not in ("encrypt", "decrypt"):
        logger.error("usage: python -m terrapod.cli.encryption_migrate {encrypt|decrypt}")
        sys.exit(2)

    await init_db()
    try:
        async with get_db_session() as db:
            await init_encryption(db)
            svc = get_encryption()
            if mode == "encrypt" and (not svc.enabled or svc._active_version is None):
                logger.error("encrypt requires encryption to be enabled with an active DEK")
                sys.exit(1)
            if mode == "decrypt" and not svc._deks:
                logger.error("decrypt requires the DEK(s) to be loadable (keys/provider present)")
                sys.exit(1)

            total_rw = total_sk = 0
            for table, col in ENCRYPTED_COLUMNS:
                rw, sk = await _migrate_column(db, table, col, mode)
                logger.info("  %-28s %-14s rewritten=%d skipped=%d", table, col, rw, sk)
                total_rw += rw
                total_sk += sk
    finally:
        await close_db()

    logger.info(
        "\nEncryption %s complete — %d rewritten, %d already-%s (skipped).",
        mode,
        total_rw,
        total_sk,
        "encrypted" if mode == "encrypt" else "plaintext",
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "encrypt"
    asyncio.run(migrate(mode))


if __name__ == "__main__":
    main()
