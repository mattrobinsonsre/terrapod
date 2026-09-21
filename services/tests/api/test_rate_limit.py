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

    def test_get_client_ip_ignores_forwarded_from_an_untrusted_peer(self):
        """X-Forwarded-For is NOT respected by default.

        This used to assert `1.2.3.4` — the left-most entry, straight out of a
        caller-supplied header (GHSA-wq2j-pppw-ff2p). Anyone could pick their
        own rate-limit bucket by sending one, and the credential-churn ceiling
        keys on this value, so it also removed the bound on the login tier.
        With no trusted proxy configured the header is ignored entirely.
        """
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
            "query_string": b"",
            "client": ("10.0.0.1", 12345),
        }
        request = Request(scope)
        assert _get_client_ip(request) == "10.0.0.1"

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

    @app.get("/api/terrapod/v1/package-cache/nuget/flat/{name}/index.json")
    async def package_cache(name: str):
        return {"versions": []}

    @app.get("/storage/get/{key:path}")
    async def storage_get(key: str):
        return {"key": key}

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


class TestForwardedForIsOnlyBelievedFromATrustedProxy:
    """GHSA-wq2j-pppw-ff2p, second half.

    `X-Forwarded-For` is caller-supplied. Reading it unconditionally let any
    caller choose its own rate-limit bucket, and since the credential-churn
    ceiling is keyed on this value, that removed the bound on the login
    finding rather than being a lesser issue beside it.

    Reading a *different entry* would not have fixed it: a caller who supplies
    the header supplies every entry in it. Only the peer can be trusted, so the
    header is believed only when the peer is a configured proxy.
    """

    def _req(self, header, peer="10.0.0.1"):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "headers": [(b"x-forwarded-for", header.encode())] if header else [],
            "client": (peer, 1234),
        }
        return Request(scope)

    def _nets(self, *cidrs):
        from terrapod.api.rate_limit import _parse_networks

        return _parse_networks(list(cidrs))

    def test_the_default_ignores_the_header_entirely(self):
        """Nothing trusted → the header is data, not identity."""
        from terrapod.api.rate_limit import _get_client_ip

        assert _get_client_ip(self._req("1.2.3.4, 5.6.7.8")) == "10.0.0.1"

    def test_an_untrusted_peer_cannot_spoof_even_when_cidrs_are_configured(self):
        """The in-cluster caller that bypasses the ingress gets nothing."""
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("192.168.50.0/24")
        assert _get_client_ip(self._req("1.2.3.4", peer="10.0.0.99"), nets) == "10.0.0.99"

    def test_a_trusted_peer_yields_the_right_most_untrusted_hop(self):
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("10.0.0.0/8")
        # Client, then two trusted proxies that appended as it passed through.
        req = self._req("203.0.113.7, 10.0.0.5, 10.0.0.6", peer="10.0.0.1")
        assert _get_client_ip(req, nets) == "203.0.113.7"

    def test_a_forged_prefix_from_a_real_client_is_skipped_not_believed(self):
        """The client prepends; the trusted proxy appends the truth."""
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("10.0.0.0/8")
        req = self._req("1.2.3.4, 203.0.113.7, 10.0.0.5", peer="10.0.0.1")
        assert _get_client_ip(req, nets) == "203.0.113.7"

    def test_a_single_trusted_hop_matches_the_shipped_topology(self):
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("10.0.0.0/8")
        assert _get_client_ip(self._req("203.0.113.7", peer="10.0.0.1"), nets) == "203.0.113.7"

    def test_an_all_trusted_chain_falls_back_to_the_peer(self):
        """No untrusted hop means no client to attribute — never guess one."""
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("10.0.0.0/8")
        assert _get_client_ip(self._req("10.0.0.5, 10.0.0.6", peer="10.0.0.1"), nets) == "10.0.0.1"

    def test_garbage_entries_are_not_trusted_and_do_not_raise(self):
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("10.0.0.0/8")
        # "not-an-ip" is not parseable, so it is not a trusted proxy, so it is
        # returned as the right-most untrusted hop. It cannot masquerade as one.
        assert _get_client_ip(self._req("1.2.3.4, not-an-ip", peer="10.0.0.1"), nets) == "not-an-ip"

    def test_ipv6_peers_and_hops(self):
        from terrapod.api.rate_limit import _get_client_ip

        nets = self._nets("fd00::/8")
        req = self._req("2001:db8::1, fd00::5", peer="fd00::1")
        assert _get_client_ip(req, nets) == "2001:db8::1"

    def test_an_unparseable_cidr_is_dropped_not_fatal(self):
        """A typo in config must not take the API down, and must not widen trust."""
        nets = self._nets("10.0.0.0/8", "nonsense", "")
        assert len(nets) == 1


class TestTheLoginTierIsKeyedOnTheSourceAlone:
    """GHSA-wq2j-pppw-ff2p. The login limit must not inherit the churn ceiling.

    Per-credential bucketing (#1075) is right for the AUTHENTICATED tier, where
    200 distinct principals a minute behind one BFF pod IP is a sane floor. The
    unauthenticated login tier inherited it, because the bucket key was chosen
    before the tier was consulted — so a password-guesser who sent a fresh
    random bearer on every attempt minted a new bucket each time and got the
    churn allowance instead of this tier's limit.
    """

    def _bucket_keys(self, mock_redis):
        return [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]

    def test_rotating_the_bearer_does_not_mint_a_new_login_bucket(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, auth_rpm=1000)
        client = TestClient(app)

        client.post("/api/terrapod/v1/auth/login", headers={"Authorization": "Bearer RANDOM-1"})
        client.post("/api/terrapod/v1/auth/login", headers={"Authorization": "Bearer RANDOM-2"})

        keys = self._bucket_keys(mock_redis)
        assert len(keys) == 2
        assert keys[0] == keys[1], (
            "a different bearer per attempt minted a different login bucket, so the "
            f"login limit never applies: {keys}"
        )
        assert all(k.startswith("tp:ratelimit:auth:") for k in keys), keys
        assert not any("cred:" in k for k in keys), (
            f"the login tier must not key on an unverified credential: {keys}"
        )

    def test_a_login_with_no_credential_shares_that_same_bucket(self):
        """An honest attempt and a bearer-carrying one are the same source."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, auth_rpm=1000)
        client = TestClient(app)

        client.post("/api/terrapod/v1/auth/login")
        client.post("/api/terrapod/v1/auth/login", headers={"Authorization": "Bearer ANY"})

        keys = self._bucket_keys(mock_redis)
        assert keys[0] == keys[1], keys

    def test_the_authenticated_tier_still_buckets_per_credential(self):
        """The #1075 fix must survive: this narrows the login tier only."""
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        client = TestClient(app)

        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer AAA"})
        client.get("/api/terrapod/v1/workspaces", headers={"Authorization": "Bearer BBB"})

        keys = self._bucket_keys(mock_redis)
        assert keys[0] != keys[1], keys
        assert all(k.startswith("tp:ratelimit:api_authn:cred:") for k in keys), keys


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
    """Log readers carry a signed capability in the path and are polled while a
    run streams, with no credential to key on. They must bucket per-capability —
    NOT on the shared BFF source IP, which would collapse every browser's log
    stream into one anonymous bucket that live tailing exhausts, freezing the
    log (#1075).
    """

    def test_capability_bucket_pure(self):
        from terrapod.api.rate_limit import _capability_bucket

        assert _capability_bucket("/api/v2/applies/cap-x.y/log") is not None
        assert _capability_bucket("/api/v2/plans/cap-x.y/log") is not None
        # Distinct capabilities → distinct buckets.
        assert _capability_bucket("/api/v2/applies/run-A/log") != _capability_bucket(
            "/api/v2/applies/run-B/log"
        )
        # Non-capability paths fall through to credential/IP keying.
        assert _capability_bucket("/api/terrapod/v1/workspaces") is None
        assert _capability_bucket("/api/v2/workspaces/ws-1") is None
        # json-output takes an ordinary credential now, so it keys on that.
        assert _capability_bucket("/api/v2/plans/run-abc/json-output") is None

    def test_both_prefixes_bucket(self):
        # The TFE surface's canonical prefix is /api/tfe/v2 and /api/v2 is its
        # deprecated alias. Matching only one meant a CLI on the canonical
        # prefix fell through to the anonymous per-IP bucket and re-created
        # #1075 — the bug this function exists to prevent.
        from terrapod.api.rate_limit import _capability_bucket

        assert _capability_bucket("/api/tfe/v2/plans/cap-x.y/log") is not None
        assert _capability_bucket("/api/tfe/v2/applies/cap-x.y/log") is not None
        assert _capability_bucket("/api/tfe/v2/plans/cap-x.y/log") == _capability_bucket(
            "/api/v2/plans/cap-x.y/log"
        )

    def test_the_capability_is_not_stored_verbatim_in_the_key(self):
        # The bucket id goes into a Redis key that outlives the request. The
        # segment used to be a bare run id; it is now the credential itself.
        from terrapod.api.rate_limit import _capability_bucket

        secret = "cap-abcdef.signaturevalue"
        bucket = _capability_bucket(f"/api/v2/plans/{secret}/log")
        assert bucket is not None
        assert secret not in bucket
        assert "signaturevalue" not in bucket

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


class TestPackageCacheChallenges:
    """An unauthenticated package-cache request is a 401 challenge, and that is
    how the npm/pip/NuGet clients authenticate -- one probe per package, which
    NuGet cannot be configured out of. Charged to the public per-IP bucket, a
    restore exhausts it, the probes start answering 429 instead of 401, and the
    client never reaches the authenticated retry: every package fails (#1566).
    """

    def test_challenge_path_pure(self):
        from terrapod.api.rate_limit import _is_challenge_path

        assert _is_challenge_path("/api/v1/package-cache/nuget/index.json") is True
        # The deprecated alias is the one our own runners still call, so it must
        # normalise to the same answer rather than falling to the public bucket.
        assert _is_challenge_path("/api/terrapod/v1/package-cache/npm/left-pad") is True
        assert _is_challenge_path("/api/v1/workspaces") is False

    def test_an_anonymous_probe_gets_the_authenticated_limit(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        resp = TestClient(app).get("/api/terrapod/v1/package-cache/nuget/flat/pulumi/index.json")
        assert resp.headers.get("x-ratelimit-limit") == "1000"
        keys = [c.args[0] for c in mock_redis.pipeline.return_value.incr.call_args_list]
        assert keys[0].startswith("tp:ratelimit:api_challenge:")

    def test_a_restore_sized_burst_is_not_throttled(self):
        # The regression in one line: more probes than the public limit, which
        # is what a real dependency tree costs, and none of them 429.
        mock_redis = MagicMock()
        pipe = MagicMock()
        counter = {"n": 0}

        def _execute():
            counter["n"] += 1
            return [counter["n"], True]

        pipe.execute = AsyncMock(side_effect=_execute)
        mock_redis.pipeline.return_value = pipe
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        client = TestClient(app)
        for i in range(150):
            r = client.get(f"/api/terrapod/v1/package-cache/nuget/flat/pkg{i}/index.json")
            assert r.status_code == 200, f"throttled at probe {i}"

    def test_everything_else_still_gets_the_public_limit(self):
        # The carve-out is scoped to package-cache; it must not raise the
        # ceiling on the rest of the unauthenticated surface.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        resp = TestClient(app).get("/api/terrapod/v1/workspaces")
        assert resp.headers.get("x-ratelimit-limit") == "100"

    def test_an_authenticated_request_is_still_charged_to_its_credential(self):
        # The challenge tier is for requests with NO credential. Once the client
        # retries with one, it goes back to being charged to the principal.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, authenticated_rpm=1000)
        TestClient(app).get(
            "/api/terrapod/v1/package-cache/nuget/flat/pulumi/index.json",
            headers={"Authorization": "Basic eDp0b2tlbg=="},
        )
        keys = [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]
        assert keys[0].startswith("tp:ratelimit:api_authn:cred:")


class TestPresignedBucketing:
    """A presigned URL's HMAC signature is a credential carried in the query
    string. It is fetched anonymously by design and in bursts, so keying it on
    the source IP throttles a filesystem-backed deployment's own runners.
    """

    def test_presigned_bucket_pure(self):
        from terrapod.api.rate_limit import _presigned_bucket

        a = _presigned_bucket("/storage/get/cache/x.tgz", "expires=1&sig=AAA")
        b = _presigned_bucket("/storage/get/cache/y.tgz", "expires=1&sig=BBB")
        assert a is not None and b is not None and a != b
        # Same signature, same bucket -- a retried download is the same thing.
        assert a == _presigned_bucket("/storage/get/cache/x.tgz", "sig=AAA&expires=1")
        # The signature is the credential, so it is hashed, never echoed.
        assert "AAA" not in a
        # No signature and no storage path both fall back to IP keying.
        assert _presigned_bucket("/storage/get/cache/x.tgz", "expires=1") is None
        assert _presigned_bucket("/api/v1/workspaces", "sig=AAA") is None

    def test_two_presigned_downloads_do_not_share_a_bucket(self):
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        client = TestClient(app)
        client.get("/storage/get/cache/a.tgz?expires=9&sig=SIGA")
        client.get("/storage/get/cache/b.tgz?expires=9&sig=SIGB")
        keys = [
            c.args[0]
            for c in mock_redis.pipeline.return_value.incr.call_args_list
            if ":churn:" not in c.args[0]
        ]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert all(k.startswith("tp:ratelimit:api_capability:sig:") for k in keys)

    def test_an_unsigned_storage_request_stays_on_the_public_bucket(self):
        # Only a signature Terrapod minted earns the generous bucket; a request
        # without one is ordinary anonymous traffic and is refused downstream.
        mock_redis = _make_redis_mock(count=1)
        app = _make_app(get_redis=lambda: mock_redis, rpm=100, authenticated_rpm=1000)
        resp = TestClient(app).get("/storage/get/cache/a.tgz")
        assert resp.headers.get("x-ratelimit-limit") == "100"
