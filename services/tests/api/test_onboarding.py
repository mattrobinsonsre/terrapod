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
class TestOnboardingGate:
    async def test_disabled_returns_404(self, *mocks):
        settings.ai_onboarding.enabled = False
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 404

    async def test_enabled_reports_model_configured(self, *mocks):
        settings.ai_onboarding.enabled = True
        settings.ai_onboarding.model = "bedrock/us.anthropic.claude-sonnet-4-6"
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["enabled"] is True
        assert attrs["model-configured"] is True

    async def test_enabled_without_model_reports_unconfigured(self, *mocks):
        settings.ai_onboarding.enabled = True
        settings.ai_onboarding.model = ""
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/onboarding", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["model-configured"] is False


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
