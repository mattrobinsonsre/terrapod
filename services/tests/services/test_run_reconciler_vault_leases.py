"""The reconciler's Vault lease watch can never affect a run (#1649).

The watch runs once per reconcile cycle, after the cycle's transitions have
committed, inside its own ``try``. These tests drive the real
``reconcile_runs`` and pin that:

- an enqueue that raises does not stop a failed Job's run from being errored
  and committed;
- the watch runs after the commit, and also when no run is in flight (a plan
  whose runner already posted its result has left ``planning``);
- a watch that raises is swallowed;
- with the option off the watch is never entered and Redis is never touched
  on its behalf;
- the revocation itself — and its bounded retry — is a triggered task, never
  awaited inline by the reconciler.
"""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import VaultConfig, settings
from terrapod.services import run_reconciler, vault_lease_service
from tests.services.test_vault_lease_service import FakeRedis


@pytest.fixture(autouse=True)
def _restore_vault():
    prior = settings.vault
    yield
    settings.vault = prior


def _on():
    settings.vault = VaultConfig(
        enabled=True,
        instances=[{"name": "db", "address": "https://v", "revoke_leases": True}],
    )


def _off():
    settings.vault = VaultConfig(enabled=True, instances=[{"name": "db", "address": "https://v"}])


def _run(status="planning"):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.pool_id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.job_name = "tprun-x-plan"
    run.job_namespace = "runners"
    run.apply_started_at = None
    # A plan whose Job failed mid-plan: a run with a finished plan is held at a
    # post-plan gate instead, and its Job is never consulted (#1725).
    run.plan_finished_at = None
    run.runner_exit_status = ""
    run.source = "tfe-api"
    return run


def _db(runs):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = runs
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    return db


@asynccontextmanager
async def _cycle(db):
    @asynccontextmanager
    async def session():
        yield db

    with (
        patch.object(run_reconciler, "get_db_session", session),
        patch.object(run_reconciler, "_refresh_pool_queue_depth", AsyncMock()),
        patch.object(run_reconciler, "_refresh_pool_liveness", AsyncMock()),
        patch.object(run_reconciler, "_refresh_ha_metrics", AsyncMock()),
        patch.object(run_reconciler, "_persist_live_log_if_missing", AsyncMock()),
    ):
        yield


async def test_an_enqueue_that_raises_never_stops_the_run_transitioning():
    _on()
    run = _run()
    db = _db([run])
    redis = FakeRedis()
    # A recorded plan lease whose Job the listener has reported failed.
    await redis.hset(
        f"tp:vault:leases:{run.id}:plan",
        mapping={"lease:1": json.dumps({"instance": "db", "lease_id": "L"})},
    )
    await redis.sadd(vault_lease_service.PENDING_SET, f"{run.id}:plan")
    await redis.setex(
        f"tp:job_status:{run.id}:plan", 120, json.dumps({"status": "failed", "terminal": True})
    )

    async def to_errored(_db, r, target, **_kw):
        r.status = target
        return r

    enqueue = AsyncMock(side_effect=ConnectionError("redis queue down"))
    async with _cycle(db):
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch("terrapod.redis.client.publish_listener_event", AsyncMock()),
            patch(
                "terrapod.services.run_service.transition_run",
                AsyncMock(side_effect=to_errored),
            ) as transition,
            patch("terrapod.services.scheduler.enqueue_trigger", enqueue),
        ):
            await run_reconciler.reconcile_runs()

    enqueue.assert_awaited_once()  # the watch did reach the enqueue …
    assert transition.await_args.args[2] == "errored"  # … and the run still errored
    assert run.status == "errored"
    db.commit.assert_awaited_once()


async def test_the_watch_runs_after_the_commit():
    _on()
    order: list[str] = []
    db = _db([_run(status="applying")])
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    watch = AsyncMock(side_effect=lambda _db: order.append("watch"))
    async with _cycle(db):
        with (
            patch.object(run_reconciler, "_reconcile_one", AsyncMock()),
            patch.object(vault_lease_service, "watch_pending", watch),
        ):
            await run_reconciler.reconcile_runs()
    assert order == ["commit", "watch"]


async def test_the_watch_runs_when_no_run_is_in_flight():
    _on()
    watch = AsyncMock()
    async with _cycle(_db([])):
        with patch.object(vault_lease_service, "watch_pending", watch):
            await run_reconciler.reconcile_runs()
    watch.assert_awaited_once()


async def test_a_watch_that_raises_is_swallowed():
    _on()
    async with _cycle(_db([])):
        with patch.object(
            vault_lease_service, "watch_pending", AsyncMock(side_effect=RuntimeError("x"))
        ):
            await run_reconciler.reconcile_runs()


async def test_option_off_never_enters_the_watch_or_touches_redis():
    _off()
    watch = AsyncMock()
    async with _cycle(_db([])):
        with (
            patch.object(vault_lease_service, "watch_pending", watch),
            patch("terrapod.redis.client.get_redis_client") as get_redis,
        ):
            await run_reconciler.reconcile_runs()
    watch.assert_not_awaited()
    get_redis.assert_not_called()


async def test_option_off_the_watch_itself_touches_no_redis():
    """Defence in depth: watch_pending gates on the option too."""
    _off()
    with patch("terrapod.redis.client.get_redis_client") as get_redis:
        await vault_lease_service.watch_pending(_db([]))
    get_redis.assert_not_called()


async def test_revocation_is_never_awaited_inline():
    """A terminal Job makes the watch *enqueue*; the Vault call and its retry
    happen in the triggered task, so a slow Vault never holds up this loop."""
    _on()
    run = _run(status="errored")
    redis = FakeRedis()
    await redis.hset(
        f"tp:vault:leases:{run.id}:plan",
        mapping={"lease:1": json.dumps({"instance": "db", "lease_id": "L"})},
    )
    await redis.sadd(vault_lease_service.PENDING_SET, f"{run.id}:plan")
    await redis.setex(
        f"tp:job_status:{run.id}:plan", 120, json.dumps({"status": "failed", "terminal": True})
    )
    async with _cycle(_db([])):
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch("terrapod.services.scheduler.enqueue_trigger", AsyncMock()) as enqueue,
            patch.object(
                vault_lease_service,
                "revoke_lease",
                AsyncMock(side_effect=AssertionError("revoked inline")),
            ),
        ):
            await run_reconciler.reconcile_runs()
    assert enqueue.await_args.args[0] == vault_lease_service.TRIGGER


def test_the_handler_is_registered_as_a_triggered_task():
    """Source check: the revoke runs through the distributed scheduler, never a
    raw task (AGENTS.md, rule 11)."""
    from pathlib import Path

    app_src = (Path(run_reconciler.__file__).parent.parent / "api" / "app.py").read_text()
    assert "register_trigger_handler(\n        VAULT_LEASE_TRIGGER" in app_src
    lease_src = Path(vault_lease_service.__file__).read_text()
    assert "create_task" not in lease_src
