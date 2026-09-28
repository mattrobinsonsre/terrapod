"""A CLI-driven `pulumi up` becomes a run in the workspace's history (#1563).

A `pulumi up` from a laptop changed infrastructure and left nothing behind.
Terrapod can record one because Pulumi drives its backend through a
begin/checkpoint/complete lifecycle — which is more than the Terraform path
gets, where local mode shows only a final state PUT and no run is created at
all. The divergence follows from what each protocol makes visible, not from a
policy choice.

The hazards these pin are all of the shape "the machinery that supervises a run
assumes a Kubernetes Job, and this run has none".
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser

pytestmark = pytest.mark.asyncio

MOD = "terrapod.api.routers.pulumi_service"


def _user(auth_method: str = "session", run_id: str | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="a@b.c",
        display_name="A",
        roles=["everyone"],
        provider_name="local",
        auth_method=auth_method,
        run_id=run_id,
    )


def _ws(*, execution_mode: str = "local", vcs_connection_id=None) -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::dev"
    ws.labels = {}
    ws.execution_mode = execution_mode
    ws.vcs_connection_id = vcs_connection_id
    return ws


async def _begin(kind: str, user: AuthenticatedUser, ws=None, *, created=None):
    """Drive `_begin_update` and hand back the record it wrote."""
    from terrapod.api.routers import pulumi_service

    ws = ws or _ws()
    redis = AsyncMock()
    redis.set.return_value = True
    run = created if created is not None else SimpleNamespace(id=uuid.uuid4())
    with (
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch(f"{MOD}.take_workspace_lock", AsyncMock()),
        patch("terrapod.services.run_service.create_run", AsyncMock(return_value=run)) as create,
        patch("terrapod.services.run_service.start_external_apply", AsyncMock()) as start,
    ):
        await pulumi_service._begin_update(ws, kind, user, AsyncMock())
    record = redis.hset.await_args.kwargs["mapping"] if redis.hset.await_args else {}
    return SimpleNamespace(record=record, create=create, start=start, redis=redis, run=run)


class TestWhichUpdatesBecomeRuns:
    async def test_a_cli_update_does(self) -> None:
        out = await _begin("update", _user())
        out.create.assert_awaited_once()
        assert out.create.await_args.kwargs["source"] == "pulumi-cli"
        assert out.create.await_args.kwargs["plan_only"] is False

    async def test_a_cli_destroy_is_recorded_as_a_destroy(self) -> None:
        out = await _begin("destroy", _user())
        assert out.create.await_args.kwargs["is_destroy"] is True

    async def test_an_agent_update_does_not(self) -> None:
        """It already has its own run — the one the runner is executing. A second
        would double-count the same work and, worse, would be a non-plan-only
        run in `applying` that the dispatcher counts as in-flight, blocking the
        workspace against the very run that created it."""
        out = await _begin("update", _user("runner_token", run_id=str(uuid.uuid4())))
        out.create.assert_not_awaited()

    async def test_a_preview_does_not(self) -> None:
        """Decided and documented: a preview changes nothing, writes no state
        version and cannot checkpoint (#1550), while a laptop preview runs
        constantly during development. A run apiece would swamp the history this
        exists to make useful."""
        out = await _begin("preview", _user())
        out.create.assert_not_awaited()

    async def test_the_run_is_placed_straight_into_applying(self) -> None:
        out = await _begin("update", _user())
        out.start.assert_awaited_once()


class TestTheRecordNamesItSeparately:
    async def test_a_cli_update_records_cli_run_id(self) -> None:
        out = await _begin("update", _user())
        assert out.record["cli_run_id"] == str(out.run.id)

    async def test_and_not_run_id(self) -> None:
        """`run_id` means "an agent Job owns this update", and `handle_run_ended`
        finds an update by it so a run reaching a terminal state tears its update
        down (#1882). A CLI update reaches exactly that path when its own run
        completes — sharing the key would have the run's completion end the
        update that was completing it, releasing a lock and promoting a
        checkpoint a second time."""
        out = await _begin("update", _user())
        assert "run_id" not in out.record

    async def test_an_agent_update_still_records_run_id(self) -> None:
        rid = str(uuid.uuid4())
        out = await _begin("update", _user("runner_token", run_id=rid))
        assert out.record["run_id"] == rid
        assert "cli_run_id" not in out.record


class TestTheRunIsCreatedAfterTheLock:
    async def test_the_lock_is_taken_first(self) -> None:
        """`take_workspace_lock` refuses when an apply-capable run on the
        workspace is already `applying`. Creating the run first would therefore
        make every CLI update refuse itself — the run it had just written being
        the thing in its way."""
        from terrapod.api.routers import pulumi_service

        order: list[str] = []
        redis = AsyncMock()
        redis.set.return_value = True

        async def _lock(*_a, **_k):
            order.append("lock")

        async def _create(*_a, **_k):
            order.append("create")
            return SimpleNamespace(id=uuid.uuid4())

        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}.take_workspace_lock", _lock),
            patch("terrapod.services.run_service.create_run", _create),
            patch("terrapod.services.run_service.start_external_apply", AsyncMock()),
        ):
            await pulumi_service._begin_update(_ws(), "update", _user(), AsyncMock())
        assert order == ["lock", "create"]


class TestTheReconcilerLeavesItAlone:
    """The five-minute fuse this feature had to defuse.

    `_check_stale` reads `job_name IS NULL` as "the Job never launched" and
    errors the run after `launch_timeout_seconds` — 300s by default. A CLI
    update legitimately has no Job and routinely takes longer than that, so
    without the guard every `pulumi up` past five minutes would be force-errored
    mid-apply while going perfectly well.
    """

    def test_a_pulumi_cli_run_is_externally_executed(self) -> None:
        from terrapod.services import run_service

        assert run_service.is_externally_executed(SimpleNamespace(source="pulumi-cli")) is True

    def test_an_ordinary_run_is_not(self) -> None:
        from terrapod.services import run_service

        for source in ("tfe-api", "vcs", "drift-detection", "run-trigger"):
            assert run_service.is_externally_executed(SimpleNamespace(source=source)) is False

    async def test_the_reconciler_returns_before_the_stale_check(self) -> None:
        from terrapod.services import run_reconciler

        run = SimpleNamespace(
            id=uuid.uuid4(), source="pulumi-cli", status="applying", job_name=None
        )
        with (
            patch.object(run_reconciler, "_check_stale", AsyncMock()) as stale,
            patch("terrapod.services.run_service.is_held_at_gate", return_value=False),
        ):
            await run_reconciler._reconcile_one(AsyncMock(), run, "pulumi")
        stale.assert_not_awaited()

    async def test_an_agent_run_without_a_job_is_still_checked(self) -> None:
        """The guard must not disarm the timeout for runs it was written for."""
        from terrapod.services import run_reconciler

        run = SimpleNamespace(id=uuid.uuid4(), source="vcs", status="applying", job_name=None)
        with (
            patch.object(run_reconciler, "_check_stale", AsyncMock()) as stale,
            patch("terrapod.services.run_service.is_held_at_gate", return_value=False),
        ):
            await run_reconciler._reconcile_one(AsyncMock(), run, "terraform")
        stale.assert_awaited_once()


class TestTheUpdateEndsTheRun:
    async def test_a_succeeded_update_applies_it(self) -> None:
        from terrapod.api.routers import pulumi_service

        run = SimpleNamespace(id=uuid.uuid4(), status="applying")
        with patch("terrapod.services.run_service.transition_run", AsyncMock()) as tr:
            await pulumi_service._end_cli_run(AsyncMock(), run, "succeeded")
        assert tr.await_args.args[2] == "applied"

    async def test_a_failed_update_errors_it(self) -> None:
        from terrapod.api.routers import pulumi_service

        run = SimpleNamespace(id=uuid.uuid4(), status="applying")
        with patch("terrapod.services.run_service.transition_run", AsyncMock()) as tr:
            await pulumi_service._end_cli_run(AsyncMock(), run, "failed")
        assert tr.await_args.args[2] == "errored"

    async def test_an_unrecognised_status_is_not_reported_as_success(self) -> None:
        """Erring towards saying an apply failed beats claiming one worked."""
        from terrapod.api.routers import pulumi_service

        run = SimpleNamespace(id=uuid.uuid4(), status="applying")
        with patch("terrapod.services.run_service.transition_run", AsyncMock()) as tr:
            await pulumi_service._end_cli_run(AsyncMock(), run, None)
        assert tr.await_args.args[2] == "errored"

    async def test_cancel_walks_through_canceling(self) -> None:
        """`applying -> canceled` is not a legal transition, and the reconciler
        deliberately ignores these runs — so a `canceling` one left to it would
        sit there for ever. It is resolved here instead."""
        from terrapod.api.routers import pulumi_service

        run = SimpleNamespace(id=uuid.uuid4(), status="applying")
        with patch("terrapod.services.run_service.transition_run", AsyncMock()) as tr:
            await pulumi_service._cancel_cli_run(AsyncMock(), run)
        assert [c.args[2] for c in tr.await_args_list] == ["canceling", "canceled"]


class TestAnAlreadyEndedRunIsNotMovedAgain:
    async def test_a_terminal_run_is_not_returned(self) -> None:
        """The sweep may have ended the run when the lease lapsed, and the CLI
        can still come back afterwards with a `complete` for an update Terrapod
        has already written off. The first answer stands."""
        from terrapod.api.routers import pulumi_service

        run = SimpleNamespace(id=uuid.uuid4(), status="errored")
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = run
        db.execute.return_value = result
        assert await pulumi_service._cli_run_of(db, {"cli_run_id": str(run.id)}) is None

    async def test_an_update_with_no_cli_run_answers_none(self) -> None:
        from terrapod.api.routers import pulumi_service

        assert await pulumi_service._cli_run_of(AsyncMock(), {}) is None

    async def test_junk_is_not_a_crash(self) -> None:
        from terrapod.api.routers import pulumi_service

        assert await pulumi_service._cli_run_of(AsyncMock(), {"cli_run_id": "nope"}) is None


class TestTheStateVersionIsAttributed:
    async def test_write_deployment_carries_the_run(self) -> None:
        """Without it a Pulumi state version has no run, so the run page has
        nothing to link to — which the Terraform upload paths already do."""
        import inspect

        from terrapod.services import pulumi_checkpoint_service

        sig = inspect.signature(pulumi_checkpoint_service.write_deployment)
        assert "run_id" in sig.parameters
        assert (
            "run_id" in inspect.signature(pulumi_checkpoint_service.promote_checkpoint).parameters
        )
