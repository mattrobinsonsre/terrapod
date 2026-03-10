"""Tests for rate limiting middleware."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from terrapod.api.rate_limit import RateLimitMiddleware, _get_client_ip, _is_auth_path


class TestHelpers:
    def test_is_auth_path(self):
        assert _is_auth_path("/api/v2/auth/login") is True
        assert _is_auth_path("/api/v2/auth/callback") is True
        assert _is_auth_path("/oauth/authorize") is True
        assert _is_auth_path("/api/v2/workspaces") is False
        assert _is_auth_path("/health") is False

    def test_get_client_ip_forwarded(self):
        """X-Forwarded-For is respected."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
            "query_string": b"",
        }
        request = Request(scope)
        assert _get_client_ip(request) == "1.2.3.4"

    def test_get_client_ip_direct(self):
        """Falls back to client host."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "client": ("10.0.0.1", 12345),
        }
        request = Request(scope)
        assert _get_client_ip(request) == "10.0.0.1"


def _make_redis_mock(count: int = 1, error: Exception | None = None) -> MagicMock:
    """Create a mock Redis client with configurable pipeline behavior.

    redis.pipeline() is synchronous, pipe.execute() is async.
    """
    mock_redis = MagicMock()
    mock_pipe = MagicMock()
    # incr() and expire() are sync calls on the pipeline (command buffering)
    mock_pipe.incr = MagicMock()
    mock_pipe.expire = MagicMock()
    # execute() is async — runs all buffered commands
    if error:
        mock_pipe.execute = AsyncMock(side_effect=error)
    else:
        mock_pipe.execute = AsyncMock(return_value=[count])
    mock_redis.pipeline.return_value = mock_pipe
    return mock_redis


def _make_app(
    get_redis=None,  # type: ignore[no-untyped-def]
    rpm: int = 5,
    auth_rpm: int = 2,
) -> FastAPI:
    """Create a minimal FastAPI app with rate limiting middleware."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v2/workspaces")
    async def workspaces():
        return {"data": []}

    @app.post("/api/v2/auth/login")
    async def login():
        return {"token": "test"}

    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=rpm,
        auth_requests_per_minute=auth_rpm,
        get_redis=get_redis,
    )
    return app


class TestRateLimitMiddleware:
    def test_exempt_paths_not_rate_limited(self):
        """Health, ready, metrics paths are exempt."""
        mock_redis = _make_redis_mock(count=999)
        app = _make_app(get_redis=lambda: mock_redis)
        client = TestClient(app)
        for _ in range(20):
            response = client.get("/health")
            assert response.status_code == 200

    def test_rate_limit_headers_present(self):
        """Rate limit headers are included in responses."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert "X-Ratelimit-Limit" in response.headers
        assert "X-Ratelimit-Remaining" in response.headers
        assert response.headers["X-Ratelimit-Limit"] == "5"
        assert response.headers["X-Ratelimit-Remaining"] == "4"

    def test_rate_limit_429_response(self):
        """Returns 429 when limit is exceeded."""
        mock_redis = _make_redis_mock(count=6)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert response.status_code == 429
        assert "Retry-After" in response.headers
        body = response.json()
        assert body["errors"][0]["status"] == "429"

    def test_auth_endpoint_uses_lower_limit(self):
        """Auth endpoints use the auth-specific rate limit."""
        mock_redis = _make_redis_mock(count=3)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, auth_rpm=2)
        client = TestClient(app)
        response = client.post("/api/v2/auth/login")
        assert response.status_code == 429

    def test_redis_failure_fails_open(self):
        """Redis errors fail open (request is allowed)."""
        mock_redis = _make_redis_mock(error=Exception("Redis down"))
        app = _make_app(get_redis=lambda: mock_redis)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert response.status_code == 200

    def test_redis_not_initialized_fails_open(self):
        """When Redis is not initialized, requests pass through."""

        def raise_runtime_error():
            raise RuntimeError("Not initialized")

        app = _make_app(get_redis=raise_runtime_error)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert response.status_code == 200
