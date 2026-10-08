"""Every optional capability's `enabled` flag has exactly one reader (#1986).

There is no engine on/off switch any more, so this module's whole job is the
half that was load-bearing: a flag an operator sets must actually be read. The
predecessor of this feature was an `enabled` flag on the OCI registry that
nothing in the code ever read, so `enabled: false` stopped two scheduled tasks
while `/v2/` went on serving push, pull and mirror.

`TestEveryFlagIsRead` is therefore **derived from `CAPABILITIES`**, not a list of
hand-written cases: it locates each capability's flag by name and proves that
flipping it flips the answer. A new capability added to the table with no reader
fails here rather than shipping unread, and a capability whose flag lives
somewhere this cannot find fails too — which is the right failure, because a flag
nobody can locate is a flag nobody reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from terrapod.config import settings
from terrapod.services.capabilities import (
    CAPABILITIES,
    capability_enabled,
    gated_capabilities,
)


def _find_flag_holder(capability: str) -> Any | None:
    """The object carrying `capability`'s own `enabled` flag, found by name.

    Deliberately a search rather than a lookup table: a second table would drift
    from `CAPABILITIES`, and the point of this test is that the table is the only
    thing it is told. Returns None when there is nothing to find, so the *test*
    reports that with a useful message rather than the fixture erroring every
    case in the file — a capability with no flag is one named failure, not
    twenty-two errors in tests that have nothing to do with it.
    """
    for parent in (settings.registry, settings.registry.package_cache):
        holder = getattr(parent, capability, None)
        if holder is not None and hasattr(holder, "enabled"):
            return holder
    return None


@pytest.fixture(autouse=True)
def _restore():
    """Settings are a process-wide singleton; put every flag back."""
    holders = [h for name in CAPABILITIES if (h := _find_flag_holder(name)) is not None]
    before = [(h, h.enabled) for h in holders]
    cache_before = settings.registry.package_cache.enabled
    yield
    for holder, value in before:
        holder.enabled = value
    settings.registry.package_cache.enabled = cache_before


class TestEveryFlagIsRead:
    """Derived from CAPABILITIES, so a new capability cannot arrive unread."""

    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_turning_it_off_stops_it_serving(self, capability: str) -> None:
        holder = _find_flag_holder(capability)
        assert holder is not None, (
            f"no `enabled` flag found for capability {capability!r} under "
            "settings.registry or settings.registry.package_cache — either the "
            "flag is missing or it lives somewhere this cannot prove "
            "capability_enabled() reads, and a flag nobody can locate is a flag "
            "nobody reads"
        )

        holder.enabled = False
        assert capability_enabled(capability) is False, (
            f"{capability} still serves with its own flag off — the flag has no "
            "reader, which is the defect the OCI registry shipped with"
        )

        holder.enabled = True
        assert capability_enabled(capability) is True

    @pytest.mark.parametrize("capability", CAPABILITIES)
    def test_it_appears_in_the_whole_picture(self, capability: str) -> None:
        """`gated_capabilities()` is what the UI probe and health surface read."""
        assert capability in gated_capabilities()

    def test_the_whole_picture_is_exactly_the_table(self) -> None:
        assert set(gated_capabilities()) == set(CAPABILITIES)


class TestDefaults:
    def test_everything_serves_out_of_the_box(self) -> None:
        """The platform should just work — upgrading switches nothing off."""
        assert gated_capabilities() == dict.fromkeys(CAPABILITIES, True)


class TestThePackageCacheMasterSwitch:
    """The ecosystem proxies need the cache as a whole on as well."""

    def test_it_stops_every_ecosystem_proxy(self) -> None:
        settings.registry.package_cache.enabled = False

        for capability in CAPABILITIES:
            if capability == "oci":
                continue
            assert capability_enabled(capability) is False, capability

    def test_it_does_not_touch_the_oci_registry(self) -> None:
        """The container registry is not part of the package cache."""
        settings.registry.package_cache.enabled = False

        assert capability_enabled("oci") is True


class TestTerraformsOwnCachesAreNotGateable:
    def test_they_are_absent_from_the_table(self) -> None:
        """The provider mirror, binary cache and module registry are not optional.

        They are what Terrapod is. Were one ever added to this table, turning a
        capability off would break terraform itself — the inverse of the failure
        this module exists to prevent.
        """
        for terraform_cache in ("provider_cache", "binary_cache", "modules", "terragrunt"):
            assert terraform_cache not in CAPABILITIES


class TestUnknownNames:
    def test_an_unknown_capability_raises(self) -> None:
        """A typo must fail loudly rather than silently reading as disabled.

        Deliberately not a plausible ecosystem name: this used to ask about
        "nuget", which stopped being unknown the moment the NuGet proxy shipped,
        so the test failed for a reason unrelated to what it was checking.
        """
        with pytest.raises(ValueError, match="unknown capability"):
            capability_enabled("not-a-real-capability")
