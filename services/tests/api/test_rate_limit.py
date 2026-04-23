"""Tests for rate limiting middleware."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from terrapod.api.rate_limit import RateLimitMiddleware, _get_client_ip, _is_auth_path
from terrapod.auth.runner_tokens import generate_runner_token


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
    authenticated_rpm: int = 1000,
    runner_rpm: int = 0,
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
        authenticated_requests_per_minute=authenticated_rpm,
        runner_requests_per_minute=runner_rpm,
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

    def test_runner_token_default_unlimited_bypasses_redis(self):
        """Valid runner token with default (0) runner limit skips Redis entirely."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, runner_rpm=0)
        client = TestClient(app)
        token = generate_runner_token(uuid.uuid4())
        for _ in range(10):
            response = client.get(
                "/api/v2/workspaces", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
        # Bypass path must not touch Redis
        mock_redis.pipeline.assert_not_called()

    def test_runner_token_respects_configured_limit(self):
        """When runner_rpm > 0, runner traffic is metered on its own bucket."""
        mock_redis = _make_redis_mock(count=6)
        app = _make_app(get_redis=lambda: mock_redis, runner_rpm=5, rpm=100)
        client = TestClient(app)
        token = generate_runner_token(uuid.uuid4())
        response = client.get("/api/v2/workspaces", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 429
        # Verify the runner-specific key prefix was used
        incr_call = mock_redis.pipeline.return_value.incr.call_args
        assert "api_runner" in incr_call[0][0]

    def test_bogus_runner_token_falls_back_to_authenticated_tier(self):
        """A Bearer header that looks like a runner token but fails HMAC
        must not grant the runner tier — it falls through to authenticated."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, runner_rpm=0, authenticated_rpm=10)
        client = TestClient(app)
        response = client.get(
            "/api/v2/workspaces",
            headers={"Authorization": "Bearer runtok:bogus:3600:0:deadbeef"},
        )
        assert response.status_code == 200
        # Should have hit the authenticated bucket, not bypassed
        mock_redis.pipeline.assert_called_once()
        incr_call = mock_redis.pipeline.return_value.incr.call_args
        assert "api_authn" in incr_call[0][0]
        assert response.headers["X-Ratelimit-Limit"] == "10"

    def test_authenticated_header_uses_higher_tier(self):
        """Any Authorization header (non-runner) uses the authenticated bucket."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5, authenticated_rpm=500)
        client = TestClient(app)
        response = client.get(
            "/api/v2/workspaces", headers={"Authorization": "Bearer some-api-token"}
        )
        assert response.status_code == 200
        incr_call = mock_redis.pipeline.return_value.incr.call_args
        assert "api_authn" in incr_call[0][0]
        assert response.headers["X-Ratelimit-Limit"] == "500"

    def test_unauthenticated_uses_base_tier(self):
        """Requests with no Authorization header use the base bucket."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5, authenticated_rpm=500)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert response.status_code == 200
        incr_call = mock_redis.pipeline.return_value.incr.call_args
        key = incr_call[0][0]
        assert ":api:" in key
        assert "api_authn" not in key
        assert response.headers["X-Ratelimit-Limit"] == "5"

    def test_zero_limit_means_unlimited(self):
        """rpm=0 should bypass the Redis bucket entirely."""
        mock_redis = _make_redis_mock(count=9999)
        app = _make_app(get_redis=lambda: mock_redis, rpm=0)
        client = TestClient(app)
        response = client.get("/api/v2/workspaces")
        assert response.status_code == 200
        mock_redis.pipeline.assert_not_called()

    def test_auth_endpoint_limit_applies_to_runner_tokens_too(self):
        """Auth endpoints use auth_rpm regardless of caller — runners included."""
        mock_redis = _make_redis_mock(count=3)
        app = _make_app(get_redis=lambda: mock_redis, auth_rpm=2, runner_rpm=0)
        client = TestClient(app)
        token = generate_runner_token(uuid.uuid4())
        response = client.post("/api/v2/auth/login", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 429
