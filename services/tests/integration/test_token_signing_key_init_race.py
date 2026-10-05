"""Integration test for the multi-replica signing-key init race (#1994).

`init_token_signing_key` must be safe when several API replicas start against a
database with no stored key: exactly one row may be created, and every caller
must end up with the *same* key. Without the advisory lock each caller sees "no
key", each generates or adopts one, and each inserts a row — leaving replicas
signing with different keys and rejecting each other's runner tokens, which is
the v1.7.3 outage reached by a different route.

Requires a real Postgres engine (advisory locks are a Postgres feature), so this
lives in the integration tier. The unit tier can only assert that the lock is
*asked for*; this is what proves it serializes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import text

from terrapod.auth import token_signing
from terrapod.config import settings
from terrapod.db.session import get_db_session


async def _init_once() -> bytes:
    """Run the initializer on its own session/connection, as a replica would."""
    async with get_db_session() as session:
        return await token_signing.init_token_signing_key(session)


async def _clear_stored_keys() -> None:
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM token_signing_keys"))
        await session.commit()
    token_signing._reset_cache_for_tests()


async def _row_count() -> int:
    async with get_db_session() as session:
        return (await session.execute(text("SELECT count(*) FROM token_signing_keys"))).scalar_one()


async def test_concurrent_init_creates_a_single_key(app):
    """Four concurrent initializers on a key-less database must yield ONE row."""
    await _clear_stored_keys()

    # An operator-supplied key would short-circuit the whole database path and
    # make this assert nothing, so pin it empty rather than trusting the env.
    with patch.object(settings, "token_signing_key", ""):
        keys = await asyncio.gather(*[_init_once() for _ in range(4)])

    assert await _row_count() == 1, "expected exactly one stored key (multi-replica race)"
    assert len({k.hex() for k in keys}) == 1, (
        f"replicas disagree on the signing key: {sorted({k.hex()[:12] for k in keys})}. "
        "Tokens minted by one would be rejected by another."
    )
    assert len(keys[0]) == 32


async def test_init_is_idempotent_once_a_key_exists(app):
    """A restart must load the stored key, not mint a replacement."""
    await _clear_stored_keys()
    with patch.object(settings, "token_signing_key", ""):
        first = await _init_once()
        token_signing._reset_cache_for_tests()
        second = await _init_once()

    assert first == second, "a restart changed the signing key, invalidating live tokens"
    assert await _row_count() == 1


async def test_the_branch_taken_matches_whether_the_deployment_has_run_anything(app):
    """Generate on a never-used deployment, adopt on one that has runs.

    Both paths insert exactly one row, so the race tests above pass either way —
    this is what pins *which* branch a real database takes, and therefore that
    an upgrade of a used deployment keeps the key its in-flight tokens were
    signed with.
    """
    await _clear_stored_keys()

    async with get_db_session() as session:
        has_runs = (await session.execute(text("SELECT EXISTS (SELECT 1 FROM runs)"))).scalar_one()

    with patch.object(settings, "token_signing_key", ""):
        await _init_once()

    expected = (
        token_signing.PROVENANCE_DATABASE_URL if has_runs else token_signing.PROVENANCE_GENERATED
    )
    assert token_signing.get_provenance() == expected

    async with get_db_session() as session:
        stored = (
            await session.execute(text("SELECT provenance FROM token_signing_keys"))
        ).scalar_one()
    assert stored == expected

    if has_runs:
        import hashlib

        assert (
            token_signing.get_token_signing_key()
            == hashlib.sha256(str(settings.database_url).encode()).digest()
        ), "an upgrade must adopt the exact key its in-flight tokens use"


async def test_a_supplied_key_does_not_create_a_row(app):
    """Bring-your-own must leave the table alone, against a real database."""
    await _clear_stored_keys()

    supplied = "Zq3+9xKcHn0vYt7LpR2wJdF8sMbQe5oUaTgXzN1iCv4="
    with patch.object(settings, "token_signing_key", supplied):
        key = await _init_once()

    import hashlib

    assert key == hashlib.sha256(supplied.encode()).digest()
    assert token_signing.get_provenance() == token_signing.PROVENANCE_CONFIG
    assert await _row_count() == 0, (
        "the configured path wrote a row; storing a supplied key would make the "
        "first value win for ever and silently ignore a later rotation"
    )
