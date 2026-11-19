"""The three aggregate commit-status names and the per-workspace switch."""

import pytest
from pydantic import ValidationError

from terrapod.config import CommitStatusConfig, Settings, VCSConfig

_NAME_FIELDS = ("plan_name", "policy_name", "apply_name")


class TestDefaults:
    def test_names_and_switch(self):
        c = VCSConfig().commit_statuses
        assert c.plan_name == "terrapod/plan"
        assert c.policy_name == "terrapod/policy"
        assert c.apply_name == "terrapod/apply"
        assert c.per_workspace is True
        assert c.enabled is True

    def test_reachable_from_settings(self):
        assert Settings().vcs.commit_statuses.plan_name == "terrapod/plan"

    def test_custom_names_are_accepted(self):
        c = CommitStatusConfig(plan_name="ci/tf-plan", policy_name="ci/tf-policy", apply_name="x")
        assert (c.plan_name, c.policy_name, c.apply_name) == ("ci/tf-plan", "ci/tf-policy", "x")

    def test_the_aggregates_can_be_turned_off(self):
        assert CommitStatusConfig(enabled=False).enabled is False

    def test_per_workspace_can_be_turned_off(self):
        assert CommitStatusConfig(per_workspace=False).per_workspace is False

    def test_the_longest_allowed_name_is_accepted(self):
        assert CommitStatusConfig(plan_name="a" * 255).plan_name == "a" * 255


class TestRejections:
    @pytest.mark.parametrize("field", _NAME_FIELDS)
    @pytest.mark.parametrize(
        "bad",
        ["", " lead", "trail ", "\tx", "x\n", "a\x00b", "a\x7fb", "a\u200bb", "a" * 256],
    )
    def test_invalid_value_is_rejected_and_names_the_field(self, field, bad):
        with pytest.raises(ValidationError) as exc:
            CommitStatusConfig(**{field: bad})
        assert field in str(exc.value)

    def test_a_value_is_rejected_not_normalised(self):
        with pytest.raises(ValidationError):
            CommitStatusConfig(plan_name="  terrapod/plan  ")

    def test_duplicate_names_are_rejected_naming_both(self):
        with pytest.raises(ValidationError) as exc:
            CommitStatusConfig(plan_name="same", apply_name="same")
        msg = str(exc.value)
        assert "plan_name" in msg and "apply_name" in msg

    def test_a_default_collision_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            CommitStatusConfig(policy_name="terrapod/plan")
        msg = str(exc.value)
        assert "plan_name" in msg and "policy_name" in msg

    def test_a_bad_name_fails_settings_at_startup(self):
        with pytest.raises(ValidationError):
            Settings(vcs={"commit_statuses": {"plan_name": ""}})
