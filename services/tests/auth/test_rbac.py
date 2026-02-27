"""Tests for RBAC service — label matching and access control."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.rbac_service import _matches_labels, _merge_labels, check_access


class TestMergeLabels:
    def test_merge_list_values(self):
        target: dict[str, set[str]] = {}
        _merge_labels(target, {"env": ["prod", "staging"]})
        assert target == {"env": {"prod", "staging"}}

    def test_merge_string_value(self):
        target: dict[str, set[str]] = {}
        _merge_labels(target, {"env": "prod"})
        assert target == {"env": {"prod"}}

    def test_merge_into_existing(self):
        target: dict[str, set[str]] = {"env": {"prod"}}
        _merge_labels(target, {"env": ["staging"], "team": ["platform"]})
        assert target == {"env": {"prod", "staging"}, "team": {"platform"}}


class TestMatchesLabels:
    def test_match_found(self):
        resource_labels = {"env": "prod", "team": "platform"}
        permission_labels: dict[str, set[str]] = {"env": {"prod", "staging"}}
        assert _matches_labels(resource_labels, permission_labels) is True

    def test_no_match(self):
        resource_labels = {"env": "dev"}
        permission_labels: dict[str, set[str]] = {"env": {"prod", "staging"}}
        assert _matches_labels(resource_labels, permission_labels) is False

    def test_no_matching_key(self):
        resource_labels = {"team": "platform"}
        permission_labels: dict[str, set[str]] = {"env": {"prod"}}
        assert _matches_labels(resource_labels, permission_labels) is False

    def test_empty_permission_labels(self):
        resource_labels = {"env": "prod"}
        assert _matches_labels(resource_labels, {}) is False

    def test_empty_resource_labels(self):
        permission_labels: dict[str, set[str]] = {"env": {"prod"}}
        assert _matches_labels({}, permission_labels) is False


class TestCheckAccess:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=AsyncSession)
        # Default: no custom roles found
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute.return_value = mock_result
        return db

    async def test_admin_bypasses_all_checks(self, mock_db):
        result = await check_access(
            db=mock_db,
            user_email="admin@example.com",
            resource_name="my-workspace",
            resource_labels={"env": "prod"},
            role_names=["admin"],
        )
        assert result is True
        # Should not query DB for admin
        mock_db.execute.assert_not_called()

    async def test_everyone_allow_label_grants_access(self, mock_db):
        result = await check_access(
            db=mock_db,
            user_email="user@example.com",
            resource_name="public-resource",
            resource_labels={"access": "everyone"},
            role_names=["everyone"],
        )
        assert result is True

    async def test_no_matching_rule_denies(self, mock_db):
        result = await check_access(
            db=mock_db,
            user_email="user@example.com",
            resource_name="private-resource",
            resource_labels={"env": "prod"},
            role_names=["everyone"],
        )
        assert result is False

    async def test_custom_role_allow_by_name(self, mock_db):
        # Mock a custom role with allow_names
        mock_role = MagicMock()
        mock_role.allow_labels = {}
        mock_role.allow_names = ["my-workspace"]
        mock_role.deny_labels = {}
        mock_role.deny_names = []

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_role]
        mock_db.execute.return_value = mock_result

        result = await check_access(
            db=mock_db,
            user_email="user@example.com",
            resource_name="my-workspace",
            resource_labels={},
            role_names=["custom-role"],
        )
        assert result is True

    async def test_deny_overrides_allow(self, mock_db):
        mock_role = MagicMock()
        mock_role.allow_labels = {"env": ["prod"]}
        mock_role.allow_names = []
        mock_role.deny_labels = {}
        mock_role.deny_names = ["restricted-workspace"]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_role]
        mock_db.execute.return_value = mock_result

        result = await check_access(
            db=mock_db,
            user_email="user@example.com",
            resource_name="restricted-workspace",
            resource_labels={"env": "prod"},
            role_names=["custom-role"],
        )
        assert result is False

    async def test_deny_labels_override_allow(self, mock_db):
        mock_role = MagicMock()
        mock_role.allow_labels = {"env": ["prod", "staging"]}
        mock_role.allow_names = []
        mock_role.deny_labels = {"env": ["prod"]}
        mock_role.deny_names = []

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_role]
        mock_db.execute.return_value = mock_result

        result = await check_access(
            db=mock_db,
            user_email="user@example.com",
            resource_name="prod-workspace",
            resource_labels={"env": "prod"},
            role_names=["custom-role"],
        )
        assert result is False
