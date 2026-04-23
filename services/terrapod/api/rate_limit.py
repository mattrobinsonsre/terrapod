"""Redis-backed sliding window rate limiter middleware.

Multi-replica safe — uses Redis INCR + EXPIRE for distributed counting.
Disabled by default; enable via config.rate_limit.enabled = true.
"""

import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from terrapod.auth.runner_tokens import verify_runner_token
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Paths exempt from rate limiting
_EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})

# Auth endpoint prefixes (lower rate limit)
_AUTH_PREFIXES = ("/api/v2/auth/", "/oauth/")


def _is_auth_path(path: str) -> bool:
    """Check if a path is an auth endpoint."""
    return any(path.startswith(p) for p in _AUTH_PREFIXES)


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind a proxy."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class RateLimitMiddleware:
    """Sliding window rate limiter using Redis.

    Pure ASGI middleware for correct async behavior.

    Tiers:
    - Runner tokens (HMAC-verified inline): exempt entirely. Runners are
      service-to-service callers and burst through the network-mirror and
      artifact endpoints during `tofu init`/`apply`; 100/min starves them.
    - Authenticated (API token / session / any `Authorization` header):
      higher limit (`authenticated_requests_per_minute`). Interactive users
      rarely approach this, but it stops one noisy client taking the pool.
    - Unauthenticated: base limit (`requests_per_minute`).
    - Auth endpoints (`/api/v2/auth/*`, `/oauth/*`): always `auth_requests_per_minute`
      regardless of who's calling — brute-force defence on login.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 100,
        authenticated_requests_per_minute: int = 1000,
        runner_requests_per_minute: int = 0,
        auth_requests_per_minute: int = 10,
        get_redis: Callable | None = None,
    ) -> None:
        self.app = app
        self.requests_per_minute = requests_per_minute
        self.authenticated_requests_per_minute = authenticated_requests_per_minute
        self.runner_requests_per_minute = runner_requests_per_minute
        self.auth_requests_per_minute = auth_requests_per_minute
        self._get_redis = get_redis

    def _resolve_redis(self):  # type: ignore[no-untyped-def]
        """Get the Redis client, using injected callable or default."""
        if self._get_redis is not None:
            return self._get_redis()
        from terrapod.redis.client import get_redis_client

        return get_redis_client()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # Identify the request tier. HMAC verification of runner tokens is
        # pure (no DB / Redis), cheap enough to run in middleware.
        auth_header = request.headers.get("authorization", "")
        is_runner = False
        if auth_header.startswith("Bearer runtok:"):
            token = auth_header.removeprefix("Bearer ").strip()
            is_runner = verify_runner_token(token) is not None

        is_auth_endpoint = _is_auth_path(path)
        # Any Authorization header or session cookie bumps the tier.
        # We don't verify the credential here — the downstream auth
        # dependency will 401 bogus tokens — but presence is enough to
        # separate interactive / machine-integration traffic from
        # unauthenticated traffic.
        is_authenticated = bool(auth_header) or "terrapod_session" in request.cookies

        if is_auth_endpoint:
            limit = self.auth_requests_per_minute
            prefix = "auth"
        elif is_runner:
            limit = self.runner_requests_per_minute
            prefix = "api_runner"
        elif is_authenticated:
            limit = self.authenticated_requests_per_minute
            prefix = "api_authn"
        else:
            limit = self.requests_per_minute
            prefix = "api"

        # 0 means unlimited for this tier — skip the bucket entirely.
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        try:
            redis = self._resolve_redis()
        except RuntimeError:
            # Redis not initialized — fail open
            await self.app(scope, receive, send)
            return

        client_ip = _get_client_ip(request)

        # Sliding window: 60-second buckets
        window_id = int(time.time()) // 60
        key = f"tp:ratelimit:{prefix}:{client_ip}:{window_id}"

        try:
            pipe = redis.pipeline(transaction=False)
            pipe.incr(key)
            pipe.expire(key, 120)  # 2 minutes TTL for cleanup
            results = await pipe.execute()
            count = results[0]
        except Exception:
            logger.warning("Rate limit Redis error, failing open", exc_info=True)
            await self.app(scope, receive, send)
            return

        if count > limit:
            retry_after = 60 - (int(time.time()) % 60)
            response = JSONResponse(
                status_code=429,
                content={"errors": [{"status": "429", "title": "Rate limit exceeded"}]},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # Inject rate limit headers into response
        original_send = send

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(limit).encode()))
                headers.append((b"x-ratelimit-remaining", str(max(0, limit - count)).encode()))
                message = {**message, "headers": headers}
            await original_send(message)

        await self.app(scope, receive, send_with_headers)
