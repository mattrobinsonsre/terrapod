"""`validate_ansible_version` — what a workspace may pin (#2010).

The write-time rule is the deployment's pre-release policy and **nothing else**.
There is no format check, deliberately: `engine_version` has none either, and
pip's own message about a version PyPI does not publish is a better error than a
regex of ours guessing at what it does. These tests pin that absence as a
decision, so a future "tighten it up" has to argue with them rather than just
pass.
"""

from __future__ import annotations

import pytest

from terrapod.services.workspace_settings import validate_ansible_version


@pytest.fixture
def _ga_only(monkeypatch):
    from terrapod.config import settings

    monkeypatch.setattr(settings.registry.binary_cache, "allow_prerelease", "none")


class TestEmptyMeansInherit:
    """Empty is a value, not an absence — the semantics `engine_version_attr`
    documents: it means "the deployment's default", resolved at use time."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
    def test_blank_normalises_to_empty(self, raw, _ga_only):
        assert validate_ansible_version(raw) == ""

    def test_surrounding_whitespace_is_stripped_from_a_real_value(self, _ga_only):
        assert validate_ansible_version("  2.21.5  ") == "2.21.5"


class TestThePolicyIsTheOnlyRule:
    def test_a_ga_version_passes(self, _ga_only):
        assert validate_ansible_version("2.21.5") == "2.21.5"

    def test_a_prerelease_is_refused_and_the_message_names_the_policy(self, _ga_only):
        with pytest.raises(ValueError) as exc:
            validate_ansible_version("2.21.5rc1")
        msg = str(exc.value)
        # The operator has to be able to act on this: it must say which setting
        # refused them and what to set it to.
        assert "binary_cache.allow_prerelease" in msg
        assert "2.21.5rc1" in msg

    def test_the_same_prerelease_passes_once_the_policy_allows_it(self, monkeypatch):
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.binary_cache, "allow_prerelease", "rc")
        assert validate_ansible_version("2.21.5rc1") == "2.21.5rc1"


class TestThereIsDeliberatelyNoFormatRule:
    """Each of these reaches pip as written and fails there, by design.

    A partial is the one worth stating: `engine-version` resolves `1.13` to the
    newest matching release, and this does **not**, because nothing resolves it
    — so `2.18` is accepted here and then fails at install. Rejecting it would
    need a PyPI lookup at write time, which is a different feature.
    """

    @pytest.mark.parametrize("raw", ["2.18", "latest", "2.21.5+local", "not-a-version"])
    def test_it_is_passed_through_untouched(self, raw, _ga_only):
        assert validate_ansible_version(raw) == raw


class TestTheTypeIsChecked:
    """A non-string reached a `String(20)` column raw and became a DataError at
    commit — a 500 where this exists to give a 422."""

    @pytest.mark.parametrize("raw", [1, 2.21, True, ["2.21.5"], {"v": "2.21.5"}])
    def test_a_non_string_raises(self, raw, _ga_only):
        with pytest.raises(ValueError, match="must be a string"):
            validate_ansible_version(raw)
