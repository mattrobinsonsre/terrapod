"""The migrated schema must agree with the ORM models (#2189).

Integration tier deliberately, because the defect this exists to catch is
invisible everywhere else: the rest of the integration tier builds its tables
from `Base.metadata` rather than by running Alembic, so the models and the
migration chain can disagree indefinitely and every test still passes.

That is not hypothetical. Carrying the 1.11 release line to `main` inserted
`7a27d4747b9e` (apply-only variables) at the point that line branched, which is
*before* `2ff6b78f7163` (#1898, category in the variable identity). The later
revision drops and recreates both variable constraints, and recreated them
without `apply_only` -- so a 2.0 database migrated from scratch carried a
three-column constraint while the models declared four, and an apply-only
variable could not share a key with a standard one. The whole of #2189, dead at
the database level.

The E2E suite *did* catch it, because its stack is the only one that builds its
database by running this chain -- but it surfaced as a `409` from a
variable-create helper, which reads as the API wrongly refusing a legitimate pair
rather than as the chain being wrong. This gate names the cause instead, and it
fails in seconds where E2E needs a whole stack.

The first test is derived from `Base.metadata`, so it covers every named unique
constraint rather than the two that happened to break. The second is the
contrast the first cannot express: a database that stopped at a release line's
head must reach the same schema as one migrated from nothing, which is the
property an operator's upgrade actually depends on.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import UniqueConstraint, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from terrapod.db.models import Base
from tests.db.test_migration_contract import _versions_dir

pytestmark = pytest.mark.integration

#: The head of `release/v1.11`. A deployment on that line stops exactly here, so
#: this is the point an upgrade to a future 2.0 resumes from.
_RELEASE_1_11_HEAD = "7a27d4747b9e"

#: Named unique constraints the models declare. A matcher that stops matching
#: must fail rather than pass on nothing.
_MIN_CONSTRAINTS = 25


def _alembic(url: str):
    # Imported here, not at module level: ruff sorts `alembic` as third-party
    # locally but first-party in CI's container (where the repo's alembic/
    # directory sits beside the code), so a top-level import cannot satisfy both.
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_versions_dir().parent))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _model_unique_constraints() -> dict[str, frozenset[str]]:
    """Every NAMED unique constraint the ORM declares, as name -> column names.

    Derived rather than listed, so a constraint added later is covered without
    anyone remembering this file. Unnamed constraints are skipped: there is no
    reliable way to pair them with a `pg_constraint` row, and in this schema
    every one that matters carries a name.
    """
    out: dict[str, frozenset[str]] = {}
    for table in Base.metadata.tables.values():
        for c in table.constraints:
            if isinstance(c, UniqueConstraint) and c.name:
                out[str(c.name)] = frozenset(col.name for col in c.columns)
    return out


async def _db_unique_constraints(engine, names: set[str]) -> dict[str, frozenset[str]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conname, a.attname "
                    "FROM pg_constraint c "
                    "JOIN unnest(c.conkey) AS k(attnum) ON true "
                    "JOIN pg_attribute a "
                    "  ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                    "WHERE c.contype = 'u' AND c.conname = ANY(:names)"
                ),
                {"names": sorted(names)},
            )
        ).all()
    found: dict[str, set[str]] = {}
    for name, col in rows:
        found.setdefault(name, set()).add(col)
    return {k: frozenset(v) for k, v in found.items()}


async def _throwaway_database(monkeypatch) -> tuple[str, object]:
    from terrapod.config import settings

    # Alembic must build the schema from the real files, not inherit the
    # session's database or the tables the conftest created from the models.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    base = make_url(str(settings.database_url))
    name = f"tp_schema_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        # The name is this test's own generated identifier, not input.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    url = base.set(database=name).render_as_string(hide_password=False)
    return url, base


async def _drop_database(base, url) -> None:
    name = make_url(url).database
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        # Same generated identifier as above.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest.mark.asyncio
async def test_every_named_unique_constraint_matches_its_model(app, monkeypatch):
    """A fully migrated database agrees with `Base.metadata` on every constraint.

    Column *membership* is the property, not column order: a missing or extra
    column is a correctness defect, a reordering is not.
    """
    expected = _model_unique_constraints()
    assert len(expected) >= _MIN_CONSTRAINTS, (
        f"only found {len(expected)} named unique constraints in the models; "
        "the matcher has stopped matching rather than the schema having shrunk"
    )

    url, base = await _throwaway_database(monkeypatch)
    engine = create_async_engine(url)
    from alembic import command  # see _alembic for why this is not at module level

    try:
        await asyncio.to_thread(command.upgrade, _alembic(url), "head")
        actual = await _db_unique_constraints(engine, set(expected))

        missing = sorted(set(expected) - set(actual))
        assert not missing, f"declared by a model, absent from the migrated schema: {missing}"

        disagree = {
            name: {"model": sorted(expected[name]), "migrated": sorted(actual[name])}
            for name in expected
            if expected[name] != actual[name]
        }
        assert not disagree, (
            "the migration chain and the models disagree about these unique "
            f"constraints: {disagree}"
        )
    finally:
        await engine.dispose()
        await _drop_database(base, url)


@pytest.mark.asyncio
async def test_a_release_line_database_reaches_the_same_schema_as_a_fresh_one(app, monkeypatch):
    """Stopping at the 1.11 head then upgrading must land where a fresh run lands.

    This is the contrast, and it is what the derived test above cannot see: both
    databases end up at `head`, so a chain that is only correct when run from
    nothing passes the first test and fails here. An operator upgrading from a
    release line runs exactly this path and nothing else covered it.
    """
    names = set(_model_unique_constraints())

    url, base = await _throwaway_database(monkeypatch)
    engine = create_async_engine(url)
    from alembic import command

    try:
        cfg = _alembic(url)
        await asyncio.to_thread(command.upgrade, cfg, _RELEASE_1_11_HEAD)

        # At the release line's head `category` is not part of a variable's
        # identity -- that is `main`-only (#1898) -- but `apply_only` is.
        at_release = await _db_unique_constraints(engine, names)
        assert at_release["uq_variables_workspace_key"] == frozenset(
            {"workspace_id", "key", "apply_only"}
        )
        assert at_release["uq_variable_set_variables"] == frozenset(
            {"variable_set_id", "key", "apply_only"}
        )

        await asyncio.to_thread(command.upgrade, cfg, "head")
        upgraded = await _db_unique_constraints(engine, names)
    finally:
        await engine.dispose()
        await _drop_database(base, url)

    fresh_url, fresh_base = await _throwaway_database(monkeypatch)
    fresh_engine = create_async_engine(fresh_url)
    try:
        await asyncio.to_thread(command.upgrade, _alembic(fresh_url), "head")
        fresh = await _db_unique_constraints(fresh_engine, names)
    finally:
        await fresh_engine.dispose()
        await _drop_database(fresh_base, fresh_url)

    differ = {
        name: {"from 1.11": sorted(upgraded.get(name, ())), "fresh": sorted(fresh.get(name, ()))}
        for name in set(upgraded) | set(fresh)
        if upgraded.get(name) != fresh.get(name)
    }
    assert not differ, (
        f"a database upgraded from the 1.11 head does not match one migrated from nothing: {differ}"
    )
