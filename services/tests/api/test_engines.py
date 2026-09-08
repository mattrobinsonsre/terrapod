"""The engines endpoint publishes what a run's status *means* (#1407 §3, #1521).

The endpoint exists because `engine` alone is not enough. A consumer holding a
run with `status: "planning"` and `engine: "pulumi"` would otherwise have to know
by convention that Pulumi calls that a preview — and four consumers working that
out separately is how they end up disagreeing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

pytestmark = pytest.mark.asyncio

BASE = "/api/terrapod/v1/engines"
AUTH = {"Authorization": "Bearer test-token"}


def _user() -> AuthenticatedUser:
    """A plain authenticated user, holding nothing but `everyone`.

    The endpoint describes the deployment's capabilities rather than anybody's
    resources, so the least-privileged caller is the right one to assert with.
    """
    return AuthenticatedUser(
        email="a@b.c",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app(authenticated: bool = True):
    app = create_app()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestListing:
    async def test_lists_the_engines_this_deployment_serves(self) -> None:
        async with await _client(_app()) as c:
            r = await c.get(BASE, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert [e["id"] for e in body["data"]] == ["terraform"]
        assert body["data"][0]["type"] == "engines"

    async def test_emits_the_house_pagination_meta(self) -> None:
        """List endpoints carry `meta.pagination`, uniformly (AGENTS.md)."""
        async with await _client(_app()) as c:
            r = await c.get(BASE, headers=AUTH)
        pagination = r.json()["meta"]["pagination"]
        assert pagination["total-count"] == 1
        assert set(pagination) >= {"current-page", "page-size", "total-count", "total-pages"}

    async def test_requires_authentication(self) -> None:
        async with await _client(_app(authenticated=False)) as c:
            r = await c.get(BASE)
        assert r.status_code in (401, 403)


class TestTheVocabulary:
    """The attributes that make the run surface interpretable."""

    async def _attrs(self, name: str = "terraform") -> dict:
        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/{name}", headers=AUTH)
        assert r.status_code == 200
        return r.json()["data"]["attributes"]

    async def test_maps_every_phase_status_onto_the_engines_word(self) -> None:
        assert (await self._attrs())["status-phases"] == {
            "planning": "plan",
            "planned": "plan",
            "applying": "apply",
            "applied": "apply",
        }

    async def test_terminal_statuses_belong_to_no_phase(self) -> None:
        """`errored` is not a plan.

        Mapping every status onto a phase would be tidier and wrong — a client
        would then report a failed run as planning. The absence is the answer.
        """
        status_phases = (await self._attrs())["status-phases"]
        for terminal in ("errored", "canceled", "discarded"):
            assert terminal not in status_phases

    async def test_phases_are_in_execution_order(self) -> None:
        assert (await self._attrs())["phases"] == ["plan", "apply"]

    async def test_publishes_a_namespace_not_prose(self) -> None:
        """Display words are translated per locale and cannot come from here.

        Serving "Planning…" would pin the UI's wording to English inside an API
        response, where no translation pipeline can reach it.
        """
        attrs = await self._attrs()
        assert attrs["vocabulary"] == "terraform"
        assert " " not in attrs["vocabulary"]

    async def test_reports_the_default_execution_backend(self) -> None:
        """tofu vs terraform is a choice *within* this engine, not another one."""
        assert (await self._attrs())["default-execution-backend"] == "tofu"


class TestAnEngineThisDeploymentDoesNotServe:
    async def test_404s_rather_than_returning_an_empty_shape(self) -> None:
        """Told plainly, rather than left to interpret silence."""
        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pulumi", headers=AUTH)
        assert r.status_code == 404
        assert "pulumi" in r.json()["detail"]

    async def test_the_error_carries_both_envelope_shapes(self) -> None:
        """The house error contract: JSON:API `errors` *and* legacy `detail`."""
        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/nope", headers=AUTH)
        body = r.json()
        assert body["errors"][0]["status"] == "404"
        assert body["detail"]
