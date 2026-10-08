"""Publishing an Ansible collection requires write on that collection.

Galaxy publish had **authentication only**. Any principal who could reach the
endpoint could publish into any namespace, because an existing collection was
looked up and then reused with nothing compared against it — `owner_email` was
used solely to stamp a row that did not yet exist. The handler's own docstring
reasoned about this ("trusting a client-supplied coordinate would let one
publisher write into another's namespace") and answered it by reading the
coordinate out of the archive instead of off the request. That stops a caller
*claiming* a namespace in the URL; it does not stop them building an archive
whose manifest declares someone else's.

Driven through the real routes, with the real capability resolver. Only the layer
*below* the new check is mocked — the manifest read, the collection lookup and
the write itself — because mocking the check would leave these tests asserting
on a function that does not run in production, which is the failure mode the
guard exists to catch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers.package_cache import authenticate_package_request
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.storage import get_storage

PUBLISH = "/api/v1/package-cache/galaxy/v3/artifacts/collections/"
SIGNATURE = "/api/v1/package-cache/galaxy/v3/collections/victim/widgets/versions/1.0.0/signature"
COORD = ("victim", "widgets", "1.0.0")


@pytest.fixture(autouse=True)
def _galaxy_on():
    """Pin the capability on so a default change cannot mute these (#1986)."""
    before = settings.registry.package_cache.galaxy.enabled
    settings.registry.package_cache.galaxy.enabled = True
    yield
    settings.registry.package_cache.galaxy.enabled = before


def _user(email: str = "stranger@example.com", roles=None, auth_method: str = "session"):
    return AuthenticatedUser(
        email=email,
        display_name=None,
        roles=list(roles or ["everyone"]),
        provider_name="local",
        auth_method=auth_method,
    )


def _collection(owner: str = "owner@example.com", labels=None):
    """A stand-in for the row the lookup returns, carrying only what RBAC reads."""
    row = MagicMock()
    row.namespace, row.name = COORD[0], COORD[1]
    row.owner_email = owner
    row.labels = labels or {}
    return row


def _app(user: AuthenticatedUser):
    app = create_app()
    app.dependency_overrides[authenticate_package_request] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestPublishingIntoSomeoneElsesNamespaceIsRefused:
    """The finding itself: an existing collection must be writable by the caller."""

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_a_stranger_is_refused(self, coords, get_coll, publish) -> None:
        coords.return_value = COORD
        get_coll.return_value = _collection(owner="owner@example.com")

        async with await _client(_app(_user())) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 403, r.text
        assert "registry:write" in r.text

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_nothing_is_written_when_it_is_refused(self, coords, get_coll, publish) -> None:
        """The property that makes the ordering load-bearing, not just the status.

        A 403 returned *after* the row and object exist would be a worse bug than
        the one being fixed: the takeover would succeed and report failure.
        """
        coords.return_value = COORD
        get_coll.return_value = _collection(owner="owner@example.com")

        async with await _client(_app(_user())) as c:
            await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        publish.assert_not_awaited()

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_the_owner_may_publish(self, coords, get_coll, publish) -> None:
        coords.return_value = COORD
        get_coll.return_value = _collection(owner="owner@example.com")
        publish.return_value = MagicMock(id="cv-1")

        async with await _client(_app(_user(email="owner@example.com"))) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 202, r.text
        publish.assert_awaited_once()

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_a_platform_admin_may_publish(self, coords, get_coll, publish) -> None:
        coords.return_value = COORD
        get_coll.return_value = _collection(owner="owner@example.com")
        publish.return_value = MagicMock(id="cv-1")

        async with await _client(_app(_user(roles=["admin"]))) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 202, r.text


class TestAnUnclaimedNamespaceStaysOpen:
    """Creating a new collection matches the module and provider registries:
    any authenticated principal may, and the creator becomes its owner. Narrowing
    that would be a separate product decision, not part of this fix."""

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_a_stranger_may_create_one(self, coords, get_coll, publish) -> None:
        coords.return_value = COORD
        get_coll.return_value = None
        publish.return_value = MagicMock(id="cv-1")

        async with await _client(_app(_user())) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 202, r.text


class TestARunnerTokenCannotPublish:
    """A run's own short-lived token has no business publishing a collection, and
    this must not rest on the capability arithmetic: the registry axis gives a
    runner token a read *floor* that does not cap what follows, and an owner match
    grants the whole axis — while a runner token's email is the literal "runner"."""

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_refused_on_an_existing_collection(self, coords, get_coll, publish) -> None:
        coords.return_value = COORD
        get_coll.return_value = _collection(owner="owner@example.com")

        user = _user(email="runner", roles=["everyone"], auth_method="runner_token")
        async with await _client(_app(user)) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 403, r.text
        assert "Runner tokens" in r.text
        publish.assert_not_awaited()

    @patch("terrapod.services.registry_collection_service.publish", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.read_coordinates", new_callable=AsyncMock)
    async def test_refused_on_a_new_namespace_too(self, coords, get_coll, publish) -> None:
        """The unclaimed-namespace path is open to users, not to runner tokens —
        and this is the case where owner-match arithmetic would have granted it,
        since the row would be stamped with the caller's own email."""
        coords.return_value = COORD
        get_coll.return_value = None

        user = _user(email="runner", roles=["everyone"], auth_method="runner_token")
        async with await _client(_app(user)) as c:
            r = await c.post(PUBLISH, files={"file": ("c.tar.gz", b"x" * 64)})

        assert r.status_code == 403, r.text
        publish.assert_not_awaited()


class TestAttachingASignatureIsGatedToo:
    """Signing is a second, deliberate step on an already-published artifact, so
    it needs the same write check — it had none at all."""

    @patch("terrapod.services.registry_collection_service.attach_signature", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_a_stranger_is_refused(self, get_coll, attach) -> None:
        get_coll.return_value = _collection(owner="owner@example.com")

        async with await _client(_app(_user())) as c:
            r = await c.put(SIGNATURE, content=b"-----BEGIN PGP SIGNATURE-----")

        assert r.status_code == 403, r.text
        attach.assert_not_awaited()

    @patch("terrapod.services.registry_collection_service.attach_signature", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_an_absent_collection_is_a_404_not_a_403(self, get_coll, attach) -> None:
        """`must_exist=True` here: there is no "create by signing" path, so an
        absent collection is a missing resource rather than a permission answer."""
        get_coll.return_value = None

        async with await _client(_app(_user())) as c:
            r = await c.put(SIGNATURE, content=b"-----BEGIN PGP SIGNATURE-----")

        assert r.status_code == 404, r.text
        attach.assert_not_awaited()
