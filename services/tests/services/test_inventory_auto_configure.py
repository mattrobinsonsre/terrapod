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

    created = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

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
    ):
        await iac.auto_configure_cycle()
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
