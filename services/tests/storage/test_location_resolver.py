"""The database-backed location resolver."""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import Workspace
from terrapod.storage import location_resolver as lr
from terrapod.storage.location_resolver import DbLocationResolver, remember_flushed_workspaces
from terrapod.storage.protocol import ObjectStoreError

WS = "0192f3a1-0000-7000-8000-00000000000a"
WS_2 = "0192f3a1-0000-7000-8000-00000000000b"
WS_3 = "0192f3a1-0000-7000-8000-00000000000c"


class FakeDb:
    """Answers the resolver's query from a dict of workspace id -> column value."""

    def __init__(self, rows: dict[str, str | None]) -> None:
        self.rows = rows
        self.queries = 0

    @contextlib.asynccontextmanager
    async def session(self):
        db = AsyncMock()

        async def execute(stmt):
            self.queries += 1
            wanted = str(stmt.whereclause.right.value)
            result = MagicMock()
            result.one_or_none.return_value = (self.rows[wanted],) if wanted in self.rows else None
            return result

        db.execute = execute
        yield db


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb({WS: None, WS_2: "eu-west", WS_3: "us-east"})
    monkeypatch.setattr(lr, "get_db_session", fake.session)
    return fake


async def test_a_null_column_is_the_default_location(db):
    assert await DbLocationResolver().location_for(WS) == "default"


async def test_a_named_location_is_returned(db):
    assert await DbLocationResolver().location_for(WS_2) == "eu-west"


async def test_a_known_workspace_is_cached(db):
    resolver = DbLocationResolver()
    for _ in range(3):
        assert await resolver.location_for(WS_2) == "eu-west"
    assert db.queries == 1


async def test_an_unknown_workspace_is_none_and_not_cached(db):
    resolver = DbLocationResolver()
    ghost = str(uuid.uuid4())
    assert await resolver.location_for(ghost) is None
    db.rows[ghost] = "eu-west"
    assert await resolver.location_for(ghost) == "eu-west"
    assert db.queries == 2


@pytest.mark.parametrize(
    "bad",
    ["", "backups", "pulumi-checkpoints", WS.upper(), WS.replace("-", ""), f"{{{WS}}}"],
)
async def test_an_id_that_is_not_a_canonical_uuid_is_unknown(db, bad):
    assert await DbLocationResolver().location_for(bad) is None
    assert db.queries == 0


@pytest.mark.parametrize(
    "error", [RuntimeError("Database not initialized"), OSError("connection refused")]
)
async def test_a_database_error_is_an_object_store_error(monkeypatch, error):
    @contextlib.asynccontextmanager
    async def broken():
        raise error
        yield

    monkeypatch.setattr(lr, "get_db_session", broken)
    with pytest.raises(ObjectStoreError, match=WS) as caught:
        await DbLocationResolver().location_for(WS)
    assert caught.value.__cause__ is error


async def test_a_query_error_is_an_object_store_error(monkeypatch):
    @contextlib.asynccontextmanager
    async def session():
        db = AsyncMock()
        db.execute.side_effect = TimeoutError("statement timeout")
        yield db

    monkeypatch.setattr(lr, "get_db_session", session)
    resolver = DbLocationResolver()
    with pytest.raises(ObjectStoreError):
        await resolver.location_for(WS)
    assert not resolver._known


async def test_the_cache_is_bounded_and_evicts_the_least_recent(db):
    resolver = DbLocationResolver(max_entries=2)
    await resolver.location_for(WS)
    await resolver.location_for(WS_2)
    await resolver.location_for(WS)
    await resolver.location_for(WS_3)
    assert list(resolver._known) == [WS, WS_3]
    await resolver.location_for(WS_2)
    assert db.queries == 4


def test_a_flushed_new_workspace_is_remembered_before_it_commits():
    resolver = DbLocationResolver()
    eu, home = uuid.uuid4(), uuid.uuid4()
    session = SimpleNamespace(
        new=[
            Workspace(id=eu, name="a", storage_location="eu-west"),
            Workspace(id=home, name="b"),
            SimpleNamespace(id=uuid.uuid4()),
        ]
    )
    remember_flushed_workspaces(session, None)
    assert resolver._known == {str(eu): "eu-west", str(home): "default"}


async def test_a_remembered_workspace_needs_no_query(db):
    resolver = DbLocationResolver()
    new = str(uuid.uuid4())
    resolver.remember(new, "us-east")
    assert await resolver.location_for(new) == "us-east"
    assert db.queries == 0


class _PreMigration(Exception):
    """What SQLAlchemy raises for a missing column: the driver error carries the sqlstate."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.orig = SimpleNamespace(sqlstate=sqlstate)


class TestTheStartupCheck:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(lr, "_unchecked", None)

    @pytest.fixture
    def rows(self, monkeypatch):
        state = SimpleNamespace(named=[], error=None)

        @contextlib.asynccontextmanager
        async def session():
            if state.error is not None:
                raise state.error
            db = AsyncMock()
            result = MagicMock()
            result.all.return_value = state.named
            db.execute = AsyncMock(return_value=result)
            yield db

        monkeypatch.setattr(lr, "get_db_session", session)
        return state

    @pytest.fixture
    def configured(self, monkeypatch):
        from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings

        def _set(*names: str) -> None:
            loc = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="b"))
            monkeypatch.setattr(settings.storage, "locations", dict.fromkeys(names, loc))

        _set()
        return _set

    async def test_a_default_deployment_starts_and_is_ready(self, rows, configured):
        await lr.check_locations_at_startup()
        assert await lr.locations_readiness() == "healthy"

    async def test_rows_naming_a_location_with_none_configured_refuse_to_start(
        self, rows, configured, monkeypatch
    ):
        rows.named.append(("eu-west", 3))
        log = MagicMock()
        monkeypatch.setattr(lr, "logger", log)
        with pytest.raises(RuntimeError, match="none are configured") as refused:
            await lr.check_locations_at_startup()
        assert "re-add" in str(refused.value) and "api.config.storage.locations" in str(
            refused.value
        )
        assert log.error.call_args.kwargs["locations"] == {"eu-west": 3}

    async def test_rows_naming_an_unconfigured_location_refuse_to_start(self, rows, configured):
        configured("us-east")
        rows.named.extend([("us-east", 1), ("eu-west", 2)])
        with pytest.raises(RuntimeError, match="not configured: eu-west; re-add"):
            await lr.check_locations_at_startup()

    async def test_rows_naming_configured_locations_start(self, rows, configured):
        configured("eu-west", "us-east")
        rows.named.extend([("us-east", 1), ("eu-west", 2)])
        await lr.check_locations_at_startup()

    @pytest.mark.parametrize("sqlstate", ["42703", "42P01"])
    async def test_a_database_before_the_migration_passes(self, rows, configured, sqlstate):
        rows.error = _PreMigration(sqlstate)
        await lr.check_locations_at_startup()
        assert await lr.locations_readiness() == "healthy"

    @pytest.mark.parametrize(
        "error", [OSError("connection refused"), _PreMigration("57P01"), RuntimeError("x")]
    )
    async def test_a_check_that_could_not_run_holds_readiness_until_one_passes(
        self, rows, configured, monkeypatch, error
    ):
        rows.error = error
        log = MagicMock()
        monkeypatch.setattr(lr, "logger", log)
        await lr.check_locations_at_startup()
        assert log.warning.called and not log.error.called
        assert (await lr.locations_readiness()).startswith("unchecked")

        rows.error = None
        assert await lr.locations_readiness() == "healthy"
        rows.error = error
        assert await lr.locations_readiness() == "healthy"

    async def test_rows_found_by_a_later_check_keep_the_node_unready(self, rows, configured):
        rows.error = OSError("connection refused")
        await lr.check_locations_at_startup()
        rows.error = None
        rows.named.append(("eu-west", 1))
        assert "none are configured" in await lr.locations_readiness()
        assert "none are configured" in await lr.locations_readiness()


def test_the_api_runs_the_startup_check_once_the_database_and_storage_are_up():
    import ast
    import pathlib

    app_py = pathlib.Path(lr.__file__).parents[1] / "api" / "app.py"
    tree = ast.parse(app_py.read_text())
    lifespan = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    awaited = [
        node.value.value.func.id
        for node in lifespan.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and isinstance(node.value.value.func, ast.Name)
    ]
    assert "check_locations_at_startup" in awaited
    assert awaited.index("init_db") < awaited.index("check_locations_at_startup")
    assert awaited.index("init_storage") < awaited.index("check_locations_at_startup")
