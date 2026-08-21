"""Every /v2/ route requires authentication (#1408).

A registry that serves anonymously turns a Terraform control plane into a free
pull-through cache for whoever finds it — outbound bandwidth, upstream rate
limits consumed against the operator's credentials, and private images readable
by anyone who can guess a repository name. Anonymous *push* is worse: unbounded
storage growth from strangers, and an execution environment that a workspace
will later run.

So this walks the app's real route table rather than testing a handful of
endpoints by hand. A route added later without the auth dependency fails here,
which is the point — the hazard is not that today's routes are wrong, it is that
tomorrow's might be.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app

_BASE = "http://test"

#: A concrete request per route shape. Built by hand rather than generated so
#: that a new route with a shape not covered here shows up as a KeyError in the
#: parametrisation instead of being silently skipped.
_SAMPLE_PATHS = {
    "/v2/": ("GET", "/v2/"),
    "/v2/{name:path}/tags/list": ("GET", "/v2/ns/repo/tags/list"),
    "/v2/{name:path}/manifests/{reference}": ("GET", "/v2/ns/repo/manifests/latest"),
    "/v2/{name:path}/blobs/{digest}": ("GET", "/v2/ns/repo/blobs/sha256:" + "a" * 64),
    "/v2/{name:path}/blobs/uploads/": ("POST", "/v2/ns/repo/blobs/uploads/"),
    "/v2/{name:path}/blobs/uploads/{session_id}": ("PATCH", "/v2/ns/repo/blobs/uploads/abc"),
    "/v2/{name:path}/referrers/{digest}": ("GET", "/v2/ns/repo/referrers/sha256:" + "a" * 64),
}


def _oci_routes() -> list:
    app = create_app()
    return sorted({r.path for r in app.routes if r.path.startswith("/v2")})


def test_every_oci_route_has_a_sample_request() -> None:
    """Fails when a route is added without being covered below.

    Deliberately separate from the auth assertions: without it, a new route
    would simply not be exercised, and the suite would keep reporting success
    for a surface it no longer covers.
    """
    uncovered = [p for p in _oci_routes() if p not in _SAMPLE_PATHS]
    assert not uncovered, f"new /v2/ route(s) not covered by the auth test: {uncovered}"


@pytest.mark.parametrize("route", _oci_routes())
async def test_route_rejects_anonymous_requests(route: str) -> None:
    method, path = _SAMPLE_PATHS[route]
    app = create_app()  # no dependency overrides — the real auth runs
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        response = await client.request(method, path)

    assert response.status_code == 401, (
        f"{method} {route} answered {response.status_code} anonymously — every /v2/ "
        "route must require a credential"
    )


@pytest.mark.parametrize("route", _oci_routes())
async def test_route_rejects_a_bad_credential(route: str) -> None:
    """Not the same test: a route could reject *missing* auth while accepting
    anything presented, which is a credential check that never checks.

    This one reaches the token lookup, so the session it opens is mocked — the
    point is that an unrecognised credential is refused, not how the lookup is
    performed.
    """
    import base64
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    method, path = _SAMPLE_PATHS[route]
    header = {"Authorization": "Basic " + base64.b64encode(b"user:not-a-real-token").decode()}
    app = create_app()

    with (
        patch("terrapod.db.session.get_db_session", _session),
        patch("terrapod.api.dependencies.validate_api_token", AsyncMock(return_value=None)),
        patch("terrapod.auth.sessions.get_session", AsyncMock(return_value=None)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            response = await client.request(method, path, headers=header)

    assert response.status_code == 401, f"{method} {route} accepted an invalid credential"


async def test_the_401_carries_a_basic_challenge() -> None:
    """Docker will not send credentials without a challenge it recognises, so a
    401 lacking this makes the registry unusable rather than merely protected."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
        response = await client.get("/v2/")

    assert response.headers.get("WWW-Authenticate", "").startswith("Basic ")
    assert response.json()["errors"][0]["code"] == "UNAUTHORIZED"
