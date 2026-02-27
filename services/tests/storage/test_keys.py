"""
Tests for storage key path helpers.
"""

from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    policy_set_key,
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
