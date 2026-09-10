"""A run's own Pulumi CLI must still reach its own stack (#1550).

Agent-mode Pulumi runs call this API from the runner Job, authenticated with the
run's runner token (`PULUMI_ACCESS_TOKEN`). A runner token carries only the
`everyone` role, so ordinary capability resolution grants it nothing on the
workspace — and the authorization #1550 added would have answered every one of
the runner's calls with 404, breaking the agent runs #1523 had just delivered.
No other test sends a runner token, which is why CI stayed green.

The allowance mirrors the Terraform surface's `_runner_state_read_allowed`:

- on its **own** run's stack, a runner holds what that run needs — read, state
  read and preview always; apply for an apply run; destroy for a destroy run —
  and never `state:write` (import) or `workspace:delete`, which no run performs;
- on **another** stack, read only, and only where #344's consumer allowlist
  grants it (a StackReference, governed as `terraform_remote_state` is).
"""

from __future__ import annotations

import uuid
from collections import namedtuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap

pytestmark = pytest.mark.asyncio

MOD = "terrapod.api.routers.pulumi_service"
_RunRow = namedtuple("_RunRow", "workspace_id plan_only is_destroy")


def _runner(run_id: uuid.UUID | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="runner",
        display_name="runner",
        roles=["everyone"],
        provider_name="runner",
        auth_method="runner_token",
        run_id=str(run_id or uuid.uuid4()),
    )


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::a"
    return ws


def _db(row) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.first.return_value = row
    db.execute.return_value = result
    return db


async def _caps(user, ws, row, *, allowlisted=False) -> frozenset[str]:
    from terrapod.api.routers.pulumi_service import _caps_on

    with (
        patch(
            "terrapod.api.routers.tfe_v2._runner_state_read_allowed",
            AsyncMock(return_value=allowlisted),
        ),
        patch(f"{MOD}.resolve_workspace_capabilities_for", AsyncMock(return_value=frozenset())),
    ):
        return await _caps_on(_db(row), user, ws)


class TestOnItsOwnStack:
    async def test_a_plan_only_run_can_preview_and_read_but_not_apply(self) -> None:
        ws = _ws()
        caps = await _caps(_runner(), ws, _RunRow(ws.id, True, False))
        for needed in (cap.WORKSPACE_READ, cap.RUN_READ, cap.STATE_READ, cap.RUN_PLAN):
            assert needed in caps, needed
        assert cap.RUN_APPLY not in caps
        assert cap.RUN_APPLY_DESTROY not in caps

    async def test_an_apply_run_can_update(self) -> None:
        ws = _ws()
        caps = await _caps(_runner(), ws, _RunRow(ws.id, False, False))
        assert cap.RUN_APPLY in caps
        assert cap.RUN_APPLY_DESTROY not in caps

    async def test_only_a_destroy_run_can_destroy(self) -> None:
        ws = _ws()
        caps = await _caps(_runner(), ws, _RunRow(ws.id, False, True))
        assert cap.RUN_APPLY_DESTROY in caps

    @pytest.mark.parametrize("plan_only", [True, False])
    async def test_no_run_imports_state_or_deletes_its_stack(self, plan_only) -> None:
        """Checkpoints are how a run writes state, and those are lease-authorized
        by the update the run started. Wholesale import and `stack rm` are
        operator actions; a runner token that could do them would be a far
        wider credential than the run it was minted for."""
        ws = _ws()
        caps = await _caps(_runner(), ws, _RunRow(ws.id, plan_only, True))
        assert cap.STATE_WRITE not in caps
        assert cap.WORKSPACE_DELETE not in caps


class TestOnAnotherStack:
    async def test_nothing_without_the_consumer_allowlist(self) -> None:
        caps = await _caps(_runner(), _ws(), _RunRow(uuid.uuid4(), False, True))
        assert caps == frozenset()

    async def test_read_only_with_it(self) -> None:
        """A StackReference: read the other stack's outputs, as
        `terraform_remote_state` may, and nothing that changes it."""
        caps = await _caps(_runner(), _ws(), _RunRow(uuid.uuid4(), False, True), allowlisted=True)
        assert caps == frozenset({cap.WORKSPACE_READ, cap.STATE_READ})


class TestFailingSafe:
    async def test_a_token_whose_run_is_gone_holds_nothing(self) -> None:
        assert await _caps(_runner(), _ws(), None) == frozenset()

    async def test_a_malformed_run_id_holds_nothing(self) -> None:
        user = _runner()
        user.run_id = "not-a-uuid"
        assert await _caps(user, _ws(), _RunRow(uuid.uuid4(), False, False)) == frozenset()


class TestOrdinaryUsersAreUnaffected:
    async def test_a_session_user_still_goes_through_rbac(self) -> None:
        """The runner branch must not become a way round RBAC for anyone else."""
        from terrapod.api.routers.pulumi_service import _caps_on

        user = AuthenticatedUser(
            email="someone@example.test",
            display_name="S",
            roles=["everyone"],
            provider_name="local",
            auth_method="session",
        )
        resolved = AsyncMock(return_value=frozenset({cap.WORKSPACE_READ}))
        with patch(f"{MOD}.resolve_workspace_capabilities_for", resolved):
            caps = await _caps_on(AsyncMock(), user, _ws())
        assert caps == frozenset({cap.WORKSPACE_READ})
        resolved.assert_awaited_once()
