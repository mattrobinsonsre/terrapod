"""Migration smoke: seed + query the ALEMBIC-built schema (#544).

The integration test tier builds the schema with ``Base.metadata.create_all``, so
the migration files are never executed by any test. Alembic guarantees the
*upgrade path* works; it does not guarantee a given ``upgrade()`` is correct
against **non-empty** data — a bad backfill, a NOT-NULL-without-default added to a
populated table, or a rename that drops a column would pass CI and only fail in
prod.

This script runs AFTER ``alembic upgrade head`` against a real Postgres: it seeds
representative, FK-linked rows using the ORM models and reads them back. Because
it inserts minimal rows and relies on column defaults for the rest, a migration
that adds a required column without a default (the classic footgun) makes this
fail loudly — which is the point.

Usage (DB already migrated to head):
    DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db \
        python scripts/migration_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid


async def _main() -> int:
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import terrapod.db.models as m

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session() as db:
            # The DB must already be at an Alembic head — the migration Job/step
            # runs before this script.
            current = (
                await db.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            if not current:
                print(
                    "alembic_version empty — migrations were not applied",
                    file=sys.stderr,
                )
                return 1

            suffix = uuid.uuid4().hex[:10]
            user = m.User(
                email=f"smoke-{suffix}@example.com", display_name="Migration Smoke"
            )
            db.add(user)
            ws = m.Workspace(name=f"smoke-ws-{suffix}", owner_email=user.email)
            db.add(ws)
            await db.commit()

            # Read the seeded rows back through the ORM — proves the model's
            # column definitions match the migrated schema (a dropped/renamed
            # column would 500 here, exactly as the app would in prod).
            got = (
                await db.execute(select(m.Workspace).where(m.Workspace.name == ws.name))
            ).scalar_one()
            assert got.owner_email == user.email, (
                "workspace owner_email round-trip mismatch"
            )
            assert got.terraform_version, "expected a defaulted terraform_version"
            n_users = (
                await db.execute(select(func.count()).select_from(m.User))
            ).scalar_one()
            assert n_users >= 1
    finally:
        await engine.dispose()

    print(
        f"migration smoke OK — schema at {current}; seeded + queried a user + workspace "
        f"against the migrated schema"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
