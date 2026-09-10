"""A runner token is refused by the Pulumi service surface (#1576).

Agent-mode Pulumi runs do not use Terrapod as a live Pulumi backend. They keep
their stack in a file backend inside the Job and hand state over through the
run's artifact API, as a Terraform run does with `terraform.tfstate`. The
service surface serves the CLI in local mode only.

#1550 once taught this surface to authorize a runner from its run, because the
runner's own CLI called it. That allowance is gone, and the refusal happens in
`pulumi_user` — the dependency every stack call resolves — so no route can
forget it and no Job environment can talk its way back in.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser

pytestmark = pytest.mark.asyncio

MOD = "terrapod.api.routers.pulumi_service"


def _request(header: str = "token runtok:abc") -> MagicMock:
    req = MagicMock()
    req.headers = {"authorization": header}
    return req


def _user(auth_method: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="runner" if auth_method == "runner_token" else "dev@example.com",
        display_name=None,
        roles=["everyone"],
        provider_name=auth_method,
        auth_method=auth_method,
        run_id=str(uuid.uuid4()) if auth_method == "runner_token" else None,
    )


class TestTheRunnerIsRefused:
    async def test_a_runner_token_gets_a_403(self) -> None:
        from terrapod.api.routers.pulumi_service import pulumi_user

        with patch(
            "terrapod.api.dependencies.get_current_user",
            AsyncMock(return_value=_user("runner_token")),
        ):
            with pytest.raises(HTTPException) as exc:
                await pulumi_user(_request(), AsyncMock())
        assert exc.value.status_code == 403
        assert "artifacts" in exc.value.detail

    async def test_the_refusal_does_not_depend_on_the_scheme(self) -> None:
        """Bearer is accepted for operators debugging with curl; it must not be
        a way round the refusal."""
        from terrapod.api.routers.pulumi_service import pulumi_user

        with patch(
            "terrapod.api.dependencies.get_current_user",
            AsyncMock(return_value=_user("runner_token")),
        ):
            with pytest.raises(HTTPException) as exc:
                await pulumi_user(_request("Bearer runtok:abc"), AsyncMock())
        assert exc.value.status_code == 403


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


class TestNoRunnerAllowanceRemains:
    def test_the_run_derived_allowance_is_gone(self) -> None:
        """The allowance let a runner read, preview, update and read other stacks
        through this surface. Its return would quietly restore agent runs using
        Terrapod as their backend, which is the thing #1576 removed."""
        import terrapod.api.routers.pulumi_service as svc

        assert not hasattr(svc, "_runner_caps_on")
