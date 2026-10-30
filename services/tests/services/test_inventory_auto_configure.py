"""Auto-configure on a non-git inventory change (#2224).

Behavioural rather than structural throughout, because every decision here is a
branch that inverts silently: a settle that does not settle fires ~150 runs for
one `for_each` apply, an adopt that fires configures a fleet on upgrade, and a
skip that stamps loses the change it skipped.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import inventory_auto_configure as iac
from terrapod.services import workspace_settings

_TOKEN_OLD = "aaaaaaaaaaaaaaaaaaaaaaaa"
_TOKEN_NEW = "bbbbbbbbbbbbbbbbbbbbbbbb"


def _ws(**kw):
    ws = MagicMock()
    ws.id = kw.get("id", uuid.uuid4())
    ws.name = kw.get("name", "inv-ws")
    ws.locked = kw.get("locked", False)
    ws.auto_configure_inventory_stamp = kw.get("stamp", _TOKEN_OLD)
    return ws


class _Redis:
    """Async get/setex over a dict, so a settle can actually be observed."""

    def __init__(self, seen: dict[str, str] | None = None):
        self.store = dict(seen or {})
        self.writes: list[tuple[str, str]] = []

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, _ttl, value):
        self.store[key] = value
        self.writes.append((key, value))


_ABSENT = object()


async def _run(workspaces, *, redis, current, in_flight=False, cv=_ABSENT):
    """Drive one cycle and return the create_run mock.

    `cv=None` means the workspace has no configuration version; omitting it
    gives a real one, since a CV must carry an `id` the run is built with.
    """
    if cv is _ABSENT:
        cv = MagicMock(id=uuid.uuid4())
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = workspaces
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    def _make(*a, **kw):
        r = MagicMock(id=uuid.uuid4())
        r.status = "pending"
        return r

    created = AsyncMock(side_effect=_make)

    async def _queue(_db, r):
        # What `queue_run` does, and the only part these tests care about.
        r.status = "queued"
        return r

    queued = AsyncMock(side_effect=_queue)

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    with (
        patch.object(iac, "get_db_session", lambda: _Session()),
        patch("terrapod.redis.client.get_redis_client", lambda: redis),
        patch.object(iac.inventory_resolve, "platform_rev", AsyncMock(return_value=current)),
        patch.object(iac, "_configure_in_flight", AsyncMock(return_value=in_flight)),
        patch.object(iac.run_service, "get_latest_uploaded_cv", AsyncMock(return_value=cv)),
        patch.object(iac.run_service, "create_run", created),
        patch.object(iac.run_service, "queue_run", queued),
    ):
        await iac.auto_configure_cycle()
    created.queued = queued
    return created, db


@pytest.mark.asyncio
class TestTheSettleIsTheDebounce:
    async def test_a_moving_token_does_not_fire_it_waits_a_tick(self):
        """The fifty-host `for_each` case, and the whole reason this is not a
        bare comparison: firing per tick while an apply is still writing is what
        turns one apply into a run per tick."""
        ws = _ws()
        redis = _Redis()  # nothing seen yet, so the token is "moving"
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_not_called()
        assert redis.writes == [(f"tp:inv_settle:{ws.id}", _TOKEN_NEW)]

    async def test_a_token_stable_since_the_previous_tick_fires_once(self):
        ws = _ws()
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_called_once()
        assert ws.auto_configure_inventory_stamp == _TOKEN_NEW

    async def test_the_run_is_a_configure_against_the_latest_uploaded_cv(self):
        ws = _ws()
        cv = MagicMock(id=uuid.uuid4())
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW, cv=cv)
        kwargs = created.call_args[1]
        assert kwargs["is_configure"] is True
        assert kwargs["configuration_version_id"] == cv.id
        # No playbook or limit override: the workspace's own defaults apply, and
        # narrowing to the hosts the change touched is #2229.
        assert "playbook" not in kwargs
        assert "ansible_limit" not in kwargs


@pytest.mark.asyncio
class TestTheFirstObservationAdoptsRatherThanFires:
    async def test_a_null_stamp_records_the_baseline_and_does_not_configure(self):
        """Every workspace has a NULL stamp the moment this ships, including ones
        that already have rows and already have the flag on. Firing here would
        configure a fleet on upgrade for a change nobody made."""
        ws = _ws(stamp=None)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_not_called()
        assert ws.auto_configure_inventory_stamp == _TOKEN_NEW

    async def test_and_the_tick_after_that_does_configure(self):
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_called_once()


@pytest.mark.asyncio
class TestNothingNewDoesNothing:
    async def test_an_unchanged_token_does_not_fire(self):
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_OLD})
        created, db = await _run([ws], redis=redis, current=_TOKEN_OLD)
        created.assert_not_called()
        db.commit.assert_not_called()


@pytest.mark.asyncio
class TestTheSkipsThatMustNotStamp:
    async def test_a_configure_already_in_flight_skips_WITHOUT_stamping(self):
        """Stamping here would lose the change: the next tick would see the token
        it had already recorded and conclude there was nothing to do."""
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW, in_flight=True)
        created.assert_not_called()
        assert ws.auto_configure_inventory_stamp == _TOKEN_OLD

    async def test_no_configuration_version_skips_without_stamping(self):
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW, cv=None)
        created.assert_not_called()
        assert ws.auto_configure_inventory_stamp == _TOKEN_OLD

    async def test_a_locked_workspace_is_skipped(self):
        ws = _ws(locked=True)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_not_called()


@pytest.mark.asyncio
class TestOneWorkspaceNeverKillsTheCycle:
    async def test_a_failure_on_the_first_still_lets_the_second_run(self):
        a, b = _ws(name="a"), _ws(name="b")
        redis = _Redis({f"tp:inv_settle:{a.id}": _TOKEN_NEW, f"tp:inv_settle:{b.id}": _TOKEN_NEW})
        calls: list[str] = []

        async def _flaky(db, *, workspace, **kw):
            calls.append(workspace.name)
            if workspace.name == "a":
                raise RuntimeError("boom")
            return MagicMock(id=uuid.uuid4())

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [a, b]
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        class _Session:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_a):
                return False

        with (
            patch.object(iac, "get_db_session", lambda: _Session()),
            patch("terrapod.redis.client.get_redis_client", lambda: redis),
            patch.object(iac.inventory_resolve, "platform_rev", AsyncMock(return_value=_TOKEN_NEW)),
            patch.object(iac, "_configure_in_flight", AsyncMock(return_value=False)),
            patch.object(
                iac.run_service,
                "get_latest_uploaded_cv",
                AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ),
            patch.object(iac.run_service, "create_run", _flaky),
            patch.object(iac.run_service, "queue_run", AsyncMock(side_effect=lambda _d, r: r)),
        ):
            await iac.auto_configure_cycle()

        assert calls == ["a", "b"], "the cycle stopped at the first failure"
        assert a.auto_configure_inventory_stamp == _TOKEN_OLD, "a failed create must not stamp"
        assert b.auto_configure_inventory_stamp == _TOKEN_NEW


@pytest.mark.asyncio
class TestTheScanIsFilteredToTheOptedIn:
    async def test_both_flags_are_required_in_the_query(self):
        """`auto_configure_enabled` is the opt-in; `configure_enabled` is what
        makes the operation exist at all. Filtering on both is what keeps a
        terraform-only deployment to one indexed SELECT returning nothing."""
        ws = _ws()
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        _created, db = await _run([ws], redis=redis, current=_TOKEN_NEW)
        clause = str(db.execute.await_args_list[0].args[0].whereclause)
        assert "auto_configure_enabled" in clause
        assert "configure_enabled" in clause


class TestTheInFlightSetExcludesAwaitingConfirm:
    def test_it_is_not_every_non_terminal_status(self):
        """The recorded drift incident: keying on "any non-terminal run" froze
        drift for ever, because a `planned` run awaiting an operator's confirm
        sits indefinitely. A configure has no confirm step, and this set must
        stay narrow enough that it cannot acquire one by accident."""
        assert "planned" not in iac._CONFIGURE_IN_FLIGHT
        assert "confirmed" not in iac._CONFIGURE_IN_FLIGHT
        assert "configuring" in iac._CONFIGURE_IN_FLIGHT


@pytest.mark.asyncio
class TestTheRunIsActuallyQUEUED:
    """The assertion that was missing, and its absence shipped an inert feature.

    Every test here used to assert `create_run` **was called** -- a presence
    check, not an application check. `create_run` leaves the run `pending`, the
    claim loop matches `queued`/`confirmed`/`applying`, and nothing sweeps
    pending; so the whole trigger did nothing while twelve tests and three
    mutations passed. Asserting the STATUS is what a reader checks, and it is
    the only thing that distinguishes a configure that will happen from one that
    will not.
    """

    async def test_a_created_run_reaches_queued(self):
        ws = _ws()
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_called_once()
        created.queued.assert_awaited_once()
        assert created.queued.await_args.args[1].status == "queued"

    async def test_nothing_is_queued_when_nothing_is_created(self):
        """The contrast, so the assertion above cannot be satisfied by queueing
        unconditionally."""
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_OLD})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_OLD)
        created.assert_not_called()
        created.queued.assert_not_awaited()


@pytest.mark.asyncio
class TestTurningTheTriggerOffForgetsTheBaseline:
    """A RE-ENABLE must adopt, exactly as a first-ever enable does (#2277 A2).

    `docs/ansible-configure.md` promises that switching the flag on for a fleet
    that already has hosts is safe, and the adopt-don't-fire guard above is what
    delivers that -- keyed on a NULL stamp. Nothing cleared the stamp when the
    flag was turned off, so

        on -> off -> the inventory moves while off -> on again

    left a stale baseline, and the next cycle read the difference as an unacted
    change and queued a run against `default-limit`, where empty means every
    host. Reachable across a whole fleet in one bulk call.

    Driven through the real cycle rather than asserting on the producer alone,
    because the property is "does it fire", not "is a column None".
    """

    async def test_disabling_clears_the_stamp(self):
        ws = _ws(stamp=_TOKEN_OLD)
        workspace_settings.set_auto_configure_enabled(ws, False)
        assert ws.auto_configure_enabled is False
        assert ws.auto_configure_inventory_stamp is None, (
            "the baseline must be forgotten when the trigger goes off, or a "
            "re-enable fires for a change made while it was off"
        )

    async def test_enabling_does_not_clear_the_stamp(self):
        """The other direction, which is what stops the careless fix.

        Clearing on every write would make the trigger forget its baseline each
        time any bulk update touched the flag -- including a no-op re-assert of
        True -- and the next cycle would then adopt a change it should have
        acted on. So the clear is conditional, and this is the test that keeps
        it conditional.
        """
        ws = _ws(stamp=_TOKEN_OLD)
        workspace_settings.set_auto_configure_enabled(ws, True)
        assert ws.auto_configure_enabled is True
        assert ws.auto_configure_inventory_stamp == _TOKEN_OLD

    async def test_a_re_enabled_workspace_adopts_rather_than_fires(self):
        """The whole point, end to end: off then on, and the cycle must not fire."""
        ws = _ws(stamp=_TOKEN_OLD)
        workspace_settings.set_auto_configure_enabled(ws, False)
        workspace_settings.set_auto_configure_enabled(ws, True)

        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)

        created.assert_not_called()
        assert ws.auto_configure_inventory_stamp == _TOKEN_NEW, (
            "it should have adopted the current inventory as the new baseline"
        )

    async def test_a_workspace_whose_trigger_was_never_off_still_fires(self):
        """The feature itself, unbroken -- a stale stamp with no disable in its
        history is a real unacted change and must still queue a run."""
        ws = _ws(stamp=_TOKEN_OLD)
        redis = _Redis({f"tp:inv_settle:{ws.id}": _TOKEN_NEW})
        created, _ = await _run([ws], redis=redis, current=_TOKEN_NEW)
        created.assert_called_once()


class TestBothUpdatePathsGoThroughTheProducer:
    """The rule is one producer, so a bare assignment has to fail here.

    Behaviourally invisible: a route that sets the column directly passes every
    test above, because those drive the producer. This is the call-site half --
    the recorded "a presence check is not an application check" shape, where the
    rule exists and nothing routes through it.
    """

    def test_no_module_assigns_the_column_outside_the_producer(self):
        import ast
        import pathlib

        root = pathlib.Path(workspace_settings.__file__).resolve().parents[1]
        producer = pathlib.Path(workspace_settings.__file__).resolve()
        offenders = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Attribute)
                        and tgt.attr == "auto_configure_enabled"
                        and path != producer
                    ):
                        offenders.append(f"{path.name}:{tgt.lineno}")
        assert not offenders, (
            "these assign `auto_configure_enabled` directly, so turning the "
            "trigger off there leaves a stale baseline and a later re-enable "
            f"fires across the fleet: {offenders}. Call "
            "workspace_settings.set_auto_configure_enabled instead."
        )

    def test_the_bulk_apply_loop_routes_the_flag_to_the_producer(self):
        """The bulk path mutates through `setattr(ws, attr, new)`, which no AST
        walk can attribute to a column -- so the clause above cannot see it and
        this one has to read the loop itself."""
        import inspect

        from terrapod.api.routers import workspace_bulk

        src = inspect.getsource(workspace_bulk)
        assert 'elif attr == "auto_configure_enabled":' in src, (
            "the bulk apply loop must special-case the flag the way it already "
            "special-cases the agent-pool set, or a fleet-wide disable leaves "
            "every stamp stale"
        )
        assert "set_auto_configure_enabled" in src
