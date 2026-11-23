"""The storage-location migration: its partial index, and a downgrade that refuses to lose a location."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrated_schema_matches_models import (
    _alembic,
    _drop_database,
    _throwaway_database,
)

pytestmark = pytest.mark.integration

_REVISION = "0e1c85ce67d0"
_BEFORE = "33575ebac4ef"


async def _scalar(engine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one_or_none()


async def test_the_downgrade_refuses_while_a_workspace_names_a_location(app, monkeypatch):
    from alembic import command  # see _alembic for why this is not at module level

    url, base = await _throwaway_database(monkeypatch)
    engine = create_async_engine(url)
    try:
        cfg = _alembic(url)
        await asyncio.to_thread(command.upgrade, cfg, _REVISION)
        index = await _scalar(
            engine,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_workspaces_storage_location'",
        )
        assert index and "WHERE (storage_location IS NOT NULL)" in index

        # RAW SQL, not the ORM. The database is pinned at `_REVISION` while
        # `Workspace` is the CURRENT model, so every column a later migration adds
        # breaks this insert -- `ssh_host_key_policy` (#2592) did exactly that, with
        # an `UndefinedColumnError` naming a column this test has no opinion about.
        # At this revision `id` and `name` are the only NOT NULL columns without a
        # default, so naming three columns is immune to whatever arrives later.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, storage_location) VALUES "
                    "(gen_random_uuid(), 'named', 'eu-west'), "
                    "(gen_random_uuid(), 'plain', NULL)"
                )
            )
        with pytest.raises(Exception, match="1 workspace"):
            await asyncio.to_thread(command.downgrade, cfg, _BEFORE)
        assert await _scalar(engine, "SELECT version_num FROM alembic_version") == _REVISION

        async with engine.begin() as conn:
            await conn.execute(text("UPDATE workspaces SET storage_location = NULL"))
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE)
        column = await _scalar(
            engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'workspaces' AND column_name = 'storage_location'",
        )
        assert column is None
    finally:
        await engine.dispose()
        await _drop_database(base, url)
