"""A gated-off engine can perform no operations (#1429).

The requirement is stronger than hiding: with an engine switched off, the
surfaces that exist only for it must refuse to *do* anything — not merely stop
being linked to from the UI, which leaves every endpoint reachable by anyone who
knows the URL.

Driven through the real routers rather than by asking the resolver, because the
bug that motivated this was precisely a resolver-shaped answer nobody consulted:
`registry.oci.enabled` read correctly, and `/v2/` served push, pull and mirror
regardless.
"""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers.package_cache import authenticate_package_request
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_REPO = "terrapod/ansible-ee"
_DIGEST = "sha256:" + "a" * 64
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"anything:tok.tpod.secret").decode()}


@pytest.fixture(autouse=True)
def _restore():
    before = (settings.engines.ansible.enabled, settings.engines.pulumi.enabled)
    yield
    settings.engines.ansible.enabled, settings.engines.pulumi.enabled = before


@pytest.fixture
def _sealed():
    """Stop an uncached lookup reaching for the network.

    A sealed node answers a miss from configuration rather than upstream, which is
    what makes the assertion below deterministic without mocking the fetch.
    """
    before = settings.registry.cache_only
    settings.registry.cache_only = True
    yield
    settings.registry.cache_only = before


def _db():
    """A session whose result accessors are synchronous, as SQLAlchemy's are.

    A bare `AsyncMock` hands back a coroutine from `.scalars()`, which fails deep
    inside a handler rather than at the call site and reads like a product bug.
    """
    from unittest.mock import AsyncMock, MagicMock

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    return db


def _app():
    from unittest.mock import AsyncMock

    app = create_app()
    app.dependency_overrides[authenticate_oci] = lambda: AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )
    app.dependency_overrides[get_db] = lambda: _db()
    # Package-cache auth opens its own session, so it needs a real database.
    # Overridden rather than patched: FastAPI captured the dependency when the
    # route was registered, so patching the module attribute has no effect.
    app.dependency_overrides[authenticate_package_request] = lambda: AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app


class TestAnsibleOffSilencesTheRegistry:
    """Every verb, not just the ones a browser would reach.

    A write refused while a read still works is not "disabled" — it is a registry
    with an unusual permissions model, and it still accepts the uploads that
    consume the storage an operator was trying to stop using.
    """

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/v2/"),
            ("GET", f"/v2/{_REPO}/manifests/latest"),
            ("HEAD", f"/v2/{_REPO}/manifests/latest"),
            ("GET", f"/v2/{_REPO}/blobs/{_DIGEST}"),
            ("HEAD", f"/v2/{_REPO}/blobs/{_DIGEST}"),
            ("POST", f"/v2/{_REPO}/blobs/uploads/"),
            ("PUT", f"/v2/{_REPO}/manifests/latest"),
            ("GET", f"/v2/{_REPO}/tags/list"),
            ("GET", "/v2/_catalog"),
        ],
    )
    async def test_it_is_refused(self, method: str, path: str) -> None:
        settings.engines.ansible.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.request(method, path, headers=_BASIC)

        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"

    def test_the_routes_do_not_exist_at_all(self) -> None:
        """Hard off: unmounted, not mounted-and-refusing.

        A surface that 404s every request is still a surface — it sits in the
        schema, carries its dependencies, and reads to anyone auditing the
        application as something this deployment does. Switched off, it should not
        be there.
        """
        settings.engines.ansible.enabled = False

        app = _app()
        assert not [r for r in app.routes if r.path.startswith("/v2")]

    def test_it_is_absent_from_the_api_schema(self) -> None:
        """Nothing advertises a capability the deployment has turned off."""
        settings.engines.ansible.enabled = False

        paths = _app().openapi()["paths"]
        assert not [p for p in paths if p.startswith("/v2")]

    async def test_pulumi_being_on_does_not_keep_it_alive(self) -> None:
        """The registry serves execution environments; Pulumi has no claim on it."""
        settings.engines.ansible.enabled = False
        settings.engines.pulumi.enabled = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get("/v2/", headers=_BASIC)

        assert response.status_code == 404


class TestThePackageProxies:
    async def test_pulumi_off_silences_npm(self) -> None:
        settings.engines.pulumi.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(
                "/api/terrapod/v1/package-cache/npm/left-pad", headers=_BASIC
            )

        assert response.status_code == 404

    async def test_pypi_survives_on_ansible_alone(self, _sealed) -> None:
        """Shared: Ansible collections' Python dependencies need it too.

        Asserted on the gate's own refusal rather than on a status, because what
        the handler goes on to answer for an uncached project is not this test's
        business — only that the request got past the gate at all. The node is
        sealed so nothing reaches for the network to find out.
        """
        settings.engines.ansible.enabled = True
        settings.engines.pulumi.enabled = False

        app = _app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            response = await client.get(
                "/api/terrapod/v1/package-cache/pypi/simple/requests/", headers=_BASIC
            )

        detail = (
            response.json().get("detail")
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        assert detail != "pypi proxy is not enabled"

    async def test_both_engines_off_silences_pypi(self) -> None:
        settings.engines.ansible.enabled = False
        settings.engines.pulumi.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(
                "/api/terrapod/v1/package-cache/pypi/simple/requests/", headers=_BASIC
            )

        assert response.status_code == 404


class TestTerraformIsUntouched:
    """The point of the exercise: an HCL-only install loses nothing.

    Turning both engines off must not disturb the surfaces terraform actually
    uses. A gate that reaches too far is worse than no gate.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/terraform.json",
            "/api/v2/ping",
        ],
    )
    async def test_the_terraform_surface_still_answers(self, path: str) -> None:
        settings.engines.ansible.enabled = False
        settings.engines.pulumi.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(path, headers=_BASIC)

        assert response.status_code != 404


class TestNothingIsMountedTwice:
    """The factory exists so repeated application builds cannot accumulate routes.

    A module-level router mutated at startup would grow a duplicate set every time
    the application is constructed — invisible in production, which builds once,
    and a slow leak across a test session that builds hundreds of times.
    """

    def test_building_twice_yields_the_same_route_count(self) -> None:
        settings.engines.ansible.enabled = True
        settings.engines.pulumi.enabled = True

        first = len([r for r in _app().routes if "package-cache" in r.path])
        second = len([r for r in _app().routes if "package-cache" in r.path])

        assert first == second > 0
