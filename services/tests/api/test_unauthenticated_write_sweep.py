"""Every write operation refuses an anonymous caller, unless it is on the list.

A3C7 suggested this in GHSA-63m3-56rj-qqfh, and it is the right shape: the four
unauthenticated writes in that report were not a subtle logic error but a
missing dependency on a handler, which no amount of reading catches reliably and
which this catches every time.

**The allow-list is the security decision; the sweep is only the mechanism.**
Every entry below is a route that must be reachable without a credential, with
the reason it must, and what stands in for authentication there. Adding an entry
is how the exemption gets reviewed — it should be as hard to add one thoughtlessly
as it currently is to notice a missing `Depends`.

What counts as a refusal: **401** (no credential) or **403** (a credential-less
principal resolved but denied). A **404** does not, and that distinction is the
whole point — the four findings all returned 404, because the handler had begun
running and only failed on a placeholder id. A 404 here means the route did work
before deciding it had no business doing any.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application
from terrapod.db.session import get_db

#: Routes that must answer an anonymous caller, and what protects them instead.
#:
#: Keyed by the route template so a path parameter's name change is visible here
#: rather than silently widening the exemption.
PUBLIC_WRITE_ROUTES: dict[str, str] = {
    # Signed capability in the path, minted into a URL the client is handed and
    # follows verbatim. go-tfe's foreign-PUT path sets no Authorization header,
    # so a bearer check would break the CLI. GHSA-63m3 / GHSA-r9v9.
    "/api/v2/configuration-versions/{cv_id}/upload": "signed capability in the path",
    "/api/tfe/v2/configuration-versions/{cv_id}/upload": "signed capability in the path",
    "/api/v2/state-versions/{state_version_id}/content": "signed capability in the path",
    "/api/tfe/v2/state-versions/{state_version_id}/content": "signed capability in the path",
    "/api/v2/state-versions/{state_version_id}/json-content": "signed capability in the path",
    "/api/tfe/v2/state-versions/{state_version_id}/json-content": "signed capability in the path",
    # HMAC over the request body (GitHub) or a timing-safe token (GitLab),
    # against the connection's own secret. The sender is a VCS provider, which
    # cannot hold a Terrapod credential.
    "/api/v1/vcs-events/github": "HMAC signature over the request body",
    "/api/terrapod/v1/vcs-events/github": "HMAC signature over the request body",
    "/api/v1/vcs-events/gitlab": "shared token, compared timing-safe",
    "/api/terrapod/v1/vcs-events/gitlab": "shared token, compared timing-safe",
    # How a caller OBTAINS a credential. Requiring one would be circular.
    "/oauth/token": "PKCE-bound authorization code",
    "/api/v1/auth/local/login": "the password is the credential",
    "/api/terrapod/v1/auth/local/login": "the password is the credential",
    "/api/v1/auth/local/authorize": "the password is the credential (PKCE start)",
    "/api/terrapod/v1/auth/local/authorize": "the password is the credential (PKCE start)",
    "/api/v1/auth/token": "one-time authorization code, consumed once",
    "/api/terrapod/v1/auth/token": "one-time authorization code, consumed once",
    "/api/v1/auth/saml/acs": "signed SAML assertion from the IdP",
    "/api/terrapod/v1/auth/saml/acs": "signed SAML assertion from the IdP",
    # The join token IS the credential: a listener has none until it joins, and
    # exchanges the token for a certificate here.
    "/api/v1/agent-pools/join": "join token, hashed at rest, exchanged for a cert",
    "/api/terrapod/v1/agent-pools/join": "join token, hashed at rest, exchanged for a cert",
    "/api/v1/agent-pools/{pool_id}/listeners/join": "join token, exchanged for a cert",
    "/api/terrapod/v1/agent-pools/{pool_id}/listeners/join": "join token, exchanged for a cert",
}


def _refuses(status: int) -> bool:
    """401 or 403 is a refusal. 404 is not — see the module docstring."""
    return status in (401, 403)


@pytest.fixture
def app():
    """The real app, with only the datastores stubbed.

    Deliberately NOT the lifespan: every dependency an anonymous request could
    reach is overridden, so a handler that does run reaches a mock rather than a
    connection attempt. A route that gets far enough to need the database is
    already the finding.
    """
    with (
        patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
        patch("terrapod.api.app.init_redis"),
        patch("terrapod.api.app.init_db"),
    ):
        application = create_application()
    application.dependency_overrides[get_db] = lambda: AsyncMock()
    return application


@pytest.fixture
def _storage_reaches_its_signature_check():
    """Let the filesystem presigned routes reach their HMAC check.

    Uninitialised storage makes them answer 503, which is an infrastructure
    error standing where an authorization answer should be — and a route that
    503s is a route this sweep cannot see. Giving them a store whose signature
    never verifies exercises the real refusal instead.
    """
    store = MagicMock()
    store.verify_signature.return_value = False
    with patch("terrapod.storage.filesystem_routes._get_store", return_value=store):
        yield


def _write_operations(app) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                out.append((method, path))
    return sorted(out)


def _concrete(path: str) -> str:
    """A placeholder for every path parameter.

    The id does not need to exist. A handler that checks authorization first
    never looks at it; one that does not will reach a lookup and answer 404,
    which is exactly the signal being tested for.
    """
    return re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)


class TestNoWriteOperationServesAnAnonymousCaller:
    async def test_every_write_refuses_or_is_listed(
        self, app, _storage_reaches_its_signature_check
    ):
        unexpected = []
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            for method, path in _write_operations(app):
                if path in PUBLIC_WRITE_ROUTES:
                    continue
                resp = await c.request(method, _concrete(path), json={})
                if not _refuses(resp.status_code):
                    unexpected.append(f"{method:6} {path}  -> {resp.status_code}")
        assert not unexpected, (
            "These write operations answered an anonymous caller.\n"
            "A 404 means the handler ran before deciding it had no business "
            "running — which is how the four unauthenticated writes in "
            "GHSA-63m3-56rj-qqfh presented.\n"
            "Add the missing auth dependency, or add the route to "
            "PUBLIC_WRITE_ROUTES with the reason it must be public:\n  " + "\n  ".join(unexpected)
        )

    def test_the_sweep_actually_swept(self, app):
        # A guard that found nothing because it looked at nothing would pass
        # silently, which is the failure mode this whole file exists to prevent.
        ops = _write_operations(app)
        assert len(ops) > 300, f"only {len(ops)} write operations found; did routing change?"

    def test_every_listed_route_still_exists(self, app):
        # An exemption for a route that has been renamed or removed is dead
        # text that quietly stops covering anything.
        live = {path for _, path in _write_operations(app)}
        stale = sorted(set(PUBLIC_WRITE_ROUTES) - live)
        assert not stale, (
            "PUBLIC_WRITE_ROUTES names routes that no longer exist as writes. "
            "Remove them, or the list is describing a surface that is gone:\n  "
            + "\n  ".join(stale)
        )

    def test_every_listed_route_says_why(self, app):
        for path, reason in PUBLIC_WRITE_ROUTES.items():
            assert reason and len(reason) > 10, f"{path} needs a real reason, got {reason!r}"
