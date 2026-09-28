"""What a runner token may do on the Pulumi service surface (#1880).

Agent-mode Pulumi runs use Terrapod as their backend (#1879), so a runner token
reaches this surface and something has to decide what it may do. This file is
that contract.

**Why it needs its own file.** A runner token carries `roles=["everyone"]` and a
`run_id`, and every runner-reachable surface authorizes on the `run_id`, never on
the roles. A test that injects an authenticated session user exercises the
role-based path and says nothing about this one — which is precisely how #1550
shipped a change that passed every test and broke every agent-mode Pulumi run,
because each gate resolved the runner's capabilities the role-based way and
answered 404.

The file this replaces pinned the opposite behaviour, down to asserting that
`_runner_caps_on` must not exist. That was #1576's model; #1879 reverses it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap

pytestmark = pytest.mark.asyncio

MOD = "terrapod.api.routers.pulumi_service"

OWN_WS = uuid.uuid4()
OTHER_WS = uuid.uuid4()


def _request(header: str = "token runtok:abc") -> MagicMock:
    req = MagicMock()
    req.headers = {"authorization": header}
    return req


def _user(auth_method: str = "runner_token", run_id: str | None = None) -> AuthenticatedUser:
    """A principal. `run_id` is taken verbatim when given, so a test can pass the
    empty or malformed values the real thing has to survive."""
    runner = auth_method == "runner_token"
    if runner and run_id is None:
        run_id = str(uuid.uuid4())
    return AuthenticatedUser(
        email="runner" if runner else "dev@example.com",
        display_name=None,
        roles=["everyone"],
        provider_name=auth_method,
        auth_method=auth_method,
        run_id=run_id if runner else None,
    )


def _run(*, plan_only: bool = True, is_destroy: bool = False, workspace_id=OWN_WS) -> MagicMock:
    run = MagicMock()
    run.workspace_id = workspace_id
    run.plan_only = plan_only
    run.is_destroy = is_destroy
    return run


def _ws(ws_id=OWN_WS) -> MagicMock:
    ws = MagicMock()
    ws.id = ws_id
    return ws


def _db_returning_run(run: MagicMock | None) -> AsyncMock:
    """A db whose Run lookup answers `run` — and that checks it WAS the Run lookup.

    Asserting the statement matters: a fake that answers the same whatever it is
    asked would keep passing if the query were narrowed or pointed at the wrong
    table, which is the failure mode this kind of test exists to catch.
    """
    db = AsyncMock()

    async def _execute(stmt):  # noqa: ANN001, ANN202
        assert "FROM runs" in str(stmt), f"expected the Run lookup, got: {stmt}"
        result = MagicMock()
        result.scalar_one_or_none.return_value = run
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


class TestTheRunnerReachesTheSurface:
    async def test_a_runner_token_is_no_longer_refused(self) -> None:
        """#1576 refused it at this door. Authentication says who is calling; what
        they may do is `_caps_on`'s decision, not this function's."""
        from terrapod.api.routers.pulumi_service import pulumi_user

        user = _user()
        with patch("terrapod.api.dependencies.get_current_user", AsyncMock(return_value=user)):
            assert await pulumi_user(_request(), AsyncMock()) is user

    async def test_caps_for_a_runner_come_from_the_run_not_from_roles(self) -> None:
        """The #1550 shape: resolving a runner through role RBAC yields nothing and
        every gate answers 404. `_caps_on` must not take that path for a runner."""
        from terrapod.api.routers.pulumi_service import _caps_on

        with (
            patch(f"{MOD}._runner_caps_on", AsyncMock(return_value=frozenset({"x"}))) as runner,
            patch(f"{MOD}.resolve_workspace_capabilities_for", AsyncMock()) as rbac,
        ):
            assert await _caps_on(AsyncMock(), _user(), _ws()) == frozenset({"x"})
        runner.assert_awaited_once()
        rbac.assert_not_awaited()


class TestItsOwnStack:
    async def test_a_plan_only_run_may_read_and_preview(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        caps = await _runner_caps_on(_db_returning_run(_run(plan_only=True)), _user(), _ws())
        assert cap.WORKSPACE_READ in caps
        assert cap.STATE_READ in caps
        assert cap.RUN_PLAN in caps

    async def test_a_plan_only_run_may_NOT_update(self) -> None:
        """The escalation this guards: a speculative PR plan runs the author's own
        program inside the Job. If its token could begin an update, that program
        could apply changes nobody approved."""
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        caps = await _runner_caps_on(_db_returning_run(_run(plan_only=True)), _user(), _ws())
        assert cap.RUN_APPLY not in caps
        assert cap.RUN_APPLY_DESTROY not in caps
        assert cap.STATE_WRITE not in caps

    async def test_an_apply_capable_run_may_update_and_write_state(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        caps = await _runner_caps_on(_db_returning_run(_run(plan_only=False)), _user(), _ws())
        assert cap.RUN_APPLY in caps
        assert cap.STATE_WRITE in caps

    async def test_only_a_destroy_run_may_destroy(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        plain = await _runner_caps_on(_db_returning_run(_run(plan_only=False)), _user(), _ws())
        destroy = await _runner_caps_on(
            _db_returning_run(_run(plan_only=False, is_destroy=True)), _user(), _ws()
        )
        assert cap.RUN_APPLY_DESTROY not in plain
        assert cap.RUN_APPLY_DESTROY in destroy

    @pytest.mark.parametrize("plan_only", [True, False])
    async def test_a_runner_may_never_delete_the_workspace(self, plan_only: bool) -> None:
        """`pulumi stack rm` goes through `WORKSPACE_DELETE`. The Job runs arbitrary
        program code; deleting the workspace it is running in is not a capability
        any run needs."""
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        caps = await _runner_caps_on(_db_returning_run(_run(plan_only=plan_only)), _user(), _ws())
        assert cap.WORKSPACE_DELETE not in caps


class TestSomebodyElsesStack:
    async def test_read_only_when_the_allowlist_permits(self) -> None:
        """This is what serves a `StackReference`, and the rule is #344's — the same
        grant that authorizes `terraform_remote_state`."""
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        db = _db_returning_run(_run(workspace_id=OTHER_WS))
        with patch(f"{MOD}.consumer_grant_id", AsyncMock(return_value=uuid.uuid4())) as grant:
            caps = await _runner_caps_on(db, _user(), _ws(OWN_WS))
        assert caps == frozenset({cap.WORKSPACE_READ, cap.STATE_READ})
        # Producer is the stack being read; consumer is the run's own workspace.
        assert grant.await_args.kwargs == {
            "producer_workspace_id": OWN_WS,
            "consumer_workspace_id": OTHER_WS,
        }

    async def test_nothing_when_the_allowlist_does_not(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        db = _db_returning_run(_run(workspace_id=OTHER_WS))
        with patch(f"{MOD}.consumer_grant_id", AsyncMock(return_value=None)):
            assert await _runner_caps_on(db, _user(), _ws(OWN_WS)) == frozenset()

    async def test_an_allowlisted_consumer_still_cannot_operate_the_stack(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        db = _db_returning_run(_run(plan_only=False, workspace_id=OTHER_WS))
        with patch(f"{MOD}.consumer_grant_id", AsyncMock(return_value=uuid.uuid4())):
            caps = await _runner_caps_on(db, _user(), _ws(OWN_WS))
        for forbidden in (cap.RUN_PLAN, cap.RUN_APPLY, cap.STATE_WRITE, cap.WORKSPACE_DELETE):
            assert forbidden not in caps


class TestItFailsClosed:
    async def test_an_unresolvable_run_gets_nothing(self) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        assert await _runner_caps_on(_db_returning_run(None), _user(), _ws()) == frozenset()

    @pytest.mark.parametrize("run_id", ["", "not-a-uuid"])
    async def test_a_malformed_run_id_gets_nothing(self, run_id: str) -> None:
        from terrapod.api.routers.pulumi_service import _runner_caps_on

        assert await _runner_caps_on(AsyncMock(), _user(run_id=run_id), _ws()) == frozenset()


class TestOrdinaryCallersAreUnaffected:
    @pytest.mark.parametrize("method", ["api_token", "session"])
    async def test_a_person_still_gets_through(self, method: str) -> None:
        from terrapod.api.routers.pulumi_service import pulumi_user

        user = _user(method)
        with patch("terrapod.api.dependencies.get_current_user", AsyncMock(return_value=user)):
            assert await pulumi_user(_request("token abc.tpod.def"), AsyncMock()) is user

    async def test_capabilities_come_from_rbac(self) -> None:
        from terrapod.api.routers.pulumi_service import _caps_on

        resolved = frozenset({"workspace:read"})
        with patch(
            f"{MOD}.resolve_workspace_capabilities_for", AsyncMock(return_value=resolved)
        ) as resolve:
            assert await _caps_on(AsyncMock(), _user("api_token"), MagicMock()) == resolved
        resolve.assert_awaited_once()
