"""Stored roles keep what they could do when `run:override` leaves `workspace:settings`.

A role stores a frozen capability list, so a new admin-tier capability reaches
built-in admin and owners through the tier but never a stored role. The
migration adds it to every role holding `workspace:settings`, and the downgrade
strips it so a rolled-back release does not meet a token it does not know.
"""

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from terrapod.auth import capabilities as cap
from tests.integration.test_migrated_schema_matches_models import (
    _alembic,
    _drop_database,
    _throwaway_database,
)

pytestmark = pytest.mark.integration

_REVISION = "80499ff0ef90"
_BEFORE = "b56cc921fd47"

_WITH_SETTINGS = sorted({cap.WORKSPACE_READ, cap.RUN_READ, cap.WORKSPACE_SETTINGS})
_WITHOUT_SETTINGS = sorted({cap.WORKSPACE_READ, cap.RUN_PLAN, cap.RUN_APPLY})
_ADMIN_PRESET = cap.expand_preset(
    workspace_permission="admin",
    pool_permission=None,
    registry_permission=None,
    catalog_permission=None,
)
# What a stored admin-preset role holds the day before the upgrade.
_LEGACY_ADMIN = sorted(set(_ADMIN_PRESET) - {cap.RUN_OVERRIDE})


async def _caps(engine) -> dict[str, list[str]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT name, capabilities FROM roles"))).all()
    return {name: sorted(caps) for name, caps in rows}


async def _seed(engine) -> None:
    async with engine.begin() as conn:
        for name, caps in (
            ("with-settings", _WITH_SETTINGS),
            ("without-settings", _WITHOUT_SETTINGS),
            ("legacy-admin", _LEGACY_ADMIN),
            ("empty", []),
        ):
            await conn.execute(
                text("INSERT INTO roles (name, capabilities) VALUES (:n, CAST(:c AS JSONB))"),
                {"n": name, "c": json.dumps(caps)},
            )


async def test_roles_holding_settings_gain_override_and_the_downgrade_strips_it(app, monkeypatch):
    from alembic import command  # see _alembic for why this is not at module level

    url, base = await _throwaway_database(monkeypatch)
    engine = create_async_engine(url)
    try:
        cfg = _alembic(url)
        await asyncio.to_thread(command.upgrade, cfg, _BEFORE)
        await _seed(engine)

        await asyncio.to_thread(command.upgrade, cfg, _REVISION)
        after = await _caps(engine)
        assert after["with-settings"] == sorted([*_WITH_SETTINGS, cap.RUN_OVERRIDE])
        assert after["legacy-admin"] == sorted([*_LEGACY_ADMIN, cap.RUN_OVERRIDE])
        assert after["without-settings"] == _WITHOUT_SETTINGS
        assert after["empty"] == []

        # The backfilled admin-preset role is still recognised as the admin preset.
        summary = cap.summarize_capabilities(after["legacy-admin"])
        assert summary["workspace_permission"] == "admin"

        # A downgrade then upgrade round trip lands on the same sets.
        await asyncio.to_thread(command.downgrade, cfg, _BEFORE)
        await asyncio.to_thread(command.upgrade, cfg, _REVISION)
        assert await _caps(engine) == after

        await asyncio.to_thread(command.downgrade, cfg, _BEFORE)
        back = await _caps(engine)
        assert back["with-settings"] == _WITH_SETTINGS
        assert back["legacy-admin"] == _LEGACY_ADMIN
        assert back["without-settings"] == _WITHOUT_SETTINGS
        assert all(cap.RUN_OVERRIDE not in caps for caps in back.values())
    finally:
        await engine.dispose()
        await _drop_database(base, url)


async def test_the_upgrade_is_idempotent_on_a_role_that_already_holds_override(app, monkeypatch):
    from alembic import command

    url, base = await _throwaway_database(monkeypatch)
    engine = create_async_engine(url)
    try:
        cfg = _alembic(url)
        await asyncio.to_thread(command.upgrade, cfg, _BEFORE)
        held = sorted([*_WITH_SETTINGS, cap.RUN_OVERRIDE])
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO roles (name, capabilities) VALUES ('has-it', CAST(:c AS JSONB))"),
                {"c": json.dumps(held)},
            )
        await asyncio.to_thread(command.upgrade, cfg, _REVISION)
        assert (await _caps(engine))["has-it"] == held
    finally:
        await engine.dispose()
        await _drop_database(base, url)
