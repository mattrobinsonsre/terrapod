"""The pre-release policy, applied to a PyPI version (#2010).

`allow_prerelease` already gated terraform/tofu versions, and ansible-core is
pinned by the same operator for the same reason, so it answers to the same
policy. It needed a second implementation anyway, and *that* is the point of
this file: PyPI and GitHub do not spell a pre-release the same way, so the
existing helper is silently wrong about every ansible pre-release.
"""

from __future__ import annotations

import pytest

from terrapod.services.binary_cache_service import (
    _parse_stability,
    is_pypi_version_allowed,
    pep440_stability,
)


class TestPep440Stability:
    @pytest.mark.parametrize(
        ("version", "tier"),
        [
            ("2.21.5", "stable"),
            ("2.21.5rc1", "rc"),
            ("2.21.5c1", "rc"),
            ("2.21.5b2", "beta"),
            ("2.21.5a1", "alpha"),
            ("2.21.5.dev7", "dev"),
            # A post-release is a *stable* release that followed one.
            ("2.21.5.post1", "stable"),
            ("", "stable"),
        ],
    )
    def test_the_tier_is_read_off_the_version(self, version, tier):
        assert pep440_stability(version) == tier

    def test_dev_wins_over_the_rc_it_follows(self):
        """`.dev` is the lowest tier, so it decides even beside an `rc`."""
        assert pep440_stability("2.21.5rc1.dev1") == "dev"


class TestTheHyphenlessSpellingIsWhyThisExists:
    """The bug a second implementation exists to avoid, pinned both ways.

    `_parse_stability` looks for a **hyphenated** tag (`1.15.0-rc2`), which is
    how GitHub release tags are written. PEP 440 writes it `2.21.5rc1` with no
    hyphen, so handed a PyPI version that helper reports every pre-release as
    stable -- and a GA-only deployment would accept an ansible-core release
    candidate without a word.

    Asserting the wrong answer deliberately: if `_parse_stability` is ever
    taught PEP 440 this fails, and at that point the two can be merged. Until
    then it stops anyone "simplifying" by reusing it here.
    """

    @pytest.mark.parametrize("version", ["2.21.5rc1", "2.21.5b2", "2.21.5a1", "2.21.5.dev7"])
    def test_the_github_shaped_helper_calls_every_pypi_prerelease_stable(self, version):
        assert _parse_stability(version) == "stable"
        assert pep440_stability(version) != "stable"


class TestIsPypiVersionAllowed:
    @pytest.mark.parametrize(
        ("version", "policy", "allowed"),
        [
            # GA is allowed under every policy, including the strictest.
            ("2.21.5", "none", True),
            ("2.21.5", "dev", True),
            # A GA-only deployment refuses every pre-release tier.
            ("2.21.5rc1", "none", False),
            ("2.21.5b2", "none", False),
            ("2.21.5a1", "none", False),
            ("2.21.5.dev7", "none", False),
            # Each policy names the LOWEST tier it accepts, so it admits
            # everything above it and nothing below.
            ("2.21.5rc1", "rc", True),
            ("2.21.5b2", "rc", False),
            ("2.21.5b2", "beta", True),
            ("2.21.5a1", "beta", False),
            ("2.21.5a1", "alpha", True),
            ("2.21.5.dev7", "alpha", False),
            ("2.21.5.dev7", "dev", True),
        ],
    )
    def test_the_policy_names_the_lowest_tier_accepted(self, version, policy, allowed):
        assert is_pypi_version_allowed(version, policy) is allowed

    def test_an_unknown_policy_falls_back_to_ga_only(self):
        """Fail closed: a policy value nobody recognises must not widen it."""
        assert is_pypi_version_allowed("2.21.5", "nonsense") is True
        assert is_pypi_version_allowed("2.21.5rc1", "nonsense") is False

    def test_it_reads_the_deployment_policy_when_none_is_passed(self, monkeypatch):
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.binary_cache, "allow_prerelease", "none")
        assert is_pypi_version_allowed("2.21.5rc1") is False
        monkeypatch.setattr(settings.registry.binary_cache, "allow_prerelease", "rc")
        assert is_pypi_version_allowed("2.21.5rc1") is True
