"""Tests for /health + /ready, including the schema-skew guard (#544)."""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app

_BASE = "http://test"


def _app():
    return create_app()


class TestReady:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.health.schema_is_current", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_storage_or_none")
    @patch("terrapod.api.health.get_redis_health", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_db_health", new_callable=AsyncMock)
    async def test_ready_all_healthy_200(
        self, mock_db, mock_redis, mock_storage, mock_schema, *app_mocks
    ):
        mock_db.return_value = True
        mock_redis.return_value = True
        mock_storage.return_value = object()
        mock_schema.return_value = (True, "abc123")

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["checks"]["migrations"] == "healthy"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.health.schema_is_current", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_storage_or_none")
    @patch("terrapod.api.health.get_redis_health", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_db_health", new_callable=AsyncMock)
    async def test_ready_schema_behind_503(
        self, mock_db, mock_redis, mock_storage, mock_schema, *app_mocks
    ):
        """Everything else healthy, but the schema is behind the code head — the
        pod must report NOT READY so the LB pulls it (#544)."""
        mock_db.return_value = True
        mock_redis.return_value = True
        mock_storage.return_value = object()
        mock_schema.return_value = (False, "schema at old000, code head new999")

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not ready"
        assert body["checks"]["migrations"].startswith("behind:")

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.health.schema_is_current", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_storage_or_none")
    @patch("terrapod.api.health.get_redis_health", new_callable=AsyncMock)
    @patch("terrapod.api.health.get_db_health", new_callable=AsyncMock)
    async def test_ready_db_down_skips_schema_check(
        self, mock_db, mock_redis, mock_storage, mock_schema, *app_mocks
    ):
        """When the DB is down, readiness already fails — and the schema check is
        skipped (it can't query alembic_version) rather than mislabelled."""
        mock_db.return_value = False
        mock_redis.return_value = True
        mock_storage.return_value = object()

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get("/ready")
        assert resp.status_code == 503
        assert "migrations" not in resp.json()["checks"]
        mock_schema.assert_not_called()


class TestHealth:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_health_liveness_200(self, *app_mocks):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"
