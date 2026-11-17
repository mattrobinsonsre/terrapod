"""Re-wrap every DEK under a new KEK secret (#2589).

Run via:
  TERRAPOD_ENCRYPTION__STATIC_KEY=<old> \\
  TERRAPOD_KEK_ROTATE_NEW_SECRET=<new> \\
  python -m terrapod.cli.kek_rotate

**This is the escape hatch that makes `require_strong_secrets` safe to default
on.** Before it existed, an operator whose static KEK passphrase was weak had no
remedy: the KEK is derived from the passphrase, so changing it leaves every
wrapped DEK unreadable -- silent, unrecoverable data loss. Refusing to start on a
weak secret would therefore have stranded exactly the deployments the check is
meant to protect. With this, the remedy is one bounded command.

**Bounded, because only the DEKs are re-wrapped, not the data.** `crypto_keys`
holds one row per DEK version -- a handful -- and the DEKs themselves do not
change, so nothing encrypted under them is touched. This is not
`encryption_migrate`, which re-keys COLUMNS to a new DEK and walks the estate.

Death-by-encryption discipline, the same as its sibling:

  - **Every DEK is unwrapped under the old secret and proven re-readable under
    the new one BEFORE anything is written.** The new wrap is verified by
    unwrapping it again and comparing the raw DEK, so a row is never replaced by
    a wrap we have not already read back.
  - **One transaction.** A partial rotation is the worst outcome available --
    some DEKs under the old secret and some under the new, with no single secret
    that reads them all -- so either every row moves or none does.
  - **The canary is checked per row**, which is what proves the DEK is the real
    one rather than merely 32 bytes that decrypted.
  - **Idempotent in the way that matters**: run it twice with the same new secret
    and the second run is a no-op that still verifies, because the old secret no
    longer reads anything and the tool says so rather than writing.

Afterwards, set the static key to the new secret and restart. Keep the old one
until the restart is confirmed: until then it is the only thing that reads the
backups taken before the rotation.
"""

import asyncio
import logging
import os
import sys

from sqlalchemy import select

from terrapod.crypto import envelope
from terrapod.crypto.providers import StaticKEKProvider
from terrapod.crypto.service import _CANARY
from terrapod.db.models import CryptoKey
from terrapod.db.session import close_db, get_db_session, init_db

logger = logging.getLogger("terrapod.kek_rotate")
logging.basicConfig(level=logging.INFO, format="%(message)s")

_NEW_SECRET_ENV = "TERRAPOD_KEK_ROTATE_NEW_SECRET"


class RotationError(RuntimeError):
    """A rotation that must not proceed. Raised before anything is written."""


async def rotate(db, old_secret: str, new_secret: str) -> int:  # type: ignore[no-untyped-def]
    """Re-wrap every DEK from `old_secret` to `new_secret`. Returns the count.

    Raises `RotationError` without writing if any row cannot be read under the
    old secret, or if any new wrap does not read back identically.
    """
    if not old_secret:
        raise RotationError("the current static key is empty; nothing to rotate from")
    if not new_secret:
        raise RotationError(f"{_NEW_SECRET_ENV} is empty; nothing to rotate to")
    if old_secret == new_secret:
        raise RotationError(
            "the new secret is identical to the current one, so this would re-wrap "
            "every DEK to the same key. If the intent is to move legacy wraps onto "
            "the stretched format, that is a different operation and is not this one."
        )

    # `strict=False` on BOTH: the old secret is whatever the deployment has, and
    # refusing to read it here would defeat the purpose. The new secret's strength
    # is checked below, where the message can say what to do about it.
    old = StaticKEKProvider(old_secret, strict=False)
    new = StaticKEKProvider(new_secret, strict=False)

    from terrapod.secret_strength import describe_weakness

    weak = describe_weakness(new_secret, name=_NEW_SECRET_ENV)
    if weak is not None:
        raise RotationError(
            f"the NEW secret is weak, so rotating to it would leave you where you started: {weak}"
        )

    rows = list((await db.execute(select(CryptoKey).order_by(CryptoKey.version))).scalars())
    if not rows:
        raise RotationError(
            "no DEKs are stored, so there is nothing to rotate. A deployment mints "
            "its first DEK on startup with encryption enabled."
        )

    # PHASE 1 -- read everything and prove every new wrap before touching a row.
    planned: list[tuple[CryptoKey, str]] = []
    for row in rows:
        try:
            dek = await old.unwrap(row.wrapped_dek)
        except Exception as exc:
            raise RotationError(
                f"DEK version {row.version} does not unwrap under the current static "
                f"key ({type(exc).__name__}). Nothing has been written. Check that "
                f"the key in the environment is the one this deployment is using."
            ) from exc

        if row.provider != "static":
            raise RotationError(
                f"DEK version {row.version} was wrapped by the {row.provider!r} "
                f"provider, not 'static'. A KMS-backed KEK is rotated in the KMS, "
                f"not here."
            )

        # The canary is what proves this is the real DEK rather than 32 bytes that
        # happened to decrypt. An ACTIVE row with a bad canary is fatal.
        if row.canary:
            try:
                if envelope.decrypt(row.canary, dek) != _CANARY:
                    raise RotationError(
                        f"DEK version {row.version} decrypted its canary to the wrong "
                        f"value. Nothing has been written."
                    )
            except RotationError:
                raise
            except Exception as exc:
                raise RotationError(
                    f"DEK version {row.version} could not decrypt its own canary "
                    f"({type(exc).__name__}). Nothing has been written."
                ) from exc

        rewrapped = await new.wrap(dek)
        # Read it back NOW, under the new secret, before it replaces anything.
        if await new.unwrap(rewrapped) != dek:
            raise RotationError(
                f"the re-wrap of DEK version {row.version} did not read back "
                f"identically. Nothing has been written."
            )
        planned.append((row, rewrapped))
        logger.info("verified re-wrap for DEK version %s", row.version)

    # PHASE 2 -- one transaction. Every row or none.
    #
    # **Every refusal above happens in phase 1, before anything is written, and
    # that is what the tests pin.** Measured: with the phase split in place,
    # committing per row instead of once passes every test in
    # `test_kek_rotation.py`, because no failure is reachable here for a per-row
    # loop to interleave with.
    #
    # So the single commit is not redundant, but what it guards is narrower than
    # the split: the database going away part-way through the WRITE. Under per-row
    # commits that leaves some DEKs under the old secret and some under the new,
    # with no single secret that reads them all. That case is NOT covered by a test
    # -- `get_db_session` commits on exit too, so patching `commit` cannot isolate
    # one write from the other -- and saying so is better than a test that looks
    # like it discriminates and does not.
    for row, rewrapped in planned:
        row.wrapped_dek = rewrapped
    await db.commit()
    logger.info("rotated %s DEK(s) onto the new secret", len(planned))
    return len(planned)


#: The env var the static provider reads its secret from. Read here the SAME way
#: `providers.build_provider` reads it -- straight from the environment, because a
#: KEK secret is never rendered into a ConfigMap and so is not on `settings`.
_OLD_SECRET_ENV = "TERRAPOD_ENCRYPTION__STATIC_KEY"


async def _main() -> int:
    from terrapod.config import settings

    backend = settings.encryption.provider
    if backend != "static":
        raise RotationError(
            f"this deployment's KEK provider is {backend!r}, not 'static'. A "
            f"KMS-backed KEK is rotated in the KMS (Vault Transit key rotation, or "
            f"an AWS KMS key rotation), and the wrapped DEKs here need no change."
        )

    old_secret = (os.environ.get(_OLD_SECRET_ENV) or "").strip()
    new_secret = (os.environ.get(_NEW_SECRET_ENV) or "").strip()

    await init_db()
    try:
        async with get_db_session() as db:
            count = await rotate(db, old_secret, new_secret)
    finally:
        await close_db()

    logger.info(
        "Done. Now set the static key to the new secret and restart. Keep the old "
        "one until that restart is confirmed: it is the only thing that reads "
        "backups taken before this rotation."
    )
    return count


def main() -> None:
    try:
        asyncio.run(_main())
    except RotationError as exc:
        logger.error("%s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
