"""Every cache surface is mounted, unconditionally.

**This replaces the capability-gating tests** — `tests/services/test_capabilities.py`
and `tests/api/test_capability_gating_endpoints.py`, both deleted, along with
`services/capabilities.py` and every `enabled` flag under `settings.registry`
except the two named below. Those files asserted the opposite of this one: that
a switched-off cache was absent rather than mounted-and-refusing.

The gating went because a cache is **infrastructure**, not a feature, and a
cache that is off is a surface half-present. Every defect the flags produced was
that shape:

* the `/admin/package-cache/` namespace with four routes answering and a fifth
  gone, because the listing and the warm route were gated differently;
* an upload route that would store an artifact into a cache nothing would then
  serve;
* the container registry gated at all, when `ansible_ee_service` resolves the
  available ansible-core lines by listing tags through it — so a deployment with
  it off could not run a configure;
* and `registry.enabled` itself, which had **no reader anywhere**: setting it
  false stopped two scheduled tasks while `/v2/` went on serving push, pull and
  mirror. That flag is why the predecessor of this file existed.

Six per-ecosystem switches above one master switch above a capability resolver
was sixty-four configurations of which one was ever exercised. The provider
mirror and the module registry were never gated, because they are what Terrapod
is; the rest now match them.

**What the assertions are, and what each one can see.** The per-surface checks
are what catch ONE surface going missing, and they are derived from
`PACKAGE_ECOSYSTEMS` and from the mirror's own two protocol prefixes rather than
written down, so a seventh ecosystem is covered the day it is added. The single
total floor is a backstop against wholesale breakage only — dropping the four
provider-mirror routes would leave it comfortably satisfied, which is precisely
why it is not the assertion that matters.

Two `enabled` flags under `settings.registry` survive deliberately and must not
be read as leftovers: `oci.gc.enabled` governs a blob-reclamation sweep rather
than the registry serving, and `module_interface.enabled` is a real feature with
its own readers.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from terrapod.api.app import create_application
from terrapod.api.prefixes import MIRROR_LEGACY_PREFIX, MIRROR_PREFIX
from terrapod.config import settings
from terrapod.services.warm_ahead import PACKAGE_ECOSYSTEMS

#: Below this, something has stopped being mounted wholesale. Measured at 105
#: unique paths across the surfaces in `_SURFACES`, so it is slack on purpose:
#: it exists to fail loudly if the application stops building its caches at all,
#: and the per-surface assertions below are what catch one of them going.
_TOTAL_FLOOR = 80


def _paths(app) -> list[str]:
    return [r.path for r in app.routes if isinstance(r, APIRoute)]


#: Each surface, and the matcher for the paths it owns.
#:
#: The registry's is a PREFIX match, not a substring: `/v2` is also the tail of
#: the deprecated TFE alias `/api/v2/...`, so a substring match would report
#: terraform's own surface as the registry's and could never fail.
_SURFACES = {
    "the package-cache proxy": lambda p: "/package-cache/" in p and "/admin/" not in p,
    "the package-cache admin namespace": lambda p: "/admin/package-cache" in p,
    "the binary cache": lambda p: "/binary-cache" in p,
    "the provider mirror": lambda p: "/provider-mirror/" in p or p.startswith("/v1/providers/"),
    "the container registry": lambda p: p.startswith("/v2"),
}


@pytest.fixture(scope="module")
def app():
    """One build for the whole module — route inspection changes nothing."""
    return create_application()


class TestEveryCacheSurfaceIsMounted:
    """Non-zero per surface, which is what a single surface going missing trips."""

    @pytest.mark.parametrize("surface", sorted(_SURFACES))
    def test_it_has_routes(self, app, surface: str) -> None:
        mounted = [p for p in _paths(app) if _SURFACES[surface](p)]

        assert mounted, (
            f"{surface} is mounted on no routes at all — a cache that serves "
            "nothing is a surface half-present, which is the defect the "
            "capability flags produced and this file replaces"
        )

    def test_the_whole_cache_surface_clears_its_floor(self, app) -> None:
        """The backstop: it sees wholesale breakage, not one missing surface."""
        union = {p for p in _paths(app) for f in _SURFACES.values() if f(p)}

        assert len(union) >= _TOTAL_FLOOR, (
            f"only {len(union)} cache routes mounted, below the floor of "
            f"{_TOTAL_FLOOR} — the caches have largely stopped being built"
        )


class TestEveryEcosystemGetsItsOwnRoutes:
    """Derived from `PACKAGE_ECOSYSTEMS`, so a seventh arrives covered.

    The per-ecosystem half is where the finest gating lived: one flag each, so
    an ecosystem could be absent while its five siblings served. Asserting the
    aggregate alone cannot see that — five of six would satisfy it.
    """

    @pytest.mark.parametrize("ecosystem", PACKAGE_ECOSYSTEMS)
    def test_its_proxy_is_mounted(self, app, ecosystem: str) -> None:
        mounted = [
            p for p in _paths(app) if f"/package-cache/{ecosystem}/" in p and "/admin/" not in p
        ]

        assert mounted, f"no proxy routes for {ecosystem}"

    @pytest.mark.parametrize("ecosystem", PACKAGE_ECOSYSTEMS)
    def test_its_operator_upload_route_is_mounted(self, app, ecosystem: str) -> None:
        """One per ecosystem (#2599), and `build_upload_router` no longer
        returns None, so there is no configuration in which it is absent."""
        suffix = f"/admin/package-cache/{ecosystem}/artifacts"
        mounted = [p for p in _paths(app) if p.endswith(suffix)]

        assert mounted, f"no operator-upload route for {ecosystem}"


class TestTheMirrorServesBothOfItsProtocolPrefixes:
    """Structural rather than a count: terraform reaches it at the legacy
    prefix, and dropping either is a break for one client or the other."""

    @pytest.mark.parametrize("prefix", [MIRROR_PREFIX, MIRROR_LEGACY_PREFIX])
    def test_the_prefix_is_mounted(self, app, prefix: str) -> None:
        assert [p for p in _paths(app) if p.startswith(prefix)], prefix


class TestTheRegistryAnswersItsOwnHandshake:
    """`GET /v2/` is the first thing a container client asks, and a registry
    that does not answer it is not a v2 registry however much else is mounted."""

    def test_the_v2_root_is_mounted(self, app) -> None:
        assert "/v2/" in _paths(app), (
            "GET /v2/ is not mounted, so nothing answers the version check a "
            "container client makes before anything else -- and an Ansible "
            "configure resolves its execution environment through this registry"
        )


class TestThereIsNoCacheLeftToSwitchOff:
    """The other side of the invariant: unconditional because there is no flag.

    Without this, "mounted" is an observation about today's defaults rather than
    a property — the surfaces above would stay green while a flag came back and
    sat unread, which is exactly what `registry.enabled` did.
    """

    @pytest.mark.parametrize(
        "chain",
        [
            "enabled",
            "binary_cache.enabled",
            "provider_cache.enabled",
            "package_cache.enabled",
            *[f"package_cache.{e}.enabled" for e in PACKAGE_ECOSYSTEMS],
            "oci.enabled",
        ],
    )
    def test_the_flag_does_not_exist(self, chain: str) -> None:
        obj = settings.registry
        for part in chain.split("."):
            # `hasattr`, not a `getattr` default: these are booleans, so a
            # sentinel-free probe would read `False` as absent and pass on a
            # flag that is present and switched off.
            if not hasattr(obj, part):
                return
            obj = getattr(obj, part)

        pytest.fail(
            f"settings.registry.{chain} is back. Every cache is mounted "
            "unconditionally, so a flag here has no reader — add the reader or "
            "drop the flag, but do not leave it for an operator to set."
        )

    @pytest.mark.parametrize("chain", ["oci.gc.enabled", "module_interface.enabled"])
    def test_the_two_that_remain_are_still_there(self, chain: str) -> None:
        """Named so the sweep above is not read as "no flags anywhere", and so
        removing one of these fails here rather than passing as tidying."""
        obj = settings.registry
        for part in chain.split("."):
            assert hasattr(obj, part), f"settings.registry.{chain} has gone"
            obj = getattr(obj, part)


class TestNothingIsMountedTwice:
    """Carried over from the deleted gating file, where it was the one test not
    about gating at all.

    The factory exists so repeated application builds cannot accumulate routes.
    A module-level router mutated at startup would grow a duplicate set every
    time the application is constructed — invisible in production, which builds
    once, and a slow leak across a test session that builds hundreds of times.
    """

    def test_building_twice_yields_the_same_route_count(self) -> None:
        def count() -> int:
            app = create_application()
            return len([p for p in _paths(app) if "package-cache" in p])

        first = count()
        second = count()

        assert first == second > 0
