"""Integration: the remainder, against real rows and a real artifact (#1973).

Real Postgres, because what is under test here is a `WHERE` clause and a column
mapping, and both are invisible to a mocked session: `outstanding_hosts` selects
`RunHostResult` by `run_id`, and a query scoped to the wrong run would return
another run's hosts -- which on a shared workspace means retrying hosts this
configure never targeted, or missing the ones it did.

The service tier already pins `outstanding_from`, the pure predicate, over every
combination of counters. What only this tier can show is that the two halves meet:
the rows come back from the database, the targeted set comes back from storage,
and the subtraction between them is the one the retry carries.

Both states are instantiated in every test, because the property IS a
relationship. A fixture holding only rows cannot see the case the feature exists
for -- a host the play never reached appears in no row at all.
"""

import json
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, RunHostResult, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import configure_recovery, pool_set

pytestmark = pytest.mark.integration


async def _seed_configure_run() -> Run:
    async with get_db_session() as db:
        pool = AgentPool(name=f"rem-{_uuid.uuid4().hex[:6]}")
        db.add(pool)
        await db.flush()

        ws = Workspace(
            name=f"remainder-{_uuid.uuid4().hex[:6]}",
            execution_mode="agent",
            config_directory="ansible",
            default_playbook="site.yml",
        )
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()

        run = Run(
            workspace_id=ws.id,
            configuration_version_id=cv.id,
            status="errored",
            is_configure=True,
            playbook="site.yml",
            source="tfe-api",
        )
        db.add(run)
        await db.flush()
        await db.commit()
        return run


def _artifact(hosts):
    storage = MagicMock()
    storage.get = AsyncMock(return_value=json.dumps({"hosts": {h: {} for h in hosts}}).encode())
    return storage


async def _rows(run, spec):
    """Seed one real row per (host, counters) pair."""
    async with get_db_session() as db:
        for host, counters in spec.items():
            defaults = {
                "ok": 0,
                "changed": 0,
                "failures": 0,
                "unreachable": 0,
                "skipped": 0,
                "rescued": 0,
                "ignored": 0,
                "saw_unreachable": False,
            }
            defaults.update(counters)
            db.add(
                RunHostResult(run_id=run.id, workspace_id=run.workspace_id, host=host, **defaults)
            )
        await db.commit()


class TestTheRemainderAgainstRealRows:
    @pytest.mark.asyncio
    async def test_the_never_reached_host_comes_back(self):
        """`web3` is in the artifact and in no row. This is the whole reason the
        remainder is a subtraction: under `serial:` + `any_errors_fatal` the
        hosts a play never reached appear in no callback event and in no PLAY
        RECAP, so nothing in the database distinguishes them from hosts that do
        not exist."""
        run = await _seed_configure_run()
        await _rows(run, {"web1": {"ok": 3}, "web2": {"failures": 1}})

        async with get_db_session() as db:
            with patch(
                "terrapod.storage.get_storage", return_value=_artifact(["web1", "web2", "web3"])
            ):
                out = await configure_recovery.outstanding_hosts(db, run)

        assert out == ["web2", "web3"]
        assert configure_recovery.remainder_limit(out) == "web2:web3"

    @pytest.mark.asyncio
    async def test_another_run_on_the_same_workspace_does_not_leak_in(self):
        """The `WHERE run_id` clause, which a mocked session cannot show.

        Two configures on one workspace is the ordinary case -- the first failed
        and this is its retry. The first run's row for `web1` is a SUCCESS, and
        that is what makes this test able to fail: the two outcomes are then
        opposites rather than the same list.

          * correctly scoped -- run 2 has no row for `web1` at all, so `web1` is
            outstanding for run 2 and the answer includes it;
          * leaking -- run 1's successful row is picked up, `web1` counts as
            done, and it DROPS OUT.

        Written the obvious way round, with run 1's row a failure, both cases
        return the same list and the test passes whatever the query does. It did
        exactly that until this fixture was inverted.
        """
        first = await _seed_configure_run()
        await _rows(first, {"web1": {"ok": 3}})

        async with get_db_session() as db:
            second = Run(
                workspace_id=first.workspace_id,
                configuration_version_id=first.configuration_version_id,
                status="errored",
                is_configure=True,
                playbook="site.yml",
                source="tfe-api",
            )
            db.add(second)
            await db.flush()
            await db.commit()
            second_id = second.id

        async with get_db_session() as db:
            second = await db.get(Run, second_id)
            await _rows(second, {"web2": {"failures": 1}})
            with patch("terrapod.storage.get_storage", return_value=_artifact(["web1", "web2"])):
                out = await configure_recovery.outstanding_hosts(db, second)

        # `web1` succeeded on the OTHER run and run 2 never reported on it, so
        # run 2 still has it outstanding. A workspace-scoped query would see
        # that success and silently drop `web1` -- a host the retry would then
        # never configure.
        assert out == ["web1", "web2"], f"run 1's rows leaked into run 2: {out}"

    @pytest.mark.asyncio
    async def test_everything_done_leaves_nothing(self):
        """And the caller must refuse to queue on it -- an empty limit means the
        workspace's own default, i.e. every host."""
        run = await _seed_configure_run()
        await _rows(run, {"web1": {"ok": 1}, "web2": {"changed": 1}})

        async with get_db_session() as db:
            with patch("terrapod.storage.get_storage", return_value=_artifact(["web1", "web2"])):
                out = await configure_recovery.outstanding_hosts(db, run)

        assert out == []
