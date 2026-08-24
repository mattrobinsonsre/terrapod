"""The registry's delete surface (#1423).

Services-api tier: the routers with the database mocked. The integration tests
cover what survives a delete; these cover who may issue one and what each verb
answers, which is where the spec and the RBAC live.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_REPO = "team/app"
_DIGEST = "sha256:" + "a" * 64
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"any:tok.tpod.secret").decode()}


def _app():
    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app


def _repo():
    repo = MagicMock()
    repo.name = _REPO
    repo.labels = {}
    repo.owner_email = "admin@example.com"
    repo.upstream = None
    return repo


class TestDeletingAManifest:
    @patch("terrapod.services.oci.registry_service.delete_tag", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_a_tag_delete_returns_202(self, get_repo, caps, delete_tag) -> None:
        """The spec's status for an accepted delete."""
        get_repo.return_value = _repo()
        caps.return_value = frozenset({cap.REGISTRY_READ, cap.REGISTRY_WRITE, cap.REGISTRY_ADMIN})
        delete_tag.return_value = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.delete(f"/v2/{_REPO}/manifests/v1", headers=_BASIC)

        assert r.status_code == 202
        delete_tag.assert_awaited_once()

    @patch("terrapod.services.oci.registry_service.delete_manifest", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_a_digest_reference_deletes_the_manifest(
        self, get_repo, caps, delete_man
    ) -> None:
        """A digest and a tag are different operations behind one endpoint."""
        get_repo.return_value = _repo()
        caps.return_value = frozenset({cap.REGISTRY_READ, cap.REGISTRY_ADMIN})
        delete_man.return_value = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.delete(f"/v2/{_REPO}/manifests/{_DIGEST}", headers=_BASIC)

        assert r.status_code == 202
        delete_man.assert_awaited_once()

    @patch("terrapod.services.oci.registry_service.delete_tag", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_deleting_what_is_not_there_is_404(self, get_repo, caps, delete_tag) -> None:
        """Not 202: an accepted delete of a thing that never existed is a lie."""
        get_repo.return_value = _repo()
        caps.return_value = frozenset({cap.REGISTRY_READ, cap.REGISTRY_ADMIN})
        delete_tag.return_value = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.delete(f"/v2/{_REPO}/manifests/nope", headers=_BASIC)

        assert r.status_code == 404


class TestBlobDeletionIsDeclined:
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_it_answers_405(self, get_repo, caps) -> None:
        """A supported position, not a gap.

        The spec allows a registry to decline, and the conformance suite treats
        405 as valid and skips its follow-up accordingly. Declining is the honest
        answer for content-addressed blobs shared across repositories: deleting
        one literally would break every manifest still pointing at those bytes.
        """
        get_repo.return_value = _repo()
        caps.return_value = frozenset({cap.REGISTRY_READ, cap.REGISTRY_ADMIN})

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.delete(f"/v2/{_REPO}/blobs/{_DIGEST}", headers=_BASIC)

        assert r.status_code == 405
        assert r.json()["errors"][0]["code"] == "UNSUPPORTED"


class TestItTakesAdmin:
    """read and write cannot delete. This destroys content that may exist nowhere else."""

    @pytest.mark.parametrize(
        "caps_held",
        [
            frozenset({cap.REGISTRY_READ}),
            frozenset({cap.REGISTRY_READ, cap.REGISTRY_WRITE}),
        ],
    )
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_without_admin_it_is_refused(self, get_repo, caps, caps_held) -> None:
        get_repo.return_value = _repo()
        caps.return_value = caps_held

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            r = await c.delete(f"/v2/{_REPO}/manifests/v1", headers=_BASIC)

        # 404, not 403 — the same answer as a repository the caller cannot see,
        # so a probe cannot map what exists.
        assert r.status_code == 404


class TestTheDigestReachesTheQueryAsAString:
    """A parsed reference carries a `Digest` object, not a string (#1423).

    Passing it straight into the query reaches asyncpg as a type it cannot bind,
    and the delete 500s instead of working. Worth its own test because the
    obvious router test hides it: mocking `delete_manifest` mocks precisely the
    boundary where the mismatch happens, so it passed while every real
    delete-by-digest failed — found by the conformance suite, not by the suite
    that was supposed to cover it.
    """

    @patch("terrapod.services.oci.registry_service.delete_manifest", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_it_is_a_str_not_a_digest_object(self, get_repo, caps, delete_man) -> None:
        get_repo.return_value = _repo()
        caps.return_value = frozenset({cap.REGISTRY_READ, cap.REGISTRY_ADMIN})
        delete_man.return_value = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            await c.delete(f"/v2/{_REPO}/manifests/{_DIGEST}", headers=_BASIC)

        passed = delete_man.await_args[0][2]
        assert isinstance(passed, str), f"the driver cannot bind a {type(passed).__name__}"
        assert passed == _DIGEST
