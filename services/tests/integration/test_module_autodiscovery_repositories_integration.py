"""Per-repository module autodiscovery state against real Postgres (#1620).

Integration tier deliberately, for what a mock cannot show:

- the migration itself, run by Alembic against a real database: a rule saved
  at the previous head is backfilled into exactly one state row carrying its
  scan state, and the downgrade takes the table and the new rule columns away
  again with the rule intact;
- the constraints: a rule's rows go when the rule does (CASCADE), one row per
  path, one row per provider id — but any number with no id yet;
- JSONB round-trips of the state's lists;
- the poller and a scan keeping the row and the rule's own columns in step,
  and `load_repositories` filling in a rule loaded without its rows.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    VCSConnection,
)
from terrapod.db.session import get_db_session
from terrapod.services import module_autodiscovery_service as svc
from tests.db.test_migration_contract import _versions_dir
from tests.integration.conftest import AUTH, admin_user, set_auth
from tests.integration.test_module_autodiscovery_integration import (
    PATHS,
    _poll,
    _repo,
    _seed_rule,
    _vcs,
)

pytestmark = pytest.mark.integration

_PREVIOUS_HEAD = "a7c3e91f52d4"
_THIS = "f9b3aac00aac"


# ── The migration ─────────────────────────────────────────────────────────


def _alembic(url: str):
    # Imported here, not at module level: ruff sorts `alembic` as third-party
    # locally but first-party in CI's container (where the repo's alembic/
    # directory sits beside the code), so a top-level import cannot satisfy both.
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_versions_dir().parent))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


_FILLERS = {
    "uuid": lambda: uuid.uuid4(),
    "character varying": lambda: "x",
    "text": lambda: "x",
    "integer": lambda: 0,
    "bigint": lambda: 0,
    "boolean": lambda: False,
    "timestamp with time zone": lambda: datetime.now(UTC),
}


async def _insert_minimal(conn, table: str, values: dict) -> None:
    """Insert a row giving every NOT NULL column without a default a value.

    The schema is whatever the migrations built at that revision, not the
    current models, so the required columns are read from the database.
    """
    required = (
        await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND is_nullable = 'NO' AND column_default IS NULL"
            ),
            {"t": table},
        )
    ).all()
    row = dict(values)
    types: dict[str, str] = {}
    for name, dtype in required:
        types[name] = dtype
        if name not in row:
            row[name] = "[]" if dtype == "jsonb" else _FILLERS[dtype]()
    params = [f"CAST(:{n} AS jsonb)" if types.get(n) == "jsonb" else f":{n}" for n in row]
    # Column names come from information_schema and the test's own literals.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    sql = f'INSERT INTO "{table}" ({", ".join(row)}) VALUES ({", ".join(params)})'
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
    await conn.execute(text(sql), row)


@pytest.mark.asyncio
async def test_the_migration_backfills_one_row_per_rule_and_downgrades_cleanly(app, monkeypatch):
    from terrapod.config import settings

    # A throwaway database, so Alembic builds the schema from the real files.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    base = make_url(str(settings.database_url))
    name = f"tp_mig_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        # The name is this test's own generated identifier, not input.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(url)
    cfg = _alembic(url)
    from alembic import command  # see _alembic for why this is not at module level

    try:
        await asyncio.to_thread(command.upgrade, cfg, _PREVIOUS_HEAD)

        gh, gl = uuid.uuid4(), uuid.uuid4()
        scanned, fresh, nested = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        first = datetime(2026, 9, 1, 12, tzinfo=UTC)
        async with engine.begin() as conn:
            await _insert_minimal(
                conn,
                "vcs_connections",
                {
                    "id": gh,
                    "provider": "github",
                    "name": "gh",
                    "server_url": "",
                    "github_installation_id": 101,
                },
            )
            await _insert_minimal(
                conn,
                "vcs_connections",
                {
                    "id": gl,
                    "provider": "gitlab",
                    "name": "gl",
                    "server_url": "https://git.example.com/gitlab",
                    "github_installation_id": 102,
                },
            )
            now = datetime.now(UTC)
            for rid, conn_id, repo_url, first_scan, sha, seen in (
                (
                    scanned,
                    gh,
                    "https://github.com/org/terraform-aws-vpc.git",
                    first,
                    "s1",
                    ["", "modules/a"],
                ),
                (fresh, gh, "https://github.com/org/terraform-aws-new", None, "", []),
                (
                    nested,
                    gl,
                    "https://git.example.com/gitlab/g/sub/terraform-aws-x/",
                    first,
                    "s2",
                    ["modules/b"],
                ),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO module_autodiscovery_rules (id, vcs_connection_id, "
                        "repo_url, pattern, name, first_scan_at, last_scanned_sha, "
                        "seen_subdirectories, created_at, updated_at) VALUES (:id, :c, :u, "
                        "'**/*.tf', :n, :f, :s, CAST(:seen AS jsonb), :now, :now)"
                    ),
                    {
                        "id": rid,
                        "c": conn_id,
                        "u": repo_url,
                        "n": f"r-{rid.hex[:6]}",
                        "f": first_scan,
                        "s": sha,
                        "seen": json.dumps(seen),
                        "now": now,
                    },
                )

        await asyncio.to_thread(command.upgrade, cfg, _THIS)

        async with engine.connect() as conn:
            rows = {
                r.rule_id: r
                for r in (
                    await conn.execute(
                        text(
                            "SELECT rule_id, repo_path, repo_url, origin, status, "
                            "last_scanned_sha, seen_subdirectories, first_seen_at, "
                            "vcs_repo_id, candidates, previous_paths "
                            "FROM module_autodiscovery_repositories"
                        )
                    )
                ).all()
            }
            rules = {
                r.id: r
                for r in (
                    await conn.execute(
                        text(
                            "SELECT id, target_kind, target_id, last_error, last_enumerated_at "
                            "FROM module_autodiscovery_rules"
                        )
                    )
                ).all()
            }
        assert set(rows) == {scanned, fresh, nested}
        assert rows[scanned].repo_path == "org/terraform-aws-vpc"
        assert rows[scanned].repo_url == "https://github.com/org/terraform-aws-vpc.git"
        assert (rows[scanned].origin, rows[scanned].status) == ("baseline", "active")
        assert rows[scanned].last_scanned_sha == "s1"
        assert rows[scanned].seen_subdirectories == ["", "modules/a"]
        assert rows[scanned].first_seen_at == first
        assert rows[scanned].vcs_repo_id == "" and rows[scanned].candidates == []
        assert rows[fresh].last_scanned_sha == "" and rows[fresh].seen_subdirectories == []
        # The GitLab instance's relative URL root is not part of the path.
        assert rows[nested].repo_path == "g/sub/terraform-aws-x"
        assert rows[nested].seen_subdirectories == ["modules/b"]
        for r in rules.values():
            assert (r.target_kind, r.target_id, r.last_error) == ("repository", "", "")
            assert r.last_enumerated_at is None

        await asyncio.to_thread(command.downgrade, cfg, _PREVIOUS_HEAD)

        async with engine.connect() as conn:
            table = (
                await conn.execute(text("SELECT to_regclass('module_autodiscovery_repositories')"))
            ).scalar_one()
            columns = {
                c
                for (c,) in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'module_autodiscovery_rules'"
                        )
                    )
                ).all()
            }
            kept = (
                await conn.execute(
                    text("SELECT last_scanned_sha FROM module_autodiscovery_rules WHERE id = :id"),
                    {"id": scanned},
                )
            ).scalar_one()
        assert table is None
        assert not {"target_kind", "target_id", "last_enumerated_at", "last_error"} & columns
        assert kept == "s1"
    finally:
        await engine.dispose()
        async with admin.connect() as c:
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


# ── Constraints and JSONB ─────────────────────────────────────────────────


async def _add_rows(rule_id: uuid.UUID, *rows: dict) -> None:
    async with get_db_session() as db:
        for fields in rows:
            db.add(ModuleAutodiscoveryRepository(rule_id=rule_id, **fields))
        await db.commit()


@pytest.mark.asyncio
async def test_deleting_the_rule_deletes_its_repository_rows(app):
    _, rule_id = await _seed_rule(uuid.uuid4().hex[:8])
    await _add_rows(rule_id, {"repo_path": "o/a"}, {"repo_path": "o/b"})
    async with get_db_session() as db:
        await db.delete(await db.get(ModuleAutodiscoveryRule, rule_id))
        await db.commit()
    async with get_db_session() as db:
        left = (
            await db.execute(
                select(ModuleAutodiscoveryRepository).where(
                    ModuleAutodiscoveryRepository.rule_id == rule_id
                )
            )
        ).all()
    assert left == []


@pytest.mark.asyncio
async def test_one_row_per_path_and_per_provider_id_but_any_number_without_an_id(app):
    _, rule_id = await _seed_rule(uuid.uuid4().hex[:8])
    # Two rows with no provider id yet are fine: the id index is partial.
    await _add_rows(rule_id, {"repo_path": "o/a"}, {"repo_path": "o/b"})
    with pytest.raises(IntegrityError):
        await _add_rows(rule_id, {"repo_path": "o/a"})
    await _add_rows(rule_id, {"repo_path": "o/c", "vcs_repo_id": "42"})
    with pytest.raises(IntegrityError):
        await _add_rows(rule_id, {"repo_path": "o/d", "vcs_repo_id": "42"})
    # The same id under another rule is another rule's business.
    _, other_rule = await _seed_rule(uuid.uuid4().hex[:8])
    await _add_rows(other_rule, {"repo_path": "o/c", "vcs_repo_id": "42"})


@pytest.mark.asyncio
async def test_the_state_lists_round_trip_through_jsonb(app):
    _, rule_id = await _seed_rule(uuid.uuid4().hex[:8])
    state = {
        "seen_subdirectories": ["", "modules/a"],
        "candidates": [{"subdirectory": "modules/a", "name": "a", "provider": "aws"}],
        "last_skips": [{"subdirectory": "", "reason": "name-taken"}],
        "previous_paths": [{"path": "o/old", "url": "https://github.com/o/old"}],
    }
    await _add_rows(rule_id, {"repo_path": "o/a", **state})
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(ModuleAutodiscoveryRepository).where(
                    ModuleAutodiscoveryRepository.rule_id == rule_id
                )
            )
        ).scalar_one()
    for key, value in state.items():
        assert getattr(row, key) == value, key
    assert (row.origin, row.status, row.failure_count) == ("baseline", "active", 0)


# ── Kept in step ──────────────────────────────────────────────────────────


async def _state(rule_id: uuid.UUID) -> list[ModuleAutodiscoveryRepository]:
    async with get_db_session() as db:
        rows = await db.execute(
            select(ModuleAutodiscoveryRepository).where(
                ModuleAutodiscoveryRepository.rule_id == rule_id
            )
        )
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_polling_creates_the_row_and_keeps_it_in_step_with_the_rule(app):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)
    with _vcs({tag: ("s1", PATHS)}):
        assert await _poll() == 0
    (row,) = await _state(rule_id)
    assert row.repo_path == f"org/terraform-azurerm-mg{tag}" and row.repo_url == _repo(tag)
    assert (row.last_scanned_sha, row.default_branch) == ("s1", "main")
    assert row.seen_subdirectories == ["", "modules/a", "modules/b"]

    with _vcs({tag: ("s2", [*PATHS, "modules/c/main.tf"])}):
        assert await _poll() == 1
    (row,) = await _state(rule_id)
    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        assert rule.last_scanned_sha == row.last_scanned_sha == "s2"
        assert rule.seen_subdirectories == row.seen_subdirectories
    assert "modules/c" in row.seen_subdirectories


@pytest.mark.asyncio
async def test_load_repositories_fills_in_a_rule_loaded_without_them(app):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)
    with _vcs({tag: ("s1", PATHS)}):
        await _poll()
    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        rows = await svc.load_repositories(db, rule)
        assert [r.repo_path for r in rows] == [f"org/terraform-azurerm-mg{tag}"]
        # And the rule's own collection is now usable without a query.
        assert svc.repository_state(rule) is rows[0]


@pytest.mark.asyncio
async def test_a_rebaselining_patch_deletes_the_state_rows(app, client):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)
    with _vcs({tag: ("s1", PATHS)}):
        await _poll()
    assert len(await _state(rule_id)) == 1

    set_auth(app, admin_user())
    body = {"data": {"attributes": {"pattern": "modules/**"}}}
    resp = await client.patch(
        f"/api/terrapod/v1/module-autodiscovery-rules/{rule_id}", json=body, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert await _state(rule_id) == []
    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        assert rule.first_scan_at is None and rule.last_scanned_sha == ""


@pytest.mark.asyncio
async def test_a_connection_can_hold_rules_that_share_a_repository_path(app):
    # Rows are per rule: two rules looking at one repository each keep their own.
    tag = uuid.uuid4().hex[:8]
    conn_id, first = await _seed_rule(tag)
    async with get_db_session() as db:
        second = ModuleAutodiscoveryRule(
            vcs_connection_id=conn_id, repo_url=_repo(tag), pattern="modules/**", name=f"b-{tag}"
        )
        db.add(second)
        await db.commit()
        second_id = second.id
    with _vcs({tag: ("s1", PATHS)}):
        await _poll()
    assert len(await _state(first)) == 1 and len(await _state(second_id)) == 1
    async with get_db_session() as db:
        assert await db.get(VCSConnection, conn_id) is not None
