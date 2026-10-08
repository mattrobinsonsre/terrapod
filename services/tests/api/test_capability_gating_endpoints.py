"""A switched-off capability can perform no operations (#1429, #1986).

The requirement is stronger than hiding: with a capability switched off, the
surface that exists for it must refuse to *do* anything — not merely stop being
linked to from the UI, which leaves every endpoint reachable by anyone who knows
the URL.

Driven through the real routers rather than by asking the resolver, because the
bug that motivated this was precisely a resolver-shaped answer nobody consulted:
`registry.oci.enabled` read correctly, and `/v2/` served push, pull and mirror
regardless.

**This used to be keyed on an engine on/off switch.** That switch is withdrawn
(#1986) — the platform should just work, so no operator decides which engines
they are allowed to use. What survived is the per-capability flag, which is the
half that was load-bearing, and this file now drives those. `test_capabilities.py`
proves each flag is *read*; this proves that reading it unmounts the surface.
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
from terrapod.services.capabilities import CAPABILITIES
from terrapod.services.oci.auth import authenticate_oci
from terrapod.storage import get_storage

_BASE = "http://test"
_REPO = "terrapod/ansible-ee"
_DIGEST = "sha256:" + "a" * 64
_BASIC = {"Authorization": "Basic " + base64.b64encode(b"anything:tok.tpod.secret").decode()}

#: The path fragment each capability's routes carry, so a test can say "this
#: capability's surface" without naming routes twice. Derived against
#: `CAPABILITIES` below, so a new capability with no entry fails rather than
#: being silently unexercised.
_FRAGMENTS = {
    "oci": "/v2",
    "pypi": "package-cache/pypi",
    "npm": "package-cache/npm",
    "galaxy": "package-cache/galaxy",
    "pulumi": "package-cache/pulumi",
    "go": "package-cache/go",
    "nuget": "package-cache/nuget",
}


def _flag(capability: str):
    """The object carrying `capability`'s own flag."""
    if capability == "oci":
        return settings.registry.oci
    return getattr(settings.registry.package_cache, capability)


@pytest.fixture(autouse=True)
def _restore():
    before = [(_flag(c), _flag(c).enabled) for c in CAPABILITIES]
    cache_before = settings.registry.package_cache.enabled
    yield
    for holder, value in before:
        holder.enabled = value
    settings.registry.package_cache.enabled = cache_before


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


def _paths(app, fragment: str) -> list[str]:
    """Routes carrying `fragment`.

    The registry's fragment is matched as a PREFIX, not a substring: `/v2` is
    also the tail of the deprecated TFE alias `/api/v2/...`, so a substring
    match would report Terraform's own surface as the registry's and the
    unmounted assertions could never pass.
    """
    paths = [getattr(r, "path", "") for r in app.routes]
    if fragment.startswith("/"):
        return [p for p in paths if p.startswith(fragment)]
    return [p for p in paths if fragment in p]


class TestTheFragmentTableCoversEveryCapability:
    def test_nothing_is_silently_unexercised(self) -> None:
        """Derived, so a new capability arrives exercised or fails here."""
        assert set(_FRAGMENTS) == set(CAPABILITIES), (
            "a capability has no route fragment, so nothing below drives it — "
            "add it to _FRAGMENTS with the path its routes carry"
        )


class TestOffMeansUnmounted:
    """Hard off: unmounted, not mounted-and-refusing.

    A surface that 404s every request is still a surface — it sits in the
    schema, carries its dependencies, and reads to anyone auditing the
    application as something this deployment does. Switched off, it should not
    be there.
    """

    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_its_routes_do_not_exist(self, capability: str) -> None:
        _flag(capability).enabled = False

        assert not _paths(_app(), _FRAGMENTS[capability])

    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_it_is_absent_from_the_api_schema(self, capability: str) -> None:
        """Nothing advertises a capability the deployment has turned off."""
        _flag(capability).enabled = False

        fragment = _FRAGMENTS[capability]
        advertised = list(_app().openapi()["paths"])
        if fragment.startswith("/"):
            assert not [p for p in advertised if p.startswith(fragment)]
        else:
            assert not [p for p in advertised if fragment in p]

    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_turning_it_off_leaves_every_other_capability_mounted(self, capability: str) -> None:
        """A gate that reaches too far is worse than no gate."""
        _flag(capability).enabled = False
        app = _app()

        for other in CAPABILITIES:
            if other == capability:
                continue
            assert _paths(app, _FRAGMENTS[other]), (
                f"turning {capability} off also unmounted {other}"
            )


class TestTheRegistryRefusesEveryVerb:
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
        settings.registry.oci.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.request(method, path, headers=_BASIC)

        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"


class TestGalaxyRefusesPublishingToo:
    """The direction that is easy to forget.

    Nothing but Ansible installs a collection, so with the proxy off none of its
    five read endpoints should answer — including the discovery document a client
    asks for first — and neither should either write.
    """

    async def test_every_read_and_write_is_refused(self) -> None:
        settings.registry.package_cache.galaxy.enabled = False
        base = "/api/terrapod/v1/package-cache/galaxy"

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            for path in (
                f"{base}/",
                f"{base}/v3/collections/community/general/",
                f"{base}/v3/collections/community/general/versions/",
                f"{base}/v3/collections/community/general/versions/1.0.0/",
                # Publishing too: a write refused while reads still work is not
                # "disabled", and it still consumes the storage an operator was
                # trying to stop using.
                f"{base}/v3/imports/collections/x/",
            ):
                response = await client.get(path, headers=_BASIC)
                assert response.status_code == 404, f"{path} answered {response.status_code}"

            for method, path in (
                ("POST", f"{base}/v3/artifacts/collections/"),
                ("PUT", f"{base}/v3/collections/acme/widgets/versions/1.0.0/signature"),
            ):
                response = await client.request(method, path, headers=_BASIC)
                assert response.status_code == 404, (
                    f"{method} {path} answered {response.status_code}"
                )


class TestARequestIsRefusedNotJustUnrouted:
    """Each proxy, driven as a real request rather than inspected as a route."""

    @pytest.mark.parametrize(
        ("capability", "path"),
        [
            ("pypi", "/api/terrapod/v1/package-cache/pypi/simple/requests/"),
            ("npm", "/api/terrapod/v1/package-cache/npm/left-pad"),
            (
                "pulumi",
                "/api/terrapod/v1/package-cache/pulumi/"
                "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz",
            ),
            ("go", "/api/terrapod/v1/package-cache/go/example.com/m/@v/list"),
            ("nuget", "/api/terrapod/v1/package-cache/nuget/index.json"),
        ],
    )
    async def test_it_answers_404(self, capability: str, path: str) -> None:
        _flag(capability).enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(path, headers=_BASIC)

        assert response.status_code == 404


class TestOnMeansItServes:
    """The other direction, which is what makes the tests above meaningful.

    Asserted on getting past the gate rather than on a status: a sealed node
    answers an uncached project its own way, which is not this test's business.
    The node is sealed so nothing reaches for the network to find out.
    """

    async def test_galaxys_discovery_document_answers(self, _sealed) -> None:
        settings.registry.package_cache.galaxy.enabled = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get("/api/terrapod/v1/package-cache/galaxy/", headers=_BASIC)

        assert response.status_code == 200
        assert response.json()["available_versions"] == {"v3": "v3/"}

    async def test_pypi_gets_past_the_gate(self, _sealed) -> None:
        settings.registry.package_cache.pypi.enabled = True

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(
                "/api/terrapod/v1/package-cache/pypi/simple/requests/", headers=_BASIC
            )

        detail = (
            response.json().get("detail")
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        assert detail != "pypi proxy is not enabled"


class TestThePackageCacheMasterSwitch:
    """One switch above the six, so an operator can stop the lot in one move."""

    async def test_it_silences_every_ecosystem(self) -> None:
        settings.registry.package_cache.enabled = False
        app = _app()

        for capability in CAPABILITIES:
            if capability == "oci":
                continue
            assert not _paths(app, _FRAGMENTS[capability]), capability

    def test_it_does_not_touch_the_container_registry(self) -> None:
        """The registry is not part of the package cache."""
        settings.registry.package_cache.enabled = False

        assert _paths(_app(), "/v2")


class TestThePulumiServiceSurfaceIsNotGateable:
    """It has no flag of its own, and there is no engine switch above it (#1986).

    Kept as a test rather than left implicit because it was gated until #1986,
    so "is it still mounted" is exactly the question a reader of that change
    asks.
    """

    def test_it_is_mounted_with_every_capability_off(self) -> None:
        for capability in CAPABILITIES:
            _flag(capability).enabled = False
        settings.registry.package_cache.enabled = False
        paths = [r.path for r in _app().routes]

        assert [p for p in paths if "/pulumi/api/" in p]
        assert [p for p in paths if "pulumi-deployment" in p]


class TestTerraformIsUntouched:
    """The point of the exercise: an HCL-only install loses nothing.

    Turning every capability off must not disturb the surfaces terraform
    actually uses. A gate that reaches too far is worse than no gate.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/terraform.json",
            "/api/v2/ping",
        ],
    )
    async def test_the_terraform_surface_still_answers(self, path: str) -> None:
        for capability in CAPABILITIES:
            _flag(capability).enabled = False
        settings.registry.package_cache.enabled = False

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as client:
            response = await client.get(path, headers=_BASIC)

        assert response.status_code != 404

    def test_the_terraform_state_routes_are_untouched(self) -> None:
        for capability in CAPABILITIES:
            _flag(capability).enabled = False
        paths = [r.path for r in _app().routes]

        assert [p for p in paths if p.endswith("/runs/{run_id}/artifacts/state")]


class TestNothingIsMountedTwice:
    """The factory exists so repeated application builds cannot accumulate routes.

    A module-level router mutated at startup would grow a duplicate set every time
    the application is constructed — invisible in production, which builds once,
    and a slow leak across a test session that builds hundreds of times.
    """

    def test_building_twice_yields_the_same_route_count(self) -> None:
        first = len(_paths(_app(), "package-cache"))
        second = len(_paths(_app(), "package-cache"))

        assert first == second > 0
