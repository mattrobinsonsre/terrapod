"""Redis-backed sliding window rate limiter middleware.

Multi-replica safe — uses Redis INCR + EXPIRE for distributed counting.
Disabled by default; enable via config.rate_limit.enabled = true.
"""

import hashlib
import re
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

# Auth endpoint prefixes (lower rate limit). Auth is Terrapod-native:
# canonical /api/terrapod/v1/auth/* (the OAuth/SAML callback included —
# its URL is built from terrapod_prefix as of #278) plus the /oauth/*
# terraform-login flow.
_AUTH_PREFIXES = ("/api/terrapod/v1/auth/", "/oauth/")


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


# Capability-scoped read endpoints: the run/plan/apply UUID in the PATH is the
# capability (go-tfe's LogReader and the web log viewer poll these anonymously —
# no bearer — matching the state-upload capability pattern). They are polled at
# high frequency while a run streams (~every 2.5s), so keying them on the source
# IP collapses every browser's stream into ONE anonymous bucket behind the BFF
# (the API sees a single web-pod IP) — which live log-tailing then exhausts,
# 429-ing the poll so the log freezes and never recovers (#1075). Bucketing on
# the path UUID instead gives each run's stream its own budget, isolating runs
# from each other and from the shared source IP.
_CAPABILITY_PATH_RE = re.compile(r"^/api/v2/(?:plans|applies)/([^/]+)/(?:log|json-output)$")


def _capability_bucket(path: str) -> str | None:
    """Per-capability bucket id for UUID-scoped anonymous polling endpoints.

    Returns ``cap:{id}`` for the plan/apply log + plan json-output readers (the
    id in the path IS the capability, not a secret — no hashing needed), or None
    for any other path so the caller falls back to credential/IP keying.
    """
    m = _CAPABILITY_PATH_RE.match(path)
    if m is None:
        return None
    return "cap:" + m.group(1)


def _credential_bucket(auth_header: str, listener_cert: str) -> str | None:
    """A stable, non-reversible per-principal bucket id from the credential.

    For AUTHENTICATED traffic the rate-limit bucket must key on WHO is calling,
    not on the source IP: behind the BFF/ingress the API sees a single internal
    pod IP for every browser, so an IP-keyed authenticated tier collapses the
    per-user limit into one shared global bucket — which a single live run's
    log-polling (every 2.5s) then exhausts, 429-ing everyone's log streams
    (#1075). Keying on a hash of the bearer token / client cert gives each
    principal its own budget regardless of the shared source IP. Returns None
    when there is no credential (fall back to IP for the unauthenticated tier).
    """
    cred = auth_header or listener_cert
    if not cred:
        return None
    return "cred:" + hashlib.sha256(cred.encode("utf-8")).hexdigest()[:20]


class RateLimitMiddleware:
    """Sliding window rate limiter using Redis.

    Pure ASGI middleware for correct async behavior.

    Tiers:
    - Capability reads (`/api/v2/{plans,applies}/{id}/log`, `.../json-output`):
      `authenticated_requests_per_minute`, bucketed per-run on the path UUID
      (the capability). These authenticate by the UUID in the path — not a
      header — and are polled continuously while a run streams, so they must
      NOT share the unauthenticated IP bucket (which behind the BFF is one
      global bucket that live log-tailing exhausts, freezing the log — #1075).
    - Runner tokens (HMAC-verified inline): `runner_requests_per_minute`
      (default 0 = unlimited). Runners are service-to-service callers and
      burst through the network-mirror and artifact endpoints during
      `tofu init`/`apply`; a low limit starves them.
    - Authenticated (any `Authorization` header): `authenticated_requests_per_minute`,
      bucketed per-principal on a hash of the credential (NOT the source IP —
      behind the BFF every browser shares one pod IP, so IP-keying collapses
      the whole tier into one bucket, #1075).
      Interactive users and API-token automation rarely approach this, but
      it stops one noisy client taking the pool.
    - Unauthenticated: base limit (`requests_per_minute`), IP-keyed.
    - Auth endpoints (`/api/terrapod/v1/auth/*`, `/oauth/*`): always `auth_requests_per_minute`
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
        # Any Authorization header OR an X-Terrapod-Client-Cert header
        # bumps the tier. We don't verify the credential here — the
        # downstream auth dependency will 401 bogus tokens / certs — but
        # presence is enough to separate interactive / machine-
        # integration traffic from unauthenticated traffic. (The web UI
        # sends Bearer tokens from localStorage; listener pods send
        # X-Terrapod-Client-Cert with their X.509 cert; there is no
        # session cookie in Terrapod.)
        listener_cert = request.headers.get("x-terrapod-client-cert", "")
        is_authenticated = bool(auth_header) or bool(listener_cert)

        # A UUID-scoped log/json-output reader authenticates by the path
        # capability and is polled continuously while a run streams. It gets its
        # own per-run bucket (not the shared anonymous IP bucket) at the
        # authenticated limit — the capability is a real principal, just carried
        # in the path rather than a header (#1075).
        capability = _capability_bucket(path)

        if capability is not None:
            limit = self.authenticated_requests_per_minute
            prefix = "api_capability"
        elif is_auth_endpoint:
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

        # Bucket by WHO, not WHERE, for authenticated traffic — the source IP is
        # the shared BFF/ingress pod IP behind the proxy, so an IP-keyed
        # authenticated tier is one global bucket (#1075). Unauthenticated
        # traffic (login, anon) has no credential and stays IP-keyed.
        identity = (
            capability or _credential_bucket(auth_header, listener_cert) or _get_client_ip(request)
        )

        # Sliding window: 60-second buckets
        window_id = int(time.time()) // 60
        key = f"tp:ratelimit:{prefix}:{identity}:{window_id}"

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
