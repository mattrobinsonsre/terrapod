"""A published module version cannot be replaced (GHSA-mhhr-896g-4p33).

The direct-upload path upserted, so re-posting 1.2.3 replaced the bytes of a
published version in place. Module consumers do not hash-lock, so every workspace
pinned to `version = "1.2.3"` silently picked up different source on its next
init. "Registry module versions are immutable" is a property the registry has to
enforce, not a convention to rely on.

Driven through the route — the endpoint's own docstring said "Idempotent", so the
thing to pin is what a second PUT answers.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.storage import get_storage

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_TARBALL = b"\x1f\x8b" + b"x" * 64  # gzip magic + filler; never parsed here
_URL = "/api/terrapod/v1/registry-modules/private/default/vpc/aws/versions/1.2.3/upload"


def _user():
    return AuthenticatedUser(
        email="publisher@example.com",
        display_name="Publisher",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _module():
    mod = MagicMock()
    mod.id = uuid.uuid4()
    mod.name = "vpc"
    mod.provider = "aws"
    mod.namespace = "default"
    mod.labels = {}
    mod.owner_email = "publisher@example.com"
    return mod


def _version(status):
    v = MagicMock()
    v.id = uuid.uuid4()
    v.version = "1.2.3"
    v.upload_status = status
    v.interface_error = None
    v.created_at = None
    return v


def _app(storage=None):
    """The route takes storage via `Depends(get_storage)`, so the override is
    what reaches the service — patching the module attribute would not."""
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


def _existing(version_or_none):
    """A `db.execute` result whose `.scalars().first()` is this version."""
    result = MagicMock()
    result.scalars.return_value.first.return_value = version_or_none
    return result


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestRepublishingAVersion:
    @staticmethod
    def _patches(existing_version):
        """The service's own seams: the module lookup and the existing-version
        query. Patched at the service boundary rather than at
        `upload_module_tarball`, so the guard inside it actually runs."""
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_existing(existing_version))
        return db

    async def test_an_already_uploaded_version_is_refused_with_409(self, *_mocks):
        storage = AsyncMock()
        app = _app(storage)
        db = self._patches(_version("uploaded"))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch(
                "terrapod.api.routers.registry_modules.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.services.registry_module_service.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.api.routers.registry_modules.resolve_registry_capabilities_for",
                new=AsyncMock(return_value=frozenset({"registry:read", "registry:write"})),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.put(_URL, content=_TARBALL, headers=_AUTH)

        assert resp.status_code == 409
        assert "immutable" in resp.json()["detail"]
        # The decisive assertion: the bytes were never written over the published
        # ones. A 409 that still stored the tarball would be worse than no 409.
        storage.put_stream.assert_not_called()

    @pytest.mark.parametrize("status", ["pending", ""])
    async def test_a_version_that_never_finished_uploading_can_still_be_completed(
        self, _db_mock, _redis_mock, _storage_mock, status
    ):
        """A resumed publish, not an overwrite: a `create` that never uploaded, or
        a first attempt that failed, must stay completable or a failed publish
        would strand the version number for ever."""
        storage = AsyncMock()
        app = _app(storage)
        db = self._patches(_version(status))
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch(
                "terrapod.api.routers.registry_modules.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.services.registry_module_service.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.api.routers.registry_modules.resolve_registry_capabilities_for",
                new=AsyncMock(return_value=frozenset({"registry:read", "registry:write"})),
            ),
            patch(
                "terrapod.services.registry_module_service.upsert_module_version",
                new=AsyncMock(return_value=_version(status)),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.put(_URL, content=_TARBALL, headers=_AUTH)

        assert resp.status_code != 409, f"a {status!r} version was treated as published"

    async def test_a_brand_new_version_is_accepted(self, *_mocks):
        """And the first publish still works, so the refusal cannot pass by the
        endpoint simply being broken."""
        storage = AsyncMock()
        app = _app(storage)
        db = self._patches(None)
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch(
                "terrapod.api.routers.registry_modules.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.services.registry_module_service.get_module",
                new=AsyncMock(return_value=_module()),
            ),
            patch(
                "terrapod.api.routers.registry_modules.resolve_registry_capabilities_for",
                new=AsyncMock(return_value=frozenset({"registry:read", "registry:write"})),
            ),
            patch(
                "terrapod.services.registry_module_service.upsert_module_version",
                new=AsyncMock(return_value=_version("pending")),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.put(_URL, content=_TARBALL, headers=_AUTH)

        assert resp.status_code == 200
        storage.put_stream.assert_awaited_once()
