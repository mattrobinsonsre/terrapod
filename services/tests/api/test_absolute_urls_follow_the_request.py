"""Absolute URLs the CLI follows are built from the caller's host (#1703).

go-tfe needs absolute URLs for a configuration version's `upload-url` and a
plan's or apply's `log-read-url`, and it uses them as-is. They used to be built
from `auth.callback_base_url`, an SSO setting that defaults to
`http://localhost:8000`. On an install without SSO, every CLI config upload was
handed a localhost URL and failed to connect, and remote plan logs could not be
streamed.

State upload URLs already derived the base from the request -- the forwarded
host, then a real-looking Host header, then that setting only as a last resort
-- so the same deployment produced working state URLs and broken config URLs.
These tests pin the other three to the same derivation.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.routers.config_versions import _cv_json
from terrapod.api.routers.runs import _apply_json, _plan_json
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

PUBLIC = "https://terrapod.example.com"


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "http",
            "server": ("terrapod-api", 8000),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def _cv():
    cv = MagicMock()
    cv.id = uuid.uuid4()
    cv.workspace_id = uuid.uuid4()
    cv.source = "tfe-api"
    cv.status = "pending"
    cv.auto_queue_runs = True
    cv.speculative = False
    cv.created_at = datetime.now(UTC)
    return cv


def _run():
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = "planned"
    run.has_json_output = True
    run.resource_additions = None
    return run


def _localhost_default():
    """The chart default: SSO off, callback_base_url never set."""
    return patch("terrapod.config.settings.auth.callback_base_url", "http://localhost:8000")


class TestTheUrlsNameTheHostTheCallerUsed:
    def test_config_version_upload_url(self):
        with _localhost_default():
            attrs = _cv_json(
                _cv(),
                _request(
                    {"x-forwarded-host": "terrapod.example.com", "x-forwarded-proto": "https"}
                ),
            )["data"]["attributes"]
        # The segment is a signed capability rather than the cv- id now
        # (GHSA: an unauthenticated upload addressed by a guessable id). This
        # test is about the HOST, so it asserts the base and leaves the segment
        # to the capability tests.
        assert attrs["upload-url"].startswith(f"{PUBLIC}/api/v2/configuration-versions/")
        assert "localhost" not in attrs["upload-url"]

    def test_plan_log_read_url_and_json_output(self):
        with _localhost_default():
            attrs = _plan_json(
                _run(),
                _request(
                    {"x-forwarded-host": "terrapod.example.com", "x-forwarded-proto": "https"}
                ),
            )["data"]["attributes"]
        assert attrs["log-read-url"].startswith(f"{PUBLIC}/api/v2/plans/")
        assert attrs["json-output"].startswith(f"{PUBLIC}/api/v2/plans/")

    def test_apply_log_read_url(self):
        with _localhost_default():
            attrs = _apply_json(
                _run(),
                _request(
                    {"x-forwarded-host": "terrapod.example.com", "x-forwarded-proto": "https"}
                ),
            )["data"]["attributes"]
        assert attrs["log-read-url"].startswith(f"{PUBLIC}/api/v2/applies/")

    def test_a_plain_host_header_is_used_without_a_proxy(self):
        with _localhost_default():
            attrs = _cv_json(_cv(), _request({"host": "terrapod.example.com"}))["data"][
                "attributes"
            ]
        assert attrs["upload-url"].startswith("http://terrapod.example.com/")


class TestTheSettingIsStillTheLastResort:
    """Additive: nothing that worked before stops working."""

    def test_no_request_falls_back_to_the_setting(self):
        with patch("terrapod.config.settings.auth.callback_base_url", "https://configured.example"):
            attrs = _cv_json(_cv())["data"]["attributes"]
        assert attrs["upload-url"].startswith("https://configured.example/")

    def test_an_in_cluster_host_falls_back_to_the_setting(self):
        # `terrapod-api:8000` resolves only inside the cluster; emitting it
        # would publish a URL no external client can reach.
        with patch("terrapod.config.settings.auth.callback_base_url", "https://configured.example"):
            attrs = _cv_json(_cv(), _request({"host": "terrapod-api:8000"}))["data"]["attributes"]
        assert attrs["upload-url"].startswith("https://configured.example/")

    def test_a_malformed_forwarded_host_is_not_echoed(self):
        with patch("terrapod.config.settings.auth.callback_base_url", "https://configured.example"):
            attrs = _cv_json(_cv(), _request({"x-forwarded-host": "evil.com/x y"}))["data"][
                "attributes"
            ]
        assert attrs["upload-url"].startswith("https://configured.example/")


class TestTheRoutesPassTheRequest:
    """The serializers accept a request; these prove the handlers hand one over."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.config_versions.run_service.get_configuration_version")
    async def test_show_configuration_version(self, mock_get_cv, *_):
        mock_get_cv.return_value = _cv()
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            email="u@example.com",
            display_name="u",
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        with _localhost_default():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(
                    f"/api/v2/configuration-versions/cv-{uuid.uuid4()}",
                    headers={
                        "Authorization": "Bearer x",
                        "X-Forwarded-Host": "terrapod.example.com",
                        "X-Forwarded-Proto": "https",
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["upload-url"].startswith(PUBLIC)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_show_plan_by_id(self, mock_get_run, mock_caps, *_):
        run = _run()
        mock_get_run.return_value = run
        mock_caps.return_value = caps_for_level("read")
        db = AsyncMock()
        db.get.return_value = MagicMock()
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            email="u@example.com",
            display_name="u",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app.dependency_overrides[get_db] = lambda: db
        with _localhost_default():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get(
                    f"/api/v2/plans/plan-{run.id}",
                    headers={
                        "Authorization": "Bearer x",
                        "X-Forwarded-Host": "terrapod.example.com",
                        "X-Forwarded-Proto": "https",
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["log-read-url"].startswith(PUBLIC)
