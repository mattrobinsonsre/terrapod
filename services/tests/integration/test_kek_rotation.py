"""Rotating the static KEK, which is what makes the strict flip safe (#2589).

Before this existed, an operator whose static passphrase was weak had no remedy:
the KEK derives from the passphrase, so changing it leaves every wrapped DEK
unreadable. Defaulting `require_strong_secrets` on would therefore have stranded
exactly the deployments the check protects.

Real Postgres, because the properties worth pinning are about ROWS: that a
rotation is all-or-nothing, and that nothing is written until every new wrap has
been read back. A mocked session cannot show either.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select

from terrapod.cli.kek_rotate import RotationError, rotate
from terrapod.crypto import envelope
from terrapod.crypto.providers import _WRAP_V2, StaticKEKProvider
from terrapod.crypto.service import _CANARY
from terrapod.db.models import CryptoKey
from terrapod.db.session import get_db_session

OLD = "the-original-static-passphrase-4f91c2a7"
NEW = "a-different-strong-static-passphrase-77b3e1"


async def _seed(*, versions=(1,), legacy=False, provider="static", canary=True):
    """Write DEK rows wrapped under OLD. `legacy` writes the pre-v2 format."""
    import base64
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    old = StaticKEKProvider(OLD, strict=False)
    made = []
    async with get_db_session() as db:
        for v in versions:
            dek = envelope.new_dek()
            if legacy:
                kek = hashlib.sha256(OLD.encode()).digest()
                nonce = os.urandom(12)
                ct = AESGCM(kek).encrypt(nonce, dek, None)
                wrapped = f"{base64.b64encode(nonce).decode()}.{base64.b64encode(ct).decode()}"
            else:
                wrapped = await old.wrap(dek)
            row = CryptoKey(
                version=v,
                wrapped_dek=wrapped,
                provider=provider,
                canary=envelope.encrypt(_CANARY, dek, v) if canary else "",
                active=(v == max(versions)),
            )
            db.add(row)
            made.append((v, dek))
        await db.commit()
    return dict(made)


async def _rows():
    async with get_db_session() as db:
        return list((await db.execute(select(CryptoKey).order_by(CryptoKey.version))).scalars())


@pytest.fixture(autouse=True)
async def _clean():
    async with get_db_session() as db:
        for row in (await db.execute(select(CryptoKey))).scalars().all():
            await db.delete(row)
        await db.commit()
    yield


class TestARotationThatWorks:
    async def test_every_dek_moves_and_is_unchanged(self, app) -> None:
        """The DEKs themselves must be IDENTICAL afterwards. If a rotation changed
        them, everything encrypted under them would be unreadable -- which is the
        failure this whole path exists to avoid, arriving through its remedy."""
        deks = await _seed(versions=(1, 2))

        async with get_db_session() as db:
            count = await rotate(db, OLD, NEW)
        assert count == 2

        new = StaticKEKProvider(NEW, strict=False)
        for row in await _rows():
            assert await new.unwrap(row.wrapped_dek) == deks[row.version], (
                "the DEK changed, so everything encrypted under it is now unreadable"
            )
            # And the canary still decrypts, which is what proves it is the real DEK
            # rather than 32 bytes that happened to decrypt.
            assert envelope.decrypt(row.canary, deks[row.version]) == _CANARY

    async def test_the_new_wraps_are_the_STRETCHED_format(self, app) -> None:
        """A rotation that re-wrapped in the legacy format would move the secret and
        leave the weakness (GHSA-hc47) exactly where it was."""
        await _seed(versions=(1,))
        async with get_db_session() as db:
            await rotate(db, OLD, NEW)
        for row in await _rows():
            assert row.wrapped_dek.startswith(f"{_WRAP_V2}."), row.wrapped_dek[:20]

    async def test_a_LEGACY_wrap_is_readable_and_is_migrated(self, app) -> None:
        """The upgrade path for a deployment that predates the stretched format: its
        wraps are unsalted sha256, must still be read, and come out stretched."""
        deks = await _seed(versions=(1,), legacy=True)
        rows = await _rows()
        assert not rows[0].wrapped_dek.startswith(f"{_WRAP_V2}."), "fixture is not legacy"

        async with get_db_session() as db:
            await rotate(db, OLD, NEW)

        new = StaticKEKProvider(NEW, strict=False)
        for row in await _rows():
            assert row.wrapped_dek.startswith(f"{_WRAP_V2}.")
            assert await new.unwrap(row.wrapped_dek) == deks[row.version]

    async def test_the_old_secret_no_longer_reads_anything(self, app) -> None:
        """Which is the point -- and is also why the docstring says to keep the old
        secret until the restart is confirmed, since backups predate the rotation."""
        await _seed(versions=(1,))
        async with get_db_session() as db:
            await rotate(db, OLD, NEW)
        old = StaticKEKProvider(OLD, strict=False)
        # InvalidTag specifically: the AEAD REJECTED it, rather than decrypting it
        # into rubbish that a caller might go on to use as a key.
        with pytest.raises(InvalidTag):
            await old.unwrap((await _rows())[0].wrapped_dek)


class TestWhatIsRefusedBeforeAnythingIsWritten:
    """Every one of these must leave the rows EXACTLY as they were.

    A partial rotation is the worst outcome available: some DEKs under the old
    secret and some under the new, with no single secret that reads them all. So
    the assertion is always on the stored rows, never only on the exception.
    """

    async def test_a_wrong_current_secret_writes_nothing(self, app) -> None:
        deks = await _seed(versions=(1, 2))
        before = [r.wrapped_dek for r in await _rows()]

        async with get_db_session() as db:
            with pytest.raises(RotationError, match="does not unwrap"):
                await rotate(db, "not-the-current-secret-at-all-9a1", NEW)

        assert [r.wrapped_dek for r in await _rows()] == before
        old = StaticKEKProvider(OLD, strict=False)
        for row in await _rows():
            assert await old.unwrap(row.wrapped_dek) == deks[row.version]

    async def test_a_WEAK_new_secret_is_refused(self, app) -> None:
        """Rotating to a weak secret would leave the operator where they started,
        and would then be refused at startup by the very check this unblocks."""
        await _seed(versions=(1,))
        before = [r.wrapped_dek for r in await _rows()]
        async with get_db_session() as db:
            with pytest.raises(RotationError, match="weak"):
                await rotate(db, OLD, "password")
        assert [r.wrapped_dek for r in await _rows()] == before

    async def test_an_identical_new_secret_is_refused(self, app) -> None:
        await _seed(versions=(1,))
        async with get_db_session() as db:
            with pytest.raises(RotationError, match="identical"):
                await rotate(db, OLD, OLD)

    async def test_a_KMS_wrapped_dek_is_refused(self, app) -> None:
        """A KMS-backed KEK is rotated in the KMS. Silently re-wrapping it with a
        static key would move a deployment off its KMS without being asked."""
        await _seed(versions=(1,), provider="awskms")
        async with get_db_session() as db:
            with pytest.raises(RotationError, match="awskms|not 'static'"):
                await rotate(db, OLD, NEW)

    async def test_a_BAD_canary_writes_nothing(self, app) -> None:
        """32 bytes that decrypt are not necessarily the DEK. The canary is the only
        thing that distinguishes them, so a mismatch must stop the rotation."""
        await _seed(versions=(1,))
        async with get_db_session() as db:
            rows = list((await db.execute(select(CryptoKey))).scalars())
            rows[0].canary = envelope.encrypt("not-the-canary", envelope.new_dek(), 1)
            await db.commit()
        before = [r.wrapped_dek for r in await _rows()]

        async with get_db_session() as db:
            with pytest.raises(RotationError, match="canary"):
                await rotate(db, OLD, NEW)
        assert [r.wrapped_dek for r in await _rows()] == before

    async def test_no_deks_at_all_is_refused_rather_than_reported_as_success(self, app) -> None:
        """Zero rows rotated is indistinguishable from a successful rotation if it
        returns 0 quietly -- and an operator would conclude they were done."""
        async with get_db_session() as db:
            with pytest.raises(RotationError, match="nothing to rotate"):
                await rotate(db, OLD, NEW)

    async def test_an_empty_new_secret_is_refused(self, app) -> None:
        await _seed(versions=(1,))
        async with get_db_session() as db:
            with pytest.raises(RotationError, match="nothing to rotate to"):
                await rotate(db, OLD, "")


class TestItIsAllOrNothing:
    async def test_a_failure_on_the_SECOND_dek_leaves_the_first_alone(self, app) -> None:
        """The property a single-row test cannot show. The second row is unreadable,
        so the first must not have moved -- otherwise one secret reads row 1 and
        another reads row 2, and no secret reads both."""
        deks = await _seed(versions=(1, 2))
        async with get_db_session() as db:
            rows = list((await db.execute(select(CryptoKey).order_by(CryptoKey.version))).scalars())
            rows[1].wrapped_dek = "v2.bm90.cmVhbA==.Z2FyYmFnZQ=="  # unreadable
            await db.commit()
        before = {r.version: r.wrapped_dek for r in await _rows()}

        async with get_db_session() as db:
            with pytest.raises(RotationError):
                await rotate(db, OLD, NEW)

        after = {r.version: r.wrapped_dek for r in await _rows()}
        assert after == before, "a partial rotation left the DEKs under two secrets"
        old = StaticKEKProvider(OLD, strict=False)
        assert await old.unwrap(after[1]) == deks[1], "version 1 moved despite the failure"
