"""Prometheus metrics instrumentation for the Terrapod API server.

Provides HTTP request counter/histogram and a /metrics endpoint.
Only active when settings.metrics.enabled is True.
"""

import time

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "terrapod_http_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status"],
)

REQUEST_DURATION = Histogram(
    "terrapod_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path_template", "status"],
)


def _get_path_template(request: Request) -> str:
    """Extract the FastAPI route pattern to avoid high-cardinality raw paths."""
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        return route.path
    return request.url.path


async def metrics_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Record request count and duration for every HTTP request."""
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    path_template = _get_path_template(request)
    status = str(response.status_code)

    REQUEST_COUNT.labels(method=request.method, path_template=path_template, status=status).inc()
    REQUEST_DURATION.labels(
        method=request.method, path_template=path_template, status=status
    ).observe(duration)

    return response


async def metrics_endpoint(request: Request) -> Response:
    """Serve Prometheus metrics in exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
