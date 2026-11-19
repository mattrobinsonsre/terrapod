"""
Tests for storage key path helpers.
"""

import pytest

from terrapod.storage.keys import (
    WORKSPACE_KEY_PREFIXES,
    apply_log_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    policy_set_key,
    shared_prefix,
    state_backup_key,
    state_key,
)


class TestKeyHelpers:
    def test_state_key(self) -> None:
        assert state_key("ws-123", "sv-456") == "state/ws-123/sv-456.tfstate"

    def test_state_backup_key(self) -> None:
        assert state_backup_key("ws-123", "sv-456") == "state/ws-123/sv-456.backup.tfstate"

    def test_plan_log_key(self) -> None:
        assert plan_log_key("ws-123", "run-789") == "logs/ws-123/plans/run-789.log"

    def test_apply_log_key(self) -> None:
        assert apply_log_key("ws-123", "run-789") == "logs/ws-123/applies/run-789.log"

    def test_plan_output_key(self) -> None:
        assert plan_output_key("ws-123", "run-789") == "plans/ws-123/run-789.tfplan"

    def test_config_version_key(self) -> None:
        assert config_version_key("ws-123", "cv-001") == "config/ws-123/cv-001.tar.gz"

    def test_policy_set_key(self) -> None:
        assert policy_set_key("ps-abc", "pv-001") == "policies/ps-abc/pv-001.tar.gz"


class TestSharedPrefix:
    @pytest.mark.parametrize("prefix", ["backups/", "backups", "dr/state-dumps/", ""])
    def test_a_shared_prefix_is_normalised(self, prefix: str) -> None:
        assert shared_prefix("backup.prefix", prefix) == (
            prefix if prefix.endswith("/") else prefix + "/"
        )

    @pytest.mark.parametrize("workspace_prefix", WORKSPACE_KEY_PREFIXES)
    @pytest.mark.parametrize("suffix", ["", "db/"])
    def test_a_workspace_scoped_prefix_is_refused(self, workspace_prefix: str, suffix: str) -> None:
        with pytest.raises(ValueError, match="workspace-scoped prefix"):
            shared_prefix("backup.prefix", workspace_prefix + suffix)

    @pytest.mark.parametrize("prefix", ["./state/", "././logs", "./state/db"])
    def test_a_dot_slash_spelling_is_refused(self, prefix: str) -> None:
        with pytest.raises(ValueError, match="workspace-scoped prefix"):
            shared_prefix("backup.prefix", prefix)

    def test_a_prefix_without_its_slash_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'state/'"):
            shared_prefix("backup.prefix", "state")
