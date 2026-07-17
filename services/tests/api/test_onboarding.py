"""Services-API tests for the AI onboarding router (#824, P1): the feature
self-gate and the availability probe.

Onboarding is an INDEPENDENT AI feature — it gates on its own
``ai_onboarding.enabled`` switch, with no dependency on ``ai_summary``.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="u@test.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="U",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture(autouse=True)
def _restore_onboarding():
    original = (settings.ai_onboarding.enabled, settings.ai_onboarding.model)
    yield
    settings.ai_onboarding.enabled, settings.ai_onboarding.model = original


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestOnboardingAvailability:
    """The probe is always reachable (onboarding has no feature flag — it's
    gated per-workspace by the workspace:onboard capability). It only reports
    whether the optional AI mode is available."""

    async def test_ai_disabled_still_200_reports_unavailable(self, *mocks):
        settings.ai_onboarding.enabled = False
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 200  # no feature flag → never 404
        attrs = resp.json()["data"]["attributes"]
        assert attrs["ai-available"] is False
        assert attrs["ai-model-configured"] is False

    async def test_ai_enabled_with_model(self, *mocks):
        settings.ai_onboarding.enabled = True
        settings.ai_onboarding.model = "bedrock/us.anthropic.claude-sonnet-4-6"
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["ai-available"] is True
        assert attrs["ai-model-configured"] is True

    async def test_ai_enabled_without_model(self, *mocks):
        settings.ai_onboarding.enabled = True
        settings.ai_onboarding.model = ""
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["ai-available"] is True
        assert attrs["ai-model-configured"] is False


class TestOnboardingConfig:
    """The config block is independent of ai_summary and safe by default."""

    def test_defaults_disabled_and_uncoupled(self):
        from terrapod.config import AIOnboardingConfig

        cfg = AIOnboardingConfig()
        assert cfg.enabled is False  # off by default
        assert cfg.model == ""  # requires explicit config
        assert cfg.daily_token_budget == 0  # unlimited unless set
        assert cfg.auth.aws_session_name == "terrapod-ai-onboarding"

    def test_mounted_on_settings_separately_from_ai_summary(self):
        # Two independent blocks — flipping one must not touch the other.
        assert hasattr(settings, "ai_onboarding")
        assert hasattr(settings, "ai_summary")
        assert settings.ai_onboarding is not settings.ai_summary


class TestOnboardingCapability:
    """workspace:onboard is a dedicated, plan-tier, grantable capability (#824)."""

    def test_in_plan_tier_and_grantable(self):
        from terrapod.auth import capabilities as cap

        # In the plan tier (same trust class as run:plan), so plan/write/admin
        # roles get it and the migration backfills existing run:plan roles.
        assert cap.WORKSPACE_ONBOARD in cap.caps_for_level("plan")
        assert cap.WORKSPACE_ONBOARD not in cap.caps_for_level("read")
        # Grantable + independently checkable (dedicated token, revocable per role).
        assert cap.WORKSPACE_ONBOARD in cap.GRANTABLE_CAPABILITIES
        assert cap.has_capability({cap.WORKSPACE_ONBOARD}, cap.WORKSPACE_ONBOARD)
        # Not a platform cap.
        assert cap.WORKSPACE_ONBOARD not in cap.PLATFORM_CAPABILITIES


# ---------------------------------------------------------------------------
# Session endpoints (#824 P2) — create / list / get, RBAC + validation gates.
# ---------------------------------------------------------------------------
import datetime as _dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from terrapod.auth.capabilities import WORKSPACE_ONBOARD  # noqa: E402
from terrapod.services import onboarding_service  # noqa: E402

_ONB = "terrapod.api.routers.onboarding"


def _fake_ws():
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="ws1",
        labels={},
        owner_email="",
        catalog_item_id=None,
    )


def _fake_session(status="pending", provider="aws"):
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    return SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        workspace_id="11111111-1111-1111-1111-111111111111",
        status=status,
        provider=provider,
        provider_version="",
        engine="tofu",
        engine_version="1.12",
        selected_types=[],
        query_results=None,
        generated_config=None,
        import_blocks=None,
        polished_config=None,
        polished_import_blocks=None,
        ai_assisted=False,
        error="",
        discovery_run_id=None,
        result_run_id=None,
        created_by="u@test.com",
        created_at=now,
        updated_at=now,
    )


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestOnboardingSessions:
    async def _client(self):
        app = _make_app(_user())
        return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)

    async def test_create_requires_onboard_capability(self, *mocks):
        with (
            patch(f"{_ONB}._get_workspace", AsyncMock(return_value=_fake_ws())),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for", AsyncMock(return_value=frozenset())
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    "/api/terrapod/v1/workspaces/ws-1/onboarding-sessions",
                    json={"data": {"attributes": {"provider": "aws"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 403

    async def test_create_rejects_bad_provider(self, *mocks):
        with (
            patch(f"{_ONB}._get_workspace", AsyncMock(return_value=_fake_ws())),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    "/api/terrapod/v1/workspaces/ws-1/onboarding-sessions",
                    json={"data": {"attributes": {"provider": "AWS!"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 422

    async def test_create_rejects_bad_provider_version(self, *mocks):
        with (
            patch(f"{_ONB}._get_workspace", AsyncMock(return_value=_fake_ws())),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    "/api/terrapod/v1/workspaces/ws-1/onboarding-sessions",
                    # would break out of the generated HCL version string
                    json={
                        "data": {"attributes": {"provider": "aws", "provider-version": '6" }\nx'}}
                    },
                    headers=_AUTH,
                )
        assert resp.status_code == 422

    async def test_create_passes_provider_version_constraint(self, *mocks):
        create = AsyncMock(return_value=_fake_session())
        with (
            patch(f"{_ONB}._get_workspace", AsyncMock(return_value=_fake_ws())),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
            patch(f"{_ONB}.onboarding_service.create_session", create),
            patch("terrapod.services.scheduler.enqueue_trigger", AsyncMock()),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    "/api/terrapod/v1/workspaces/ws-1/onboarding-sessions",
                    json={"data": {"attributes": {"provider": "aws", "provider-version": "< 6.0"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 201
        # The validated constraint is threaded to the session, not dropped.
        assert create.await_args.kwargs["provider_version"] == "< 6.0"

    async def test_create_happy_enqueues_discovery(self, *mocks):
        enq = AsyncMock()
        with (
            patch(f"{_ONB}._get_workspace", AsyncMock(return_value=_fake_ws())),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
            patch(
                f"{_ONB}.onboarding_service.create_session", AsyncMock(return_value=_fake_session())
            ),
            patch("terrapod.services.scheduler.enqueue_trigger", enq),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    "/api/terrapod/v1/workspaces/ws-1/onboarding-sessions",
                    json={"data": {"attributes": {"provider": "aws"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["type"] == "onboarding-sessions"
        assert data["attributes"]["status"] == "pending"
        assert data["attributes"]["provider"] == "aws"
        enq.assert_awaited_once()  # discovery runs off the request thread

    async def test_get_returns_surface_from_cache(self, *mocks):
        surface = {"count": 2, "data_sources": [{"name": "aws_vpcs"}]}
        with (
            patch(
                f"{_ONB}.onboarding_service.get_session",
                AsyncMock(return_value=_fake_session(status="schema_ready")),
            ),
            patch(
                f"{_ONB}.onboarding_service.get_session_surface", AsyncMock(return_value=surface)
            ),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
        ):
            async with await self._client() as c:
                resp = await c.get(
                    "/api/terrapod/v1/onboarding-sessions/22222222-2222-2222-2222-222222222222",
                    headers=_AUTH,
                )
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["discovery-surface"] == surface
        assert attrs["data-source-count"] == 2

    async def test_get_missing_is_404(self, *mocks):
        with patch(f"{_ONB}.onboarding_service.get_session", AsyncMock(return_value=None)):
            async with await self._client() as c:
                resp = await c.get(
                    "/api/terrapod/v1/onboarding-sessions/22222222-2222-2222-2222-222222222222",
                    headers=_AUTH,
                )
        assert resp.status_code == 404

    # --- POST /onboarding-sessions/{id}/discover (D2/D3 dispatch) ---
    _DISCOVER = "/api/terrapod/v1/onboarding-sessions/22222222-2222-2222-2222-222222222222/discover"

    async def test_discover_requires_onboard_capability(self, *mocks):
        with (
            patch(
                f"{_ONB}.onboarding_service.get_session",
                AsyncMock(return_value=_fake_session(status="schema_ready")),
            ),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for", AsyncMock(return_value=frozenset())
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    self._DISCOVER,
                    json={"data": {"attributes": {"selected-types": ["aws_vpcs"]}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 403

    async def test_discover_rejects_non_list_selected_types(self, *mocks):
        with (
            patch(
                f"{_ONB}.onboarding_service.get_session",
                AsyncMock(return_value=_fake_session(status="schema_ready")),
            ),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    self._DISCOVER,
                    json={"data": {"attributes": {"selected-types": "aws_vpcs"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 422

    async def test_discover_bad_uuid_is_404(self, *mocks):
        async with await self._client() as c:
            resp = await c.post(
                "/api/terrapod/v1/onboarding-sessions/not-a-uuid/discover",
                json={"data": {"attributes": {"selected-types": []}}},
                headers=_AUTH,
            )
        assert resp.status_code == 404

    async def test_discover_maps_onboarding_error_to_422(self, *mocks):
        with (
            patch(
                f"{_ONB}.onboarding_service.get_session",
                AsyncMock(return_value=_fake_session(status="pending")),
            ),
            patch(
                f"{_ONB}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset({WORKSPACE_ONBOARD})),
            ),
            patch(
                f"{_ONB}.onboarding_service.start_discovery",
                AsyncMock(
                    side_effect=onboarding_service.OnboardingError("session is not schema_ready")
                ),
            ),
        ):
            async with await self._client() as c:
                resp = await c.post(
                    self._DISCOVER,
                    json={"data": {"attributes": {"selected-types": ["aws_vpcs"]}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 422
