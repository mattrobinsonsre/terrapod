"""`GET /api/v1/storage/locations` (#2547).

The route exists so a client can populate a picker for the workspace attribute
the server has accepted since #2547's server half. Its whole job is to answer
"what may I set this to", so the tests that matter are about which names come
back and which fields do NOT.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import StorageBackend, StorageLocationConfig
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(roles=None):
    return AuthenticatedUser(
        email="someone@example.com",
        display_name="Someone",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _locations(**kw) -> dict[str, StorageLocationConfig]:
    """Real config objects, not mocks: the route reads `.backend.value`, and a
    mock would serialise as a string that happens to look right while proving
    nothing about the field actually being a `StorageBackend`."""
    out = {}
    for name, backend in kw.items():
        out[name.replace("_", "-")] = StorageLocationConfig(
            backend=StorageBackend(backend),
            **{backend: {"bucket": f"{name}-bucket", "region": "eu-west-1"}}
            if backend == "s3"
            else {},
        )
    return out


def _app(user=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user or _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


class TestTheListing:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_it_returns_the_configured_locations(self, *mocks):
        locs = _locations(eu_restricted="s3", archive="s3")
        with patch("terrapod.config.settings.storage.locations", locs):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get("/api/v1/storage/locations", headers=_AUTH)

        assert resp.status_code == 200
        body = resp.json()
        # Sorted, so a picker's order does not depend on dict insertion order
        # (which follows the operator's YAML rather than anything meaningful).
        assert [d["id"] for d in body["data"]] == ["archive", "eu-restricted"]
        assert {d["type"] for d in body["data"]} == {"storage-locations"}
        assert body["meta"]["pagination"]["total-count"] == 2

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_it_leaks_no_addressing_or_credential_field(self, *mocks):
        """THE test. A location config also carries the bucket, the region, the
        endpoint and a `credentials` block with `role_arn` and `external_id` --
        all sitting beside each other in the model, which is what makes "while
        we are here, add the bucket" cheap to write.

        Asserted as an exact key set rather than an absence of named fields: a
        deny-list cannot stay complete against a config model someone else
        extends, and the failure direction is a credential on a route open to
        any authenticated user.
        """
        locs = _locations(eu_restricted="s3")
        with patch("terrapod.config.settings.storage.locations", locs):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get("/api/v1/storage/locations", headers=_AUTH)

        attrs = resp.json()["data"][0]["attributes"]
        assert set(attrs) == {"name", "backend"}, f"unexpected fields: {sorted(attrs)}"
        assert attrs == {"name": "eu-restricted", "backend": "s3"}
        # And nothing anywhere in the body, in case a field is added nested.
        assert "bucket" not in resp.text
        assert "role_arn" not in resp.text and "role-arn" not in resp.text

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_default_is_not_a_location(self, *mocks):
        """`default` is the ABSENCE of a location, and `validate_storage_location`
        refuses the literal string -- so listing it would offer a value the write
        path rejects. A caller supplies its own null option instead."""
        locs = _locations(eu_restricted="s3")
        with patch("terrapod.config.settings.storage.locations", locs):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get("/api/v1/storage/locations", headers=_AUTH)
        assert "default" not in [d["id"] for d in resp.json()["data"]]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_none_configured_is_an_empty_list_not_an_error(self, *mocks):
        """The single-store deployment, which is every deployment today. A
        caller reads this as "offer no control", so it must be an ordinary empty
        collection rather than a 404 that reads as a missing feature."""
        with patch("terrapod.config.settings.storage.locations", {}):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get("/api/v1/storage/locations", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        assert resp.json()["meta"]["pagination"]["total-count"] == 0


class TestTheGate:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unauthenticated_is_refused(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v1/storage/locations")
        assert resp.status_code == 401

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_plain_user_may_read_it(self, *mocks):
        """Deliberate, and matching the execution-environment listing: anyone who
        may set the attribute needs the valid values, and gating it to admin would
        leave a non-admin workspace editor with an unpopulatable control."""
        with patch("terrapod.config.settings.storage.locations", _locations(a="s3")):
            async with AsyncClient(
                transport=ASGITransport(app=_app(_user(["everyone"]))), base_url=_BASE
            ) as c:
                resp = await c.get("/api/v1/storage/locations", headers=_AUTH)
        assert resp.status_code == 200


class TestBothPrefixesServeIt:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_the_deprecated_alias_resolves_too(self, *mocks):
        """`include_terrapod` mounts both, and a router mounted at one prefix
        silently drops the alias a lagging consumer still calls."""
        with patch("terrapod.config.settings.storage.locations", _locations(a="s3")):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                canonical = await c.get("/api/v1/storage/locations", headers=_AUTH)
                alias = await c.get("/api/terrapod/v1/storage/locations", headers=_AUTH)
        assert canonical.status_code == 200
        assert alias.status_code == 200
        assert canonical.json() == alias.json()
