"""Tests for RBAC service — label matching, allow/deny logic, admin bypass."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.rbac_service import (
    check_access,
    matches_labels,
    merge_labels,
)


class TestMergeLabels:
    def test_merges_list_values(self):
        target: dict[str, set[str]] = {}
        merge_labels(target, {"env": ["prod", "staging"]})
        assert target == {"env": {"prod", "staging"}}

    def test_merges_string_value(self):
        target: dict[str, set[str]] = {}
        merge_labels(target, {"team": "platform"})
        assert target == {"team": {"platform"}}

    def test_merges_into_existing(self):
        target: dict[str, set[str]] = {"env": {"prod"}}
        merge_labels(target, {"env": ["staging"]})
        assert target == {"env": {"prod", "staging"}}

    def test_merges_multiple_keys(self):
        target: dict[str, set[str]] = {}
        merge_labels(target, {"env": ["prod"], "team": ["sre"]})
        assert target == {"env": {"prod"}, "team": {"sre"}}

    def test_empty_source(self):
        target: dict[str, set[str]] = {"env": {"prod"}}
        merge_labels(target, {})
        assert target == {"env": {"prod"}}


class TestMatchesLabels:
    def test_matches_when_value_present(self):
        assert matches_labels({"env": "prod"}, {"env": {"prod", "staging"}}) is True

    def test_no_match_when_value_absent(self):
        assert matches_labels({"env": "dev"}, {"env": {"prod", "staging"}}) is False

    def test_no_match_when_key_absent(self):
        assert matches_labels({"team": "sre"}, {"env": {"prod"}}) is False

    def test_empty_resource_labels(self):
        assert matches_labels({}, {"env": {"prod"}}) is False

    def test_empty_permission_labels(self):
        assert matches_labels({"env": "prod"}, {}) is False

    def test_multiple_permission_keys_any_match(self):
        perms: dict[str, set[str]] = {"env": {"prod"}, "team": {"sre"}}
        assert matches_labels({"team": "sre"}, perms) is True

    def test_no_match_all_keys_miss(self):
        perms: dict[str, set[str]] = {"env": {"prod"}, "team": {"sre"}}
        assert matches_labels({"region": "eu"}, perms) is False


def _make_role(
    *,
    allow_labels: dict | None = None,
    allow_names: list | None = None,
    deny_labels: dict | None = None,
    deny_names: list | None = None,
):
    role = MagicMock()
    role.allow_labels = allow_labels or {}
    role.allow_names = allow_names or []
    role.deny_labels = deny_labels or {}
    role.deny_names = deny_names or []
    return role


def _mock_db_with_roles(roles):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = roles
    db.execute.return_value = result
    return db


class TestCheckAccess:
    @pytest.mark.asyncio
    async def test_admin_always_allowed(self):
        db = AsyncMock()
        assert await check_access(db, "user@test.com", "ws-1", {}, ["admin"]) is True
        # DB should not be queried for admin
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_everyone_role_with_access_label(self):
        db = _mock_db_with_roles([])
        result = await check_access(
            db, "user@test.com", "ws-public", {"access": "everyone"}, ["everyone"]
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_everyone_role_without_access_label(self):
        db = _mock_db_with_roles([])
        result = await check_access(
            db, "user@test.com", "ws-private", {"team": "sre"}, ["everyone"]
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_role_allow_by_label(self):
        role = _make_role(allow_labels={"env": ["prod"]})
        db = _mock_db_with_roles([role])
        result = await check_access(db, "user@test.com", "ws-prod", {"env": "prod"}, ["deployer"])
        assert result is True

    @pytest.mark.asyncio
    async def test_custom_role_allow_by_name(self):
        role = _make_role(allow_names=["ws-special"])
        db = _mock_db_with_roles([role])
        result = await check_access(db, "user@test.com", "ws-special", {}, ["deployer"])
        assert result is True

    @pytest.mark.asyncio
    async def test_deny_by_name_overrides_allow(self):
        role = _make_role(
            allow_labels={"env": ["prod"]},
            deny_names=["ws-restricted"],
        )
        db = _mock_db_with_roles([role])
        result = await check_access(
            db, "user@test.com", "ws-restricted", {"env": "prod"}, ["deployer"]
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_deny_by_label_overrides_allow(self):
        role = _make_role(
            allow_labels={"env": ["prod"]},
            deny_labels={"sensitive": ["true"]},
        )
        db = _mock_db_with_roles([role])
        result = await check_access(
            db,
            "user@test.com",
            "ws-sensitive",
            {"env": "prod", "sensitive": "true"},
            ["deployer"],
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_roles_no_access(self):
        db = _mock_db_with_roles([])
        result = await check_access(db, "user@test.com", "ws-1", {"env": "prod"}, [])
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_roles_any_allow(self):
        role1 = _make_role(allow_labels={"env": ["staging"]})
        role2 = _make_role(allow_labels={"env": ["prod"]})
        db = _mock_db_with_roles([role1, role2])
        result = await check_access(
            db, "user@test.com", "ws-prod", {"env": "prod"}, ["viewer", "deployer"]
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_builtin_roles_not_queried_as_custom(self):
        """Built-in roles (admin, audit, everyone) should not trigger DB queries for Role objects."""
        db = _mock_db_with_roles([])
        await check_access(db, "user@test.com", "ws-1", {}, ["everyone", "audit"])
        # DB is not queried because there are no custom role names
        db.execute.assert_not_called()
