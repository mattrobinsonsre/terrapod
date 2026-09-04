"""Reducing Terrapod to its traditional terraform/tofu shape (#1429).

Two switches turn off everything that exists only for Ansible or Pulumi. The
rules under test are that an engine gate outranks a capability flag, and that a
capability shared by both engines survives while either is on.

What makes this worth testing carefully rather than trusting to a boolean: the
predecessor of this module was an `enabled` flag on the registry that nothing in
the code ever read, so an operator who switched the registry off still had a
registry serving push, pull and mirror.
"""

from __future__ import annotations

import pytest

from terrapod.config import settings
from terrapod.services.engine_gating import (
    capability_enabled,
    engine_enabled,
    gated_capabilities,
)


@pytest.fixture(autouse=True)
def _restore():
    """Settings are a process-wide singleton; put them back."""
    before = (
        settings.engines.ansible.enabled,
        settings.engines.pulumi.enabled,
        settings.registry.oci.enabled,
        settings.registry.package_cache.enabled,
        settings.registry.package_cache.pypi.enabled,
        settings.registry.package_cache.npm.enabled,
    )
    yield
    (
        settings.engines.ansible.enabled,
        settings.engines.pulumi.enabled,
        settings.registry.oci.enabled,
        settings.registry.package_cache.enabled,
        settings.registry.package_cache.pypi.enabled,
        settings.registry.package_cache.npm.enabled,
    ) = before


def _engines(*, ansible: bool, pulumi: bool) -> None:
    settings.engines.ansible.enabled = ansible
    settings.engines.pulumi.enabled = pulumi


class TestDefaults:
    def test_everything_is_on_out_of_the_box(self) -> None:
        """Upgrading must not silently switch a working capability off."""
        assert engine_enabled("ansible")
        assert engine_enabled("pulumi")
        assert gated_capabilities() == {"oci": True, "pypi": True, "npm": True, "galaxy": True}


class TestTheEngineGateOutranksTheCapabilityFlag:
    """The rule the whole feature rests on.

    Without it an operator would have to know which capability belongs to which
    engine for turning an engine off to mean anything — which is the knowledge
    this is meant to spare them.
    """

    def test_ansible_off_stops_the_registry_despite_its_own_flag(self) -> None:
        _engines(ansible=False, pulumi=True)
        settings.registry.oci.enabled = True

        assert capability_enabled("oci") is False

    def test_pulumi_off_stops_npm_despite_its_own_flag(self) -> None:
        _engines(ansible=True, pulumi=False)
        settings.registry.package_cache.npm.enabled = True

        assert capability_enabled("npm") is False

    def test_an_enabled_engine_does_not_override_a_capability_switched_off(self) -> None:
        """It outranks in one direction only — off wins, on does not force on."""
        _engines(ansible=True, pulumi=True)
        settings.registry.oci.enabled = False

        assert capability_enabled("oci") is False


class TestASharedCapability:
    """PyPI serves Pulumi's Python programs and Ansible's collection dependencies.

    The case that makes this a table rather than a boolean, and the one most
    likely to be got wrong by wiring each capability to a single engine.
    """

    def test_it_survives_on_ansible_alone(self) -> None:
        _engines(ansible=True, pulumi=False)
        assert capability_enabled("pypi") is True

    def test_it_survives_on_pulumi_alone(self) -> None:
        _engines(ansible=False, pulumi=True)
        assert capability_enabled("pypi") is True

    def test_it_stops_only_when_both_are_off(self) -> None:
        _engines(ansible=False, pulumi=False)
        assert capability_enabled("pypi") is False


class TestTheTraditionalShape:
    def test_both_engines_off_leaves_nothing_gated_serving(self) -> None:
        """What an operator who only writes HCL should end up with."""
        _engines(ansible=False, pulumi=False)

        assert gated_capabilities() == {
            "oci": False,
            "pypi": False,
            "npm": False,
            "galaxy": False,
        }

    def test_terraform_s_own_caches_are_not_gateable(self) -> None:
        """The provider mirror, binary cache and module registry are not optional.

        They are what Terrapod is. Were one ever added to the map, disabling an
        engine would break terraform itself — the precise failure this feature
        exists to prevent the inverse of.
        """
        from terrapod.services.engine_gating import _CAPABILITY_ENGINES

        for terraform_cache in ("provider_cache", "binary_cache", "modules", "terragrunt"):
            assert terraform_cache not in _CAPABILITY_ENGINES


class TestUnknownNames:
    """A typo must fail loudly rather than silently reading as disabled."""

    def test_an_unknown_capability_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown gated capability"):
            capability_enabled("nuget")

    def test_an_unknown_engine_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown engine"):
            engine_enabled("chef")
