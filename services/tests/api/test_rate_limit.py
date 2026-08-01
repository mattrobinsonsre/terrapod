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
        assert _is_auth_path("/api/terrapod/v1/auth/login") is True
        assert _is_auth_path("/api/terrapod/v1/auth/callback") is True
        assert _is_auth_path("/oauth/authorize") is True
        assert _is_auth_path("/api/terrapod/v1/workspaces") is False
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


def _tier_key(mock_redis) -> str:  # type: ignore[no-untyped-def]
    """The tier bucket key — always the FIRST incr of a request.

    Authenticated requests that mint a new credential bucket issue a SECOND
    incr for the credential-churn ceiling, so `call_args` (the last call) is
    not the tier key.
    """
    return mock_redis.pipeline.return_value.incr.call_args_list[0].args[0]


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
    distinct_credentials_rpm: int = 200,
) -> FastAPI:
    """Create a minimal FastAPI app with rate limiting middleware."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/terrapod/v1/workspaces")
    async def workspaces():
        return {"data": []}

    @app.post("/api/terrapod/v1/auth/login")
    async def login():
        return {"token": "test"}

    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=rpm,
        authenticated_requests_per_minute=authenticated_rpm,
        runner_requests_per_minute=runner_rpm,
        auth_requests_per_minute=auth_rpm,
        distinct_credentials_per_minute=distinct_credentials_rpm,
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
        response = client.get("/api/terrapod/v1/workspaces")
        assert "X-Ratelimit-Limit" in response.headers
        assert "X-Ratelimit-Remaining" in response.headers
        assert response.headers["X-Ratelimit-Limit"] == "5"
        assert response.headers["X-Ratelimit-Remaining"] == "4"

    def test_rate_limit_429_response(self):
        """Returns 429 when limit is exceeded."""
        mock_redis = _make_redis_mock(count=6)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5)
        client = TestClient(app)
        response = client.get("/api/terrapod/v1/workspaces")
        assert response.status_code == 429
        assert "Retry-After" in response.headers
        body = response.json()
        assert body["errors"][0]["status"] == "429"

    def test_auth_endpoint_uses_lower_limit(self):
        """Auth endpoints use the auth-specific rate limit."""
        mock_redis = _make_redis_mock(count=3)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, auth_rpm=2)
        client = TestClient(app)
        response = client.post("/api/terrapod/v1/auth/login")
        assert response.status_code == 429

    def test_redis_failure_fails_open(self):
        """Redis errors fail open (request is allowed)."""
        mock_redis = _make_redis_mock(error=Exception("Redis down"))
        app = _make_app(get_redis=lambda: mock_redis)
        client = TestClient(app)
        response = client.get("/api/terrapod/v1/workspaces")
        assert response.status_code == 200

    def test_redis_not_initialized_fails_open(self):
        """When Redis is not initialized, requests pass through."""

        def raise_runtime_error():
            raise RuntimeError("Not initialized")

        app = _make_app(get_redis=raise_runtime_error)
        client = TestClient(app)
        response = client.get("/api/terrapod/v1/workspaces")
        assert response.status_code == 200

    def test_runner_token_default_unlimited_bypasses_redis(self):
        """Valid runner token with default (0) runner limit skips Redis entirely."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, runner_rpm=0)
        client = TestClient(app)
        token = generate_runner_token(uuid.uuid4())
        for _ in range(10):
            response = client.get(
                "/api/terrapod/v1/workspaces", headers={"Authorization": f"Bearer {token}"}
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
        response = client.get(
            "/api/terrapod/v1/workspaces", headers={"Authorization": f"Bearer {token}"}
        )
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
            "/api/terrapod/v1/workspaces",
            headers={"Authorization": "Bearer runtok:bogus:3600:0:deadbeef"},
        )
        assert response.status_code == 200
        # Should have hit the authenticated bucket, not bypassed
        assert "api_authn" in _tier_key(mock_redis)
        assert response.headers["X-Ratelimit-Limit"] == "10"

    def test_authenticated_header_uses_higher_tier(self):
        """Any Authorization header (non-runner) uses the authenticated bucket."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5, authenticated_rpm=500)
        client = TestClient(app)
        response = client.get(
            "/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer some-api-token"}
        )
        assert response.status_code == 200
        assert "api_authn" in _tier_key(mock_redis)
        assert response.headers["X-Ratelimit-Limit"] == "500"

    def test_unauthenticated_uses_base_tier(self):
        """Requests with no Authorization header use the base bucket."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5, authenticated_rpm=500)
        client = TestClient(app)
        response = client.get("/api/terrapod/v1/workspaces")
        assert response.status_code == 200
        key = _tier_key(mock_redis)
        assert ":api:" in key
        assert "api_authn" not in key
        assert response.headers["X-Ratelimit-Limit"] == "5"

    def test_listener_cert_uses_authenticated_tier(self):
        """X-Terrapod-Client-Cert (listener auth) maps to authenticated tier.

        Listener pods don't send Authorization; they auth with their X.509
        cert via X-Terrapod-Client-Cert. Without recognising this header,
        listener traffic falls into the unauthenticated 100/min bucket and
        all listeners across the fleet sharing a NAT-source IP DoS each
        other on rollout.
        """
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5, authenticated_rpm=500)
        client = TestClient(app)
        response = client.get(
            "/api/terrapod/v1/workspaces",
            headers={"X-Terrapod-Client-Cert": "base64-cert-bytes-here"},
        )
        assert response.status_code == 200
        assert "api_authn" in _tier_key(mock_redis)
        assert response.headers["X-Ratelimit-Limit"] == "500"

    def test_zero_limit_means_unlimited(self):
        """rpm=0 should bypass the Redis bucket entirely."""
        mock_redis = _make_redis_mock(count=9999)
        app = _make_app(get_redis=lambda: mock_redis, rpm=0)
        client = TestClient(app)
        response = client.get("/api/terrapod/v1/workspaces")
        assert response.status_code == 200
        mock_redis.pipeline.assert_not_called()

    def test_auth_endpoint_limit_applies_to_runner_tokens_too(self):
        """Auth endpoints use auth_rpm regardless of caller — runners included."""
        mock_redis = _make_redis_mock(count=3)
        app = _make_app(get_redis=lambda: mock_redis, auth_rpm=2, runner_rpm=0)
        client = TestClient(app)
        token = generate_runner_token(uuid.uuid4())
        response = client.post(
            "/api/terrapod/v1/auth/login", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 429


class TestCredentialBucketing:
    """Authenticated traffic buckets by credential, not IP (#1075).

    Behind the BFF/ingress every browser shares one internal source IP, so an
    IP-keyed authenticated tier is a single global bucket that a live run's
    log-polling exhausts, 429-ing everyone's log streams. Keying on the bearer
    token / client cert gives each principal its own budget.
    """

    def test_credential_bucket_pure(self):
        from terrapod.api.rate_limit import _credential_bucket

        a = _credential_bucket("Bearer tokenA", "")
        b = _credential_bucket("Bearer tokenB", "")
        again = _credential_bucket("Bearer tokenA", "")
        assert a and b and a != b  # different tokens → different buckets
        assert a == again  # stable per token
        assert a.startswith("cred:") and "tokenA" not in a  # hashed, not reversible
        assert _credential_bucket("", "cert-pem") is not None  # listener cert counts
        assert _credential_bucket("", "") is None  # no credential → fall back to IP

    def test_two_tokens_same_ip_get_separate_buckets(self):
        # Two distinct principals from the SAME source IP (the shared BFF pod IP)
        # must land in DIFFERENT rate-limit buckets.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)

        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer AAA"})
        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer BBB"})

        keys = [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]
        assert len(keys) == 2
        # Same tier prefix + same source IP, but the credential discriminator differs.
        assert keys[0] != keys[1]
        assert all(k.startswith("tp:ratelimit:api_authn:cred:") for k in keys)

    def test_same_token_shares_bucket(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)
        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer SAME"})
        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer SAME"})
        keys = [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]
        # Same token → same bucket (minus the time-window suffix, which is equal here).
        assert keys[0] == keys[1]


class TestCredentialChurnCeiling:
    """Per-credential bucketing (#1075) is only safe with a churn ceiling.

    The credential is deliberately NOT verified in middleware, so a caller who
    sends a different random Authorization value on every request gets a fresh
    bucket each time — count always 1, never limited. That is a total bypass of
    the tier, landing the attacker in a BETTER position than the anonymous
    tier they avoided. The ceiling caps how fast one source IP may mint NEW
    buckets, which rotation trips and ordinary traffic never approaches.
    """

    def test_rotating_credentials_are_blocked_once_churn_exceeds_the_ceiling(self):
        # Every INCR returns 4 — i.e. the per-credential bucket is fresh-ish but
        # the churn counter for this IP has passed the ceiling of 3.
        mock_redis = _make_redis_mock(count=1)
        calls = {"n": 0}

        async def execute():
            # First execute() per request is the credential bucket (count 1 =
            # newly minted); the second is the churn counter, which climbs.
            calls["n"] += 1
            return [1] if calls["n"] % 2 == 1 else [calls["n"]]

        mock_redis.pipeline.return_value.execute = AsyncMock(side_effect=execute)
        app = _make_app(
            get_redis=lambda: mock_redis, authenticated_rpm=1000, distinct_credentials_rpm=3
        )
        client = TestClient(app)

        codes = [
            client.get(
                "/api/terrapod/v1/workspaces", headers={"Authorization": f"Bearer random-{i}"}
            ).status_code
            for i in range(5)
        ]
        # The per-credential counter never exceeds 1, so without the ceiling
        # every one of these would be a 200.
        assert 429 in codes, f"rotating credentials were never limited: {codes}"

    def test_a_repeat_credential_does_not_consume_churn(self):
        # Only a first-in-window bucket (INCR == 1) touches the churn counter, so
        # a real caller reusing one token must not be charged for it repeatedly.
        mock_redis = _make_redis_mock(count=7)  # bucket already exists this window
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)

        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer SAME"})

        keys = [c.args[0] for c in mock_redis.pipeline.return_value.incr.call_args_list]
        assert not any("churn" in k for k in keys), (
            "an existing credential bucket must not consume churn budget"
        )

    def test_anonymous_traffic_does_not_consume_churn(self):
        # Unauthenticated requests are already IP-keyed, so identity == the IP
        # and there is no bucket-minting to police.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=5)
        client = TestClient(app)

        client.get("/api/terrapod/v1/workspaces")

        keys = [c.args[0] for c in mock_redis.pipeline.return_value.incr.call_args_list]
        assert not any("churn" in k for k in keys)

    def test_zero_ceiling_means_unlimited(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(
            get_redis=lambda: mock_redis, authenticated_rpm=1000, distinct_credentials_rpm=0
        )
        client = TestClient(app)
        for i in range(5):
            r = client.get(
                "/api/terrapod/v1/workspaces", headers={"Authorization": f"Bearer random-{i}"}
            )
            assert r.status_code == 200


class TestCapabilityBucketing:
    """Log/json-output readers authenticate by the run UUID in the path and are
    polled while a run streams. They must bucket per-run — NOT on the shared BFF
    source IP, which would collapse every browser's log stream into one
    anonymous bucket that live tailing exhausts, freezing the log (#1075).
    """

    def test_capability_bucket_pure(self):
        from terrapod.api.rate_limit import _capability_bucket

        assert _capability_bucket("/api/v2/applies/run-abc/log") == "cap:run-abc"
        assert _capability_bucket("/api/v2/plans/run-abc/log") == "cap:run-abc"
        assert _capability_bucket("/api/v2/plans/run-abc/json-output") == "cap:run-abc"
        # Distinct runs → distinct buckets.
        assert _capability_bucket("/api/v2/applies/run-A/log") != _capability_bucket(
            "/api/v2/applies/run-B/log"
        )
        # Non-capability paths fall through to credential/IP keying.
        assert _capability_bucket("/api/terrapod/v1/workspaces") is None
        assert _capability_bucket("/api/v2/workspaces/ws-1") is None

    def test_log_reader_buckets_per_run_not_shared_ip(self):
        # Two DIFFERENT runs polled anonymously from the SAME source IP (the
        # shared BFF pod IP) must land in DIFFERENT buckets under the capability
        # tier — so one streaming run cannot 429 another's log.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)

        client.get("/api/v2/applies/run-AAA/log")
        client.get("/api/v2/applies/run-BBB/log")

        keys = [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert all(k.startswith("tp:ratelimit:api_capability:cap:") for k in keys)

    def test_same_run_log_shares_bucket(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)
        client.get("/api/v2/applies/run-SAME/log")
        client.get("/api/v2/applies/run-SAME/log")
        keys = [c.args[0] for c in mock_redis.pipeline.return_value.incr.call_args_list]
        assert keys[0] == keys[1]

    def test_capability_tier_uses_authenticated_limit(self):
        # The capability reader gets the generous authenticated limit, not the
        # low unauthenticated base — live tailing polls it continuously.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        client = TestClient(app)
        resp = client.get("/api/v2/applies/run-XYZ/log")
        assert resp.headers.get("x-ratelimit-limit") == "1000"
