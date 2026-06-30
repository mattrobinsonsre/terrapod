"""Encryption doctor — prove every encrypted secret is still decryptable.

Run via: ``python -m terrapod.cli.encryption_doctor``

Losing the ability to decrypt is data loss. This drill independently re-builds
the KEK provider and re-unwraps **every** stored DEK version live (catching a KMS
permission revoked, a Vault key deleted, or a static master key rotated away
*after* startup), then verifies each version's canary. Exits non-zero on any
failure, so it doubles as a scheduled/CI "are we still recoverable?" gate.

Run it on demand against a running deployment:
  kubectl exec deploy/terrapod-api -- python -m terrapod.cli.encryption_doctor

Environment: the same config the API uses (DATABASE_URL + encryption config +
the KEK secret env). Read-only — it never writes.
"""

import asyncio
import json
import logging
import sys

from terrapod.crypto.service import verify_live
from terrapod.db.session import close_db, get_db_session, init_db

logger = logging.getLogger("terrapod.encryption_doctor")
logging.basicConfig(level=logging.INFO, format="%(message)s")


async def doctor() -> None:
    await init_db()
    try:
        async with get_db_session() as db:
            result = await verify_live(db)
    finally:
        await close_db()

    logger.info(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        logger.error(
            "\nENCRYPTION DOCTOR FAILED — some encrypted data is NOT currently decryptable. "
            "Do NOT lose the KEK; see docs/encryption-at-rest.md#recovery."
        )
        sys.exit(1)
    checked = result.get("checked_versions") or []
    logger.info(
        "\nEncryption doctor PASSED — %d DEK version(s) unwrap and verify; all encrypted "
        "data is decryptable.",
        len(checked),
    )


def main() -> None:
    asyncio.run(doctor())


if __name__ == "__main__":
    main()
