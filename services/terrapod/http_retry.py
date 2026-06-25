"""Shared bounded backoff/retry for outbound HTTP (#567).

Every Terrapod process that makes outbound HTTP calls — the runner, the
listener, and the API server (to upstream registries / VCS / webhooks) —
routes through one of the two entry points here so the retry policy is
consistent and testable rather than re-implemented per call site:

- ``request_with_retry`` — synchronous, for ``httpx.Client`` (the runner).
- ``arequest_with_retry`` — asynchronous, for ``httpx.AsyncClient`` (the
  listener and the API). Uses ``asyncio.sleep`` for backoff (never blocks the
  event loop — see the no-sync-work-in-async rule).

Both have identical, **method-aware** semantics so a retry can never cause a
double-write:

- Retry transient failures: connection errors, timeouts, and 5xx responses.
- A **4xx is final** and never retried (it's a definitive answer).
- **Idempotent** operations (HTTP GET/HEAD/OPTIONS/PUT/DELETE, or a POST the
  caller marks ``idempotent=True`` because its server handler is idempotent)
  retry on *all* transient failures.
- A **non-idempotent** POST/PATCH retries **only** on a connection error where
  the request provably never reached the server (``ConnectError`` /
  ``ConnectTimeout`` / ``PoolTimeout``) — never on a ``ReadTimeout`` or 5xx,
  which could mean the server already processed it. This is what prevents a
  retry from duplicating a create/mutate.

Both return the ``httpx.Response`` (the caller inspects the status code), or
re-raise the last transport exception if every attempt failed before a
response was received.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog

logger = structlog.get_logger(__name__)

# HTTP methods that are idempotent by spec (RFC 9110 §9.2.2) — safe to retry on
# any transient failure regardless of whether the server saw the first attempt.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Transport errors where the request was NOT delivered to the server, so a
# retry can't double-apply even for a non-idempotent method.
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

DEFAULT_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 8.0


def _is_idempotent(method: str, idempotent: bool | None) -> bool:
    if idempotent is not None:
        return idempotent
    return method.upper() in _IDEMPOTENT_METHODS


def _should_retry(
    method: str,
    idempotent: bool | None,
    *,
    exc: Exception | None = None,
    status: int | None = None,
) -> bool:
    """Decide whether a failed attempt is retriable, method-aware."""
    idem = _is_idempotent(method, idempotent)
    if exc is not None:
        # A connection-class error means the request never reached the server,
        # so retrying is always safe (no double-apply).
        if isinstance(exc, _CONNECT_ERRORS):
            return True
        # Any other transport error (ReadTimeout, WriteTimeout, RemoteProtocol…)
        # may have been delivered — only retry if the operation is idempotent.
        return idem
    if status is not None and status >= 500:
        # 5xx may have partially applied a non-idempotent write — only retry
        # idempotent operations.
        return idem
    return False


def _backoff_seconds(attempt: int, base_delay: float, max_delay: float) -> float:
    return min(max_delay, base_delay * (2**attempt))


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    idempotent: bool | None = None,
    retries: int = DEFAULT_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    **kwargs: object,
) -> httpx.Response:
    """Synchronous bounded-retry request. See module docstring for semantics."""
    last_exc: Exception | None = None
    send = getattr(client, method.lower())  # client.get/post/put/... — same as .request
    for attempt in range(retries + 1):
        try:
            resp = send(url, **kwargs)  # type: ignore[arg-type]
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < retries and _should_retry(method, idempotent, exc=exc):
                logger.warning(
                    "http retry after transport error",
                    method=method,
                    url=str(url),
                    attempt=attempt + 1,
                    retries=retries,
                    err=str(exc),
                )
                time.sleep(_backoff_seconds(attempt, base_delay, max_delay))
                continue
            raise
        if (
            resp.status_code >= 500
            and attempt < retries
            and _should_retry(method, idempotent, status=resp.status_code)
        ):
            logger.warning(
                "http retry after 5xx",
                method=method,
                url=str(url),
                attempt=attempt + 1,
                retries=retries,
                status=resp.status_code,
            )
            time.sleep(_backoff_seconds(attempt, base_delay, max_delay))
            continue
        return resp
    assert last_exc is not None  # loop only exits the retry branch via return/raise
    raise last_exc


async def arequest_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    idempotent: bool | None = None,
    retries: int = DEFAULT_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    **kwargs: object,
) -> httpx.Response:
    """Asynchronous bounded-retry request. See module docstring for semantics."""
    last_exc: Exception | None = None
    send = getattr(client, method.lower())  # client.get/post/put/... — same as .request
    for attempt in range(retries + 1):
        try:
            resp = await send(url, **kwargs)  # type: ignore[arg-type]
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < retries and _should_retry(method, idempotent, exc=exc):
                logger.warning(
                    "http retry after transport error",
                    method=method,
                    url=str(url),
                    attempt=attempt + 1,
                    retries=retries,
                    err=str(exc),
                )
                await asyncio.sleep(_backoff_seconds(attempt, base_delay, max_delay))
                continue
            raise
        if (
            resp.status_code >= 500
            and attempt < retries
            and _should_retry(method, idempotent, status=resp.status_code)
        ):
            logger.warning(
                "http retry after 5xx",
                method=method,
                url=str(url),
                attempt=attempt + 1,
                retries=retries,
                status=resp.status_code,
            )
            await asyncio.sleep(_backoff_seconds(attempt, base_delay, max_delay))
            continue
        return resp
    assert last_exc is not None
    raise last_exc
