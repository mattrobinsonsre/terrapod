"""Integration test for the multi-replica CA-init race (#1060).

`init_ca` must be safe when multiple API replicas start against a fresh
(CA-less) database at once: exactly one CA row may be created, and every caller
must load the *same* CA. Before the advisory-lock fix, two concurrent callers
each saw "no CA", each generated one, and each inserted a row — leaving replicas
caching different CAs and permanently disagreeing (listener certs then fail with
"Certificate not signed by this CA").

Requires a real Postgres engine (advisory locks are a Postgres feature), so this
lives in the integration tier.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from terrapod.auth import ca as ca_module
from terrapod.auth.ca import get_certificate_fingerprint, init_ca
from terrapod.db.session import get_db_session


async def _run_init_ca() -> str:
    """Run init_ca on its own session/connection and return the CA fingerprint."""
    async with get_db_session() as session:
        ca = await init_ca(session)
    return get_certificate_fingerprint(ca.ca_cert)


async def test_concurrent_init_ca_creates_single_ca(app):
    """Two concurrent init_ca calls on a fresh DB must yield ONE CA row.

    Reproduces the #1060 race: without the advisory lock, both callers insert a
    row (2 rows, divergent CAs). With the fix, the second caller blocks on the
    advisory lock, then loads the row the first created — one row, identical CA.
    """
    # Start from a clean slate: no CA row, no cached singleton.
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM certificate_authority"))
        await session.commit()
    ca_module._ca = None

    # Fire N concurrent initializers, each on its own connection — the scenario
    # of N API replicas starting together.
    fingerprints = await asyncio.gather(*[_run_init_ca() for _ in range(4)])

    # Exactly one CA row exists...
    async with get_db_session() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM certificate_authority"))
        ).scalar_one()
    assert count == 1, f"expected exactly one CA row, found {count} (multi-replica race)"

    # ...and every caller loaded the identical CA.
    assert len(set(fingerprints)) == 1, f"replicas disagree on CA: {set(fingerprints)}"


async def test_init_ca_is_idempotent_when_ca_exists(app):
    """A second init_ca against an existing CA loads it — no new row, same CA."""
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM certificate_authority"))
        await session.commit()
    ca_module._ca = None

    first = await _run_init_ca()
    second = await _run_init_ca()

    async with get_db_session() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM certificate_authority"))
        ).scalar_one()
    assert count == 1
    assert first == second
