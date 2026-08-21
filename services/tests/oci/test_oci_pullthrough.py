"""Pull-through mirroring for the OCI registry (#1408).

Unit tier — resolution and policy, no HTTP.

The allow-list assertions are the important ones. Without them a client could
name any host in a repository path and make Terrapod issue a request to it,
using the API's own network position. That is a server-side request forgery
primitive, so the tests pin the *negative* behaviour explicitly rather than
trusting that a lookup happens to fail.
"""

from unittest.mock import patch

import pytest

from terrapod.config import OCIRegistryConfig, OCIUpstreamConfig
from terrapod.services.oci.pullthrough_service import (
    UpstreamUnavailable,
    _base_url,
    mirroring_allowed,
    resolve_upstream,
)


def _with(upstreams, cache_only=False):
    """Patch the registry config for one test."""
    oci = OCIRegistryConfig(upstreams=[OCIUpstreamConfig(**u) for u in upstreams])
    return patch.multiple(
        "terrapod.config.settings.registry", oci=oci, cache_only=cache_only, create=True
    )


class TestResolveUpstream:
    def test_a_configured_host_resolves(self) -> None:
        with _with([{"host": "quay.io"}]):
            assert resolve_upstream("quay.io/ansible/awx-ee") == ("quay.io", "ansible/awx-ee")

    def test_an_unconfigured_host_does_not(self) -> None:
        """The allow-list: naming a host does not make Terrapod fetch from it."""
        with _with([{"host": "quay.io"}]):
            assert resolve_upstream("evil.example.com/anything") is None

    def test_nothing_resolves_when_no_upstreams_are_configured(self) -> None:
        """Push-only is the default, and the right setting for an air gap."""
        with _with([]):
            assert resolve_upstream("quay.io/ansible/awx-ee") is None

    def test_a_bare_name_with_no_path_is_not_an_upstream(self) -> None:
        with _with([{"host": "quay.io"}]):
            assert resolve_upstream("quay.io") is None

    def test_the_host_must_be_the_whole_first_component(self) -> None:
        """`quay.io.evil.com/x` must not match a `quay.io` upstream."""
        with _with([{"host": "quay.io"}]):
            assert resolve_upstream("quay.io.evil.com/x") is None

    def test_a_local_name_that_merely_contains_a_host_does_not_resolve(self) -> None:
        with _with([{"host": "quay.io"}]):
            assert resolve_upstream("team/quay.io/thing") is None

    def test_multiple_upstreams_are_each_matched(self) -> None:
        with _with([{"host": "quay.io"}, {"host": "ghcr.io"}]):
            assert resolve_upstream("ghcr.io/org/img") == ("ghcr.io", "org/img")
            assert resolve_upstream("quay.io/org/img") == ("quay.io", "org/img")


class TestMirroringAllowed:
    def test_requires_at_least_one_upstream(self) -> None:
        with _with([]):
            assert mirroring_allowed() is False

    def test_allowed_when_configured(self) -> None:
        with _with([{"host": "quay.io"}]):
            assert mirroring_allowed() is True

    def test_cache_only_seals_it_shut(self) -> None:
        """Sealed mode must guarantee no upstream request is even attempted, so
        it overrides a configured upstream rather than being a separate switch
        someone could forget."""
        with _with([{"host": "quay.io"}], cache_only=True):
            assert mirroring_allowed() is False


class TestBaseUrl:
    def test_defaults_to_https_on_the_host(self) -> None:
        with _with([{"host": "quay.io"}]):
            assert _base_url("quay.io") == "https://quay.io"

    def test_an_explicit_api_url_wins(self) -> None:
        with _with([{"host": "internal", "api_url": "http://registry.internal:5000/"}]):
            assert _base_url("internal") == "http://registry.internal:5000"

    def test_an_unconfigured_host_raises_rather_than_building_a_url(self) -> None:
        """Belt and braces: resolve_upstream should already have refused, but a
        URL for an unconfigured host must never be constructed by accident."""
        with _with([{"host": "quay.io"}]):
            with pytest.raises(UpstreamUnavailable):
                _base_url("evil.example.com")
