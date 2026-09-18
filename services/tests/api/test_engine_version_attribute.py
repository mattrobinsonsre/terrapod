"""One engine version, under two names (#1559).

The column pins the version of whichever engine the workspace runs, so it is
called `engine_version` and the canonical attribute is `engine-version`.
`terraform-version` is the name it had when Terraform was the only engine, and
it keeps working -- permanently, not for a deprecation window: it is go-tfe's
own attribute name, so `tofu`, `terraform` and `tfci` send and read it.

These pin the three things that can go wrong with a two-name attribute: the old
name quietly stopping working, the new one not being read, and the two being
allowed to disagree so that a run executes with a version nobody asked for.
"""

import pytest
from fastapi import HTTPException

from terrapod.api.serialization import engine_version_attr


class TestReadingTheAttribute:
    def test_the_canonical_name_is_read(self):
        assert engine_version_attr({"engine-version": "1.13"}, "1.12") == "1.13"

    def test_the_old_name_still_works(self):
        # A client that has never heard of the rename -- go-tfe, tfci, an
        # operator's script -- must be unaffected.
        assert engine_version_attr({"terraform-version": "1.13"}, "1.12") == "1.13"

    def test_both_agreeing_is_fine(self):
        attrs = {"engine-version": "1.13", "terraform-version": "1.13"}
        assert engine_version_attr(attrs, "1.12") == "1.13"

    def test_both_disagreeing_is_refused(self):
        # Picking a winner would hide the bug and run a version nobody chose.
        with pytest.raises(HTTPException) as e:
            engine_version_attr({"engine-version": "1.13", "terraform-version": "1.9"}, "1.12")
        assert e.value.status_code == 422
        assert "disagree" in str(e.value.detail)

    def test_an_absent_key_takes_the_default(self):
        assert engine_version_attr({}, "1.12") == "1.12"

    @pytest.mark.parametrize("key", ["engine-version", "terraform-version"])
    def test_an_empty_value_is_a_value_not_an_absence(self, key):
        # Empty means "the deployment's default, resolved at run time", which is
        # not the same as the default this call was handed.
        assert engine_version_attr({key: ""}, "1.12") == ""

    def test_none_reads_as_empty_rather_than_the_string_none(self):
        assert engine_version_attr({"engine-version": None}, "1.12") == ""


class TestTheBulkUpdateSelector:
    """`terraform-version` normalises onto the canonical key before validation."""

    def _normalise(self, update):
        from terrapod.api.routers.workspace_bulk import _normalise_version_key

        return _normalise_version_key(update)

    def test_the_old_name_becomes_the_new_one(self):
        assert self._normalise({"terraform-version": "1.13"}) == {"engine-version": "1.13"}

    def test_the_canonical_name_wins_over_the_old_one(self):
        # Not a property of dict ordering: the canonical spelling is chosen.
        out = self._normalise({"terraform-version": "1.9", "engine-version": "1.13"})
        assert out == {"engine-version": "1.13"}

    def test_an_update_without_a_version_is_untouched(self):
        update = {"execution-mode": "agent"}
        assert self._normalise(update) is update

    def test_other_keys_survive_the_normalisation(self):
        out = self._normalise({"terraform-version": "1.13", "labels": {"env": "prod"}})
        assert out == {"engine-version": "1.13", "labels": {"env": "prod"}}


class TestTheWorkspaceSearchFilter:
    """A stored selector keeps its old spelling and must keep matching."""

    def test_the_old_spelling_is_accepted(self):
        from terrapod.services.workspace_search_service import parse_filter

        assert parse_filter({"terraform-version": "1.12"}).engine_version == "1.12"

    def test_the_underscored_old_spelling_is_accepted(self):
        # How it is stored in a variable-set assignment rule.
        from terrapod.services.workspace_search_service import parse_filter

        assert parse_filter({"terraform_version": "1.12"}).engine_version == "1.12"

    def test_the_canonical_spelling_is_accepted(self):
        from terrapod.services.workspace_search_service import parse_filter

        assert parse_filter({"engine-version": "1.12"}).engine_version == "1.12"

    def test_the_canonical_spelling_wins(self):
        from terrapod.services.workspace_search_service import parse_filter

        parsed = parse_filter({"engine-version": "1.13", "terraform_version": "1.9"})
        assert parsed.engine_version == "1.13"

    def test_a_typo_is_still_rejected(self):
        # The alias must not have opened the door to arbitrary keys.
        from terrapod.services.workspace_search_service import (
            WorkspaceFilterError,
            parse_filter,
        )

        with pytest.raises(WorkspaceFilterError):
            parse_filter({"terrafrom-version": "1.12"})


class TestTheRunnerWire:
    """A runner claims from an API that may be older or newer than it is."""

    def _options(self, attrs):
        from terrapod.engines.terraform import TerraformStrategy

        return TerraformStrategy().options_from_attrs(attrs, "plan")

    def test_the_canonical_attribute_is_read(self):
        assert self._options({"engine-version": "1.13"}).engine_version == "1.13"

    def test_an_older_api_sending_only_the_old_name_still_works(self):
        # The fallback is what lets a current runner claim a run from an API
        # that predates the rename.
        assert self._options({"terraform-version": "1.13"}).engine_version == "1.13"

    def test_the_canonical_attribute_wins(self):
        attrs = {"engine-version": "1.13", "terraform-version": "1.9"}
        assert self._options(attrs).engine_version == "1.13"

    def test_neither_leaves_it_to_the_platform_default(self):
        assert self._options({}).engine_version == ""


class TestRestoringAWorkspaceDeletedBeforeTheRename:
    """The snapshot is persisted JSON, so old rows carry the old key."""

    def test_an_old_marker_keeps_its_version(self):
        import inspect

        from terrapod.services import deleted_workspace_service

        source = inspect.getsource(deleted_workspace_service.restore_workspace)
        new_key = source.index('settings.get("engine_version")')
        old_key = source.index('settings.get("terraform_version")')
        # Both consulted, and the current one first -- a marker written after
        # the rename must not be overridden by a stale key.
        assert new_key < old_key
