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
