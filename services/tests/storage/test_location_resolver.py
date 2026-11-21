"""The database-backed location resolver."""

from __future__ import annotations

import asyncio
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


class TestTheModulesMutableState:
    """Every piece of it is on `_node`, and nothing rebinds a module global.

    These were five module-level names. They are process-wide state written by
    one call and read by another, which is deliberate -- but it cost a `global`
    statement per writer, each needing a `# noqa: PLW0603`, and CodeQL read the
    rebinds as unused globals because no single function both writes and reads
    one (`py/unused-global-variable`). Both complaints were false and both would
    have kept returning: an alert dismissal is bound to one alert number, so the
    next edit to the file raises a fresh one (#2623).

    Holding them on one instance removes the construction both tools object to,
    so this asserts the construction rather than the alert -- there is nothing to
    silence, and a `global` reintroduced here fails by name.
    """

    def test_the_module_rebinds_no_global(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(lr))
        offenders = [
            f"{n.lineno}: global {', '.join(n.names)}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Global)
        ]
        assert offenders == [], (
            "this module's mutable state lives on `_node` so that no writer needs a "
            "`global` -- put the new field on the holder rather than beside it: "
            + "; ".join(offenders)
        )

    def test_every_mutable_field_is_on_the_holder(self):
        """The converse, so the holder cannot be bypassed by a mutable module default.

        A `global` is only one way back: a plain module-level dict or list is
        mutated in place without one, and would be invisible to the test above.
        Constants are fine and are what the UPPER_CASE convention marks.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(lr))
        offenders = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if not isinstance(t, ast.Name) or t.id.lstrip("_").isupper():
                    continue
                if isinstance(
                    node.value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp)
                ):
                    offenders.append(f"{node.lineno}: {t.id}")
        assert offenders == [], (
            "a mutable module-level default is state outside `_node` that no `global` "
            "check can see; put it on the holder: " + "; ".join(offenders)
        )

    def test_the_holder_carries_the_five_it_replaced(self):
        """Named, because losing one silently would turn a read into an AttributeError
        at the point of use rather than here."""
        assert {f.name for f in __import__("dataclasses").fields(lr._node)} == {
            "unchecked",
            "unreachable",
            "probed_at",
            "counted_at",
            "refresh_task",
        }


class TestTheStartupCheck:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(lr._node, "unchecked", None)
        monkeypatch.setattr(lr._node, "counted_at", None)

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
        assert await lr.locations_readiness() == (True, "healthy")

    async def test_a_row_arriving_after_startup_is_noticed_without_a_restart(
        self, rows, configured
    ):
        """The standby case, and the reason the counts check has its own clock.

        A node with no locations configured starts clean -- nothing names one
        yet -- so `_unchecked` goes to None. Readiness used to re-query only
        when a PREVIOUS check had failed, so a row replicated afterwards was
        never seen: the node stayed ready, and a promotion without a restart
        wrote that workspace's objects to the default store (#2621).
        """
        await lr.check_locations_at_startup()
        assert await lr.locations_readiness() == (True, "healthy")

        rows.named.append(("eu-west", 3))

        # Inside the window the last answer stands, deliberately: the TTL is
        # what stops /ready querying on every kubelet probe.
        assert (await lr.locations_readiness())[0] is True

        # Past it, with no restart and no prior failure to trigger the re-query.
        lr._node.counted_at = lr.time.monotonic() - lr._PROBE_TTL_SECONDS - 1
        ready, detail = await lr.locations_readiness()
        assert ready is False
        assert "eu-west" in detail

    async def test_readiness_owes_no_query_before_the_startup_check_has_run(self, rows, configured):
        """The TTL re-count is a REFRESH, so an unstamped process is not stale.

        Reading `_counted_at is None` as stale made the first readiness call in
        every process query the database. On a process where the startup check
        has not run -- a test, or one still booting -- that raises, and the raise
        was reported as `unchecked: RuntimeError`: readiness refused for the
        ABSENCE of a check rather than for its result, which is the opposite of
        what holding readiness is for.
        """
        rows.error = RuntimeError("Database not initialized")
        assert await lr.locations_readiness() == (True, "healthy")
        assert lr._node.unchecked is None

    async def test_the_startup_check_stamps_the_counts_clock(self, rows, configured):
        """Or the refresh above fires on the call right after startup.

        Startup has just done this count, so a readiness call a moment later
        must take the answer rather than repeat the query.
        """
        await lr.check_locations_at_startup()
        assert lr._node.counted_at is not None

        rows.error = RuntimeError("the clock was not stamped, so this was queried again")
        assert await lr.locations_readiness() == (True, "healthy")

    async def test_a_startup_count_that_raised_is_still_stamped(self, rows, configured):
        """A raise leaves `_unchecked` set, so the re-query runs on `pending`.

        Stamping only on success would leave the clock at None on exactly the
        path that needs re-checking, and `pending` -- not staleness -- is what
        carries it.
        """
        rows.error = RuntimeError("boom")
        await lr.check_locations_at_startup()
        assert lr._node.counted_at is not None
        assert lr._node.unchecked == "RuntimeError"

        rows.error = None
        assert await lr.locations_readiness() == (True, "healthy")

    async def test_rows_naming_a_location_with_none_configured_hold_readiness(
        self, rows, configured, monkeypatch
    ):
        """NOT a raise (#2621): a raise is a CrashLoopBackOff for every replica
        over one row, and a crash-looping pod cannot serve the detail below."""
        rows.named.append(("eu-west", 3))
        log = MagicMock()
        monkeypatch.setattr(lr, "logger", log)
        await lr.check_locations_at_startup()
        ready, detail = await lr.locations_readiness()
        assert ready is False
        assert "none are configured" in detail
        assert "re-add" in detail and "api.config.storage.locations" in detail
        assert log.error.call_args.kwargs["locations"] == {"eu-west": 3}

    async def test_the_refusal_is_not_raised(self, rows, configured):
        """Pinned on its own, because the whole point is the absence of a raise
        and a test that merely checks readiness would pass either way."""
        rows.named.append(("eu-west", 3))
        await lr.check_locations_at_startup()  # must not raise

    async def test_rows_naming_an_unconfigured_location_hold_readiness(self, rows, configured):
        configured("us-east")
        rows.named.extend([("us-east", 1), ("eu-west", 2)])
        await lr.check_locations_at_startup()
        ready, detail = await lr.locations_readiness()
        assert ready is False
        assert "not configured: eu-west; re-add" in detail

    async def test_rows_naming_configured_locations_start(self, rows, configured):
        configured("eu-west", "us-east")
        rows.named.extend([("us-east", 1), ("eu-west", 2)])
        await lr.check_locations_at_startup()

    @pytest.mark.parametrize("sqlstate", ["42703", "42P01"])
    async def test_a_database_before_the_migration_passes(self, rows, configured, sqlstate):
        rows.error = _PreMigration(sqlstate)
        await lr.check_locations_at_startup()
        assert await lr.locations_readiness() == (True, "healthy")

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
        ready, detail = await lr.locations_readiness()
        assert not ready and detail.startswith("unchecked")

        rows.error = None
        assert await lr.locations_readiness() == (True, "healthy")
        recovered = [c for c in log.info.call_args_list if "passed" in c.args[0]]
        assert len(recovered) == 1 and recovered[0].kwargs["earlier"]
        rows.error = error
        assert await lr.locations_readiness() == (True, "healthy")
        assert len([c for c in log.info.call_args_list if "passed" in c.args[0]]) == 1

    async def test_rows_found_by_a_later_check_keep_the_node_unready(self, rows, configured):
        rows.error = OSError("connection refused")
        await lr.check_locations_at_startup()
        rows.error = None
        rows.named.append(("eu-west", 1))
        for _ in range(2):
            ready, detail = await lr.locations_readiness()
            assert not ready and "none are configured" in detail


class TestResolutionCannotStarveThePool:
    """A cache miss resolves INSIDE a request that already holds a session, so it
    takes a second connection from the same pool. Warm that never happens; the
    cold window after a rollout is when every workspace is a first touch at
    once, and an unbounded storm races the requests waiting on it (#2621)."""

    async def test_concurrent_misses_never_hold_more_than_the_gate(self, monkeypatch):
        from terrapod.config import settings

        limit = max(2, settings.database.pool_size // 2)
        state = SimpleNamespace(live=0, peak=0)

        @contextlib.asynccontextmanager
        async def session():
            state.live += 1
            state.peak = max(state.peak, state.live)
            try:
                # Long enough that every admitted task overlaps; without the
                # gate they would all be in here together.
                await asyncio.sleep(0.02)
                db = AsyncMock()
                result = MagicMock()
                result.one_or_none.return_value = (None,)
                db.execute = AsyncMock(return_value=result)
                yield db
            finally:
                state.live -= 1

        monkeypatch.setattr(lr, "get_db_session", session)
        resolver = DbLocationResolver()
        ids = [f"0192f3a1-0000-7000-8000-{i:012d}" for i in range(limit * 4)]
        await asyncio.gather(*(resolver.location_for(i) for i in ids))

        assert state.peak <= limit
        # The control: without real overlap the assertion above is vacuous, and
        # a gate of one would satisfy it while serialising the whole fleet.
        assert state.peak > 1


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


class _ProbeStore:
    """A location store whose listing succeeds, fails, or hangs."""

    def __init__(self, error: BaseException | None = None, hang: bool = False) -> None:
        self.error, self.hang, self.listed = error, hang, 0

    async def list_prefix(self, prefix, *, after="", limit=None):
        self.listed += 1
        assert limit == 1 and prefix == "state/"
        if self.hang:
            await asyncio.sleep(10)
        if self.error is not None:
            raise self.error
        return []


class TestTheReachabilityProbe:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(lr._node, "unchecked", None)
        monkeypatch.setattr(lr._node, "unreachable", {})
        monkeypatch.setattr(lr._node, "probed_at", None)
        monkeypatch.setattr(lr._node, "refresh_task", None)
        monkeypatch.setattr(lr, "_PROBE_TIMEOUT_SECONDS", 0.2)

    @pytest.fixture
    def node(self, monkeypatch):
        """Locations eu-west and us-east, with rows counted per location."""
        import terrapod.storage as storage_mod
        from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings
        from terrapod.storage.routing import RoutingStore

        loc = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="b"))
        monkeypatch.setattr(
            settings.storage, "locations", dict.fromkeys(["eu-west", "us-east"], loc)
        )
        state = SimpleNamespace(eu=_ProbeStore(), us=_ProbeStore(), counts=[])
        state.router = RoutingStore(_ProbeStore(), {"eu-west": state.eu, "us-east": state.us}, None)
        monkeypatch.setattr(storage_mod, "_store", state.router)

        @contextlib.asynccontextmanager
        async def session():
            db = AsyncMock()
            result = MagicMock()
            result.all.return_value = state.counts
            db.execute = AsyncMock(return_value=result)
            yield db

        monkeypatch.setattr(lr, "get_db_session", session)
        state.log = MagicMock()
        monkeypatch.setattr(lr, "logger", state.log)
        return state

    @staticmethod
    def _gauge(name: str) -> float:
        from terrapod.api.metrics import STORAGE_LOCATION_REACHABLE

        return STORAGE_LOCATION_REACHABLE.labels(location=name)._value.get()

    async def test_every_named_location_is_probed_read_only_under_state(self, node):
        await lr.check_locations_at_startup()

        assert node.eu.listed == node.us.listed == 1 and node.router.default.listed == 0
        assert self._gauge("eu-west") == self._gauge("us-east") == 1
        assert await lr.locations_readiness() == (True, "healthy")

    @pytest.mark.parametrize(
        "store",
        [
            _ProbeStore(error=OSError("Could not connect to the endpoint URL")),
            _ProbeStore(hang=True),
        ],
    )
    async def test_an_unreachable_location_is_reported_and_the_node_stays_ready(self, node, store):
        node.router.locations["eu-west"] = store
        node.counts.extend([("eu-west", 2)])

        await lr.check_locations_at_startup()

        error = next(c for c in node.log.error.call_args_list if c.kwargs.get("location"))
        assert error.kwargs["location"] == "eu-west" and error.kwargs["reason"]
        assert self._gauge("eu-west") == 0 and self._gauge("us-east") == 1
        assert await lr.locations_readiness() == (True, "unreachable: eu-west")

    async def test_ready_serves_the_cached_result_and_refreshes_in_the_background(
        self, node, monkeypatch
    ):
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 30.0)
        await lr.check_locations_at_startup()
        for _ in range(5):
            assert await lr.locations_readiness() == (True, "healthy")
        assert node.eu.listed == 1 and lr._node.refresh_task is None

    async def test_a_hanging_probe_never_delays_ready(self, node, monkeypatch):
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 0.0)
        monkeypatch.setattr(lr, "_PROBE_TIMEOUT_SECONDS", 5.0)
        await lr.check_locations_at_startup()
        node.eu.hang = True

        answer = await asyncio.wait_for(lr.locations_readiness(), timeout=0.1)

        assert answer == (True, "healthy")
        assert lr._node.refresh_task is not None and not lr._node.refresh_task.done()
        await lr.stop_probing()
        assert lr._node.refresh_task is None

    async def test_concurrent_calls_start_one_refresh_and_the_result_updates(
        self, node, monkeypatch
    ):
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 0.0)
        await lr.check_locations_at_startup()
        node.eu.error = OSError("AccessDenied")
        before = node.eu.listed

        answers = await asyncio.gather(*(lr.locations_readiness() for _ in range(5)))
        assert set(answers) == {(True, "healthy")}
        await lr._node.refresh_task

        assert node.eu.listed == before + 1
        assert await lr.locations_readiness() == (True, "unreachable: eu-west")
        assert self._gauge("eu-west") == 0

    async def test_a_failed_refresh_is_logged_and_never_reaches_ready(self, node, monkeypatch):
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 0.0)
        await lr.check_locations_at_startup()

        async def boom():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(lr, "_probe_locations", boom)
        assert await lr.locations_readiness() == (True, "healthy")
        await lr._node.refresh_task

        assert any("refresh failed" in c.args[0] for c in node.log.warning.call_args_list)
        assert await lr.locations_readiness() == (True, "healthy")

    async def test_stopping_cancels_a_running_refresh_and_is_safe_when_idle(
        self, node, monkeypatch
    ):
        await lr.stop_probing()
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 0.0)
        await lr.check_locations_at_startup()
        node.eu.hang = True
        monkeypatch.setattr(lr, "_PROBE_TIMEOUT_SECONDS", 5.0)
        await lr.locations_readiness()
        task = lr._node.refresh_task

        await lr.stop_probing()

        assert task.cancelled() and lr._node.refresh_task is None

    async def test_the_gauge_and_the_detail_follow_the_latest_probe_both_ways(
        self, node, monkeypatch
    ):
        monkeypatch.setattr(lr, "_PROBE_TTL_SECONDS", 0.0)
        await lr.check_locations_at_startup()
        assert self._gauge("us-east") == 1

        node.us.error = OSError("AccessDenied")
        await lr.locations_readiness()
        await lr._node.refresh_task
        assert await lr.locations_readiness() == (True, "unreachable: us-east")
        assert self._gauge("us-east") == 0
        assert any(c.kwargs.get("location") == "us-east" for c in node.log.error.call_args_list)

        node.us.error = None
        await lr.locations_readiness()
        await lr._node.refresh_task
        assert await lr.locations_readiness() == (True, "healthy")
        assert self._gauge("us-east") == 1

    async def test_the_reason_carries_the_error_not_the_configuration(self, node):
        node.eu.error = OSError("x" * 500)
        await lr.check_locations_at_startup()
        reason = lr._node.unreachable["eu-west"]
        assert reason.startswith("OSError: ") and len(reason) <= 210

    async def test_a_default_deployment_probes_nothing(self, monkeypatch):
        import terrapod.storage as storage_mod
        from terrapod.config import settings

        plain = _ProbeStore()
        monkeypatch.setattr(settings.storage, "locations", {})
        monkeypatch.setattr(storage_mod, "_store", plain)

        @contextlib.asynccontextmanager
        async def session():
            db = AsyncMock()
            result = MagicMock()
            result.all.return_value = []
            db.execute = AsyncMock(return_value=result)
            yield db

        monkeypatch.setattr(lr, "get_db_session", session)
        await lr.check_locations_at_startup()
        assert await lr.locations_readiness() == (True, "healthy")
        assert plain.listed == 0
