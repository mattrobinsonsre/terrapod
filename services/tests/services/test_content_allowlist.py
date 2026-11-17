"""The content allowlist: what a deployment is willing to serve (#2589, Gap B).

`provider_cache.upstream_registries` restricts which HOSTS we reach. Nothing
restricted WHAT, so a workspace naming any package got it faithfully fetched,
verified and served -- verification working perfectly while an artifact signed by
its own author executes.

The properties worth pinning are about the MODES rather than about glob matching:
`off` must stay silent, `observe` must not refuse, and `enforce` must refuse
before anything is fetched. Each of those is a different failure if wrong, and
two of them are silent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.package_cache import content_allowlist, substrate
from terrapod.services.package_cache.content_allowlist import ContentRefused


def _cfg(mode="off", packages=None, ecosystems=None):
    """A settings stand-in for one mode. Patched on the module that READS it."""
    cfg = MagicMock()
    cfg.mode = mode
    cfg.packages = packages or []
    cfg.ecosystems = ecosystems or {}
    s = MagicMock()
    s.registry.content_allowlist = cfg
    return s


class TestTheModes:
    def test_off_permits_everything_and_logs_nothing(self) -> None:
        """The default. A control nobody configured must not add a line per fetch:
        that is how a feature gets switched off wholesale before it is ever used."""
        with (
            patch.object(content_allowlist, "settings", _cfg(mode="off")),
            patch.object(content_allowlist.logger, "info") as info,
        ):
            content_allowlist.check("npm", "left-pad", "left-pad-1.0.0.tgz")
        assert info.call_count == 0, "logged in 'off' mode"

    def test_observe_permits_an_unmatched_artifact_and_says_so(self) -> None:
        """`observe` is how the list gets built, so it must NOT refuse -- an
        operator running it is still learning what their estate pulls."""
        with (
            patch.object(content_allowlist, "settings", _cfg(mode="observe", packages=["@acme/*"])),
            patch.object(content_allowlist.logger, "info") as info,
        ):
            content_allowlist.check("npm", "left-pad", "left-pad-1.0.0.tgz")
        assert info.call_count == 1, "observe mode did not record the unmatched artifact"
        assert info.call_args.kwargs["name"] == "left-pad"

    def test_observe_is_silent_about_a_MATCHED_artifact(self) -> None:
        """Otherwise the log says nothing useful: every fetch would appear, and the
        set an operator is trying to discover would be buried in the set they
        already have."""
        with (
            patch.object(content_allowlist, "settings", _cfg(mode="observe", packages=["@acme/*"])),
            patch.object(content_allowlist.logger, "info") as info,
        ):
            content_allowlist.check("npm", "@acme/widget", "widget-1.0.0.tgz")
        assert info.call_count == 0

    def test_enforce_refuses_an_unmatched_artifact_naming_the_setting(self) -> None:
        with patch.object(
            content_allowlist, "settings", _cfg(mode="enforce", packages=["@acme/*"])
        ):
            with pytest.raises(ContentRefused) as exc:
                content_allowlist.check("npm", "left-pad", "left-pad-1.0.0.tgz")
        # The message has to name the setting and the way out, because this is the
        # error an operator meets mid-`npm install` with no other context.
        assert "content_allowlist" in str(exc.value)
        assert "observe" in str(exc.value)

    def test_enforce_permits_a_matched_artifact(self) -> None:
        with patch.object(
            content_allowlist, "settings", _cfg(mode="enforce", packages=["@acme/*"])
        ):
            content_allowlist.check("npm", "@acme/widget", "widget-1.0.0.tgz")

    def test_an_empty_list_under_enforce_refuses_everything_deliberately(self) -> None:
        """Fail-closed, and LOUD: every fetch fails at once with a message that
        says why. The alternative -- quietly permitting everything when the list is
        empty -- would make the whole posture a false claim, which is the one
        outcome worse than an outage."""
        with patch.object(content_allowlist, "settings", _cfg(mode="enforce", packages=[])):
            with pytest.raises(ContentRefused):
                content_allowlist.check("npm", "@acme/widget", "widget-1.0.0.tgz")

    def test_a_refusal_is_a_PermissionError_not_an_OSError_the_network_catches(self) -> None:
        """A bare `except OSError` meant for the network must not swallow a policy
        refusal and leave it looking like a flaky upstream."""
        assert issubclass(ContentRefused, PermissionError)
        assert not issubclass(ContentRefused, (ConnectionError, TimeoutError))


class TestTheTwoScopes:
    def test_a_per_ecosystem_pattern_is_UNIONED_with_the_global_one(self) -> None:
        """Not overriding: an estate that mirrors one namespace everywhere says so
        once, and a rule meaningful to one cache does not have to be repeated under
        the others. Overriding would silently drop the global patterns the moment
        anybody added a per-ecosystem one."""
        s = _cfg(mode="enforce", packages=["@acme/*"], ecosystems={"go": ["github.com/acme/*"]})
        with patch.object(content_allowlist, "settings", s):
            content_allowlist.check("go", "github.com/acme/mod", "v1.0.0.zip")  # per-ecosystem
            content_allowlist.check("go", "@acme/thing", "x")  # global still applies
            with pytest.raises(ContentRefused):
                content_allowlist.check("go", "github.com/evil/mod", "v1.0.0.zip")

    def test_one_ecosystems_pattern_does_not_leak_into_another(self) -> None:
        s = _cfg(mode="enforce", ecosystems={"go": ["github.com/acme/*"]})
        with patch.object(content_allowlist, "settings", s):
            content_allowlist.check("go", "github.com/acme/mod", "v1.0.0.zip")
            with pytest.raises(ContentRefused):
                # Same name, different cache: npm has no patterns at all here.
                content_allowlist.check("npm", "github.com/acme/mod", "x")

    def test_matching_is_case_insensitive(self) -> None:
        """Every cache this guards either addresses packages case-insensitively or
        normalises before it gets here, so a pattern that only matched one casing
        would refuse artifacts an operator believes they allowed."""
        with patch.object(
            content_allowlist, "settings", _cfg(mode="enforce", packages=["@ACME/*"])
        ):
            content_allowlist.check("npm", "@acme/widget", "x")

    def test_a_blank_pattern_matches_nothing_rather_than_everything(self) -> None:
        """An empty string is `fnmatch`'s match-nothing, but a stray blank line in a
        values file must not read as a wildcard -- which is the direction that would
        silently disable the control."""
        with patch.object(content_allowlist, "settings", _cfg(mode="enforce", packages=["", "  "])):
            with pytest.raises(ContentRefused):
                content_allowlist.check("npm", "anything", "x")


class TestItIsCheckedBeforeTheFetch:
    """The placement, which is the half no unit test of `check` can see.

    An artifact that has already been downloaded has already been reached for, so
    a refusal after the fetch does not mean anything. Driven through
    `fetch_and_cache` rather than asserting on the source.
    """

    @staticmethod
    def _artifact():
        a = MagicMock()
        a.ecosystem, a.name, a.filename, a.version = (
            "npm",
            "left-pad",
            "left-pad-1.0.0.tgz",
            "1.0.0",
        )
        return a

    async def test_an_unmatched_artifact_is_never_FETCHED(self) -> None:
        client = AsyncMock()
        with (
            patch.object(content_allowlist, "settings", _cfg(mode="enforce", packages=["@acme/*"])),
            patch.object(substrate, "sealed", return_value=False),
        ):
            with pytest.raises(ContentRefused):
                await substrate.fetch_and_cache(
                    AsyncMock(), MagicMock(), self._artifact(), client=client
                )
        assert client.method_calls == [], "reached upstream for an artifact it then refused"

    async def test_the_sealed_error_wins_so_it_is_not_masked(self) -> None:
        """A sealed node fetches nothing at all, so its own error is the more useful
        one. An allowlist refusal there would tell an operator to edit a list that
        was never the reason."""
        from terrapod.services.package_cache.substrate import SealedError

        with (
            patch.object(content_allowlist, "settings", _cfg(mode="enforce", packages=[])),
            patch.object(substrate, "sealed", return_value=True),
        ):
            with pytest.raises(SealedError):
                await substrate.fetch_and_cache(AsyncMock(), MagicMock(), self._artifact())
