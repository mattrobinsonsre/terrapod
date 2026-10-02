"""A pushed repository cannot impersonate a registry (GHSA-mhhr-896g-4p33).

`_authorised_repository` consults the database before the upstream list, by
design, so that a repository someone has pushed is never overwritten by upstream
content. That protection becomes the attack when the pushed name was chosen to
look like an upstream: pushing `docker.io/library/nginx` into a deployment with no
`docker.io` upstream creates a local repository that **permanently** shadows one,
and adding the upstream later does not dislodge it.

Driven through the route, because the refusal lives inside the create branch of
`_authorised_repository` and that branch is only reached by a push.
"""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.services.oci.names import looks_like_a_registry_host
from terrapod.storage import get_storage

_BASE = "http://test"
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"u:tok").decode()}
_WRITE = frozenset({"registry:read", "registry:write"})


def _user():
    return AuthenticatedUser(
        email="alice@example.com",
        display_name="Alice",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app():
    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: _user()
    db = AsyncMock()
    db.add = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app, db


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


class TestTheHostHeuristic:
    """Docker's own rule for "is the first component a registry": it contains a
    dot, or it is `localhost`."""

    @pytest.mark.parametrize(
        "component",
        ["docker.io", "quay.io", "ghcr.io", "registry.k8s.io", "localhost", "evil.example.com"],
    )
    def test_host_shaped_components(self, component):
        assert looks_like_a_registry_host(component) is True

    @pytest.mark.parametrize(
        "component", ["alice", "terrapod", "library", "my-team", "ansible_ee", "localhostx"]
    )
    def test_namespace_shaped_components(self, component):
        assert looks_like_a_registry_host(component) is False


@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.pullthrough_service.mirroring_allowed", return_value=False)
@patch("terrapod.services.oci.pullthrough_service.resolve_upstream", return_value=None)
@patch("terrapod.services.oci.upload_service.open_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
class TestPushingAHostShapedName:
    async def test_it_is_refused_and_no_repository_is_created(
        self, get_repo, open_session, _resolve, _mirroring, caps
    ):
        """DENIED, not a silent namespacing: the push names something it is not."""
        caps.return_value = _WRITE
        get_repo.return_value = None  # no such repository yet — the create path

        app, db = _app()
        async with await _client(app) as c:
            resp = await c.post("/v2/docker.io/library/nginx/blobs/uploads/", headers=_BASIC)

        assert resp.status_code == 403
        assert resp.json()["errors"][0]["code"] == "DENIED"
        assert "names a registry" in resp.json()["errors"][0]["message"]
        db.add.assert_not_called()
        open_session.assert_not_awaited()

    async def test_an_ordinary_namespace_is_still_created_by_a_push(
        self, get_repo, open_session, _resolve, _mirroring, caps
    ):
        """The other half: first-push-creates-the-repository is the behaviour
        `docker push` depends on, and must survive the refusal above."""
        caps.return_value = _WRITE
        get_repo.return_value = None
        session = MagicMock()
        session.id = uuid.uuid4()
        open_session.return_value = session

        app, db = _app()
        async with await _client(app) as c:
            resp = await c.post("/v2/alice/nginx/blobs/uploads/", headers=_BASIC)

        assert resp.status_code == 202
        db.add.assert_called_once()

    async def test_a_single_component_name_is_not_a_host(
        self, get_repo, open_session, _resolve, _mirroring, caps
    ):
        """`quay.io` alone names no repository on any registry, so treating it as
        a host would refuse a legal — if odd — single-component push for nothing.
        The rule needs a remainder, exactly as `resolve_upstream` does."""
        caps.return_value = _WRITE
        get_repo.return_value = None
        session = MagicMock()
        session.id = uuid.uuid4()
        open_session.return_value = session

        app, _db = _app()
        async with await _client(app) as c:
            resp = await c.post("/v2/quay.io/blobs/uploads/", headers=_BASIC)

        assert resp.status_code == 202


@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.upload_service.open_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
async def test_a_repository_that_already_exists_is_unaffected(get_repo, open_session, caps):
    """The refusal is on CREATE only. A mirror row — which the pull-through path
    creates under exactly such a name — must keep serving."""
    caps.return_value = _WRITE
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.name = "quay.io/ansible/awx-ee"
    existing.labels = {"access": "everyone"}
    existing.owner_email = None
    get_repo.return_value = existing
    session = MagicMock()
    session.id = uuid.uuid4()
    open_session.return_value = session

    app, _db = _app()
    async with await _client(app) as c:
        resp = await c.post("/v2/quay.io/ansible/awx-ee/blobs/uploads/", headers=_BASIC)

    assert resp.status_code == 202


@patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
@patch("terrapod.services.oci.pullthrough_service.mirroring_allowed", return_value=True)
@patch("terrapod.services.oci.pullthrough_service.ensure_mirror_repository", new_callable=AsyncMock)
@patch("terrapod.services.oci.pullthrough_service.resolve_upstream")
@patch("terrapod.services.oci.upload_service.open_session", new_callable=AsyncMock)
@patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
async def test_a_configured_upstream_still_becomes_a_mirror(
    get_repo, open_session, resolve_upstream, ensure_mirror, _mirroring, caps
):
    """The refusal sits in the `elif create` branch, AFTER the upstream branch, so
    a host the deployment actually mirrors is unaffected — that is the whole
    distinction the fix rests on."""
    caps.return_value = _WRITE
    get_repo.return_value = None
    resolve_upstream.return_value = ("quay.io", "ansible/awx-ee")
    mirror = MagicMock()
    mirror.id = uuid.uuid4()
    mirror.name = "quay.io/ansible/awx-ee"
    mirror.labels = {}
    mirror.owner_email = None
    ensure_mirror.return_value = mirror
    session = MagicMock()
    session.id = uuid.uuid4()
    open_session.return_value = session

    app, _db = _app()
    async with await _client(app) as c:
        resp = await c.post("/v2/quay.io/ansible/awx-ee/blobs/uploads/", headers=_BASIC)

    assert resp.status_code == 202
    ensure_mirror.assert_awaited_once()
