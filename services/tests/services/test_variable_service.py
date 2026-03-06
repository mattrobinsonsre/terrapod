"""Tests for variable CRUD and resolution service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.variable_service import (
    _version_hash,
    create_variable,
    delete_variable,
    resolve_variables,
    update_variable,
)

# ── _version_hash ──────────────────────────────────────────────────────


class TestVersionHash:
    def test_deterministic(self):
        h1 = _version_hash("key", "value", "terraform")
        h2 = _version_hash("key", "value", "terraform")
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        h1 = _version_hash("key", "value1", "terraform")
        h2 = _version_hash("key", "value2", "terraform")
        assert h1 != h2

    def test_length_is_16(self):
        assert len(_version_hash("k", "v", "c")) == 16


# ── create_variable ───────────────────────────────────────────────────


class TestCreateVariable:
    @patch("terrapod.services.variable_service.Variable")
    async def test_non_sensitive(self, MockVar):
        db = AsyncMock(spec=AsyncSession)
        ws_id = uuid.uuid4()

        await create_variable(db, ws_id, key="region", value="us-east-1", category="terraform")
        call_kwargs = MockVar.call_args[1]
        assert call_kwargs["key"] == "region"
        assert call_kwargs["value"] == "us-east-1"
        assert call_kwargs["sensitive"] is False
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @patch("terrapod.services.variable_service.Variable")
    async def test_sensitive_stores_value_directly(self, MockVar):
        db = AsyncMock(spec=AsyncSession)
        ws_id = uuid.uuid4()

        await create_variable(db, ws_id, key="secret", value="mysecret", sensitive=True)
        call_kwargs = MockVar.call_args[1]
        assert call_kwargs["value"] == "mysecret"
        assert call_kwargs["sensitive"] is True

    @patch("terrapod.services.variable_service.Variable")
    async def test_version_id_set(self, MockVar):
        db = AsyncMock(spec=AsyncSession)
        await create_variable(db, uuid.uuid4(), key="k", value="v")
        call_kwargs = MockVar.call_args[1]
        assert call_kwargs["version_id"] == _version_hash("k", "v", "terraform")


# ── update_variable ───────────────────────────────────────────────────


class TestUpdateVariable:
    async def test_partial_update_key(self):
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "old_key"
        var.value = "val"
        var.sensitive = False
        var.category = "terraform"

        await update_variable(db, var, key="new_key")
        assert var.key == "new_key"
        db.flush.assert_called_once()

    async def test_update_value(self):
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.sensitive = False
        var.category = "terraform"

        await update_variable(db, var, value="new_val")
        assert var.value == "new_val"

    async def test_update_sensitive_flag(self):
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.value = "plaintext"
        var.sensitive = False
        var.category = "terraform"

        await update_variable(db, var, sensitive=True)
        assert var.sensitive is True

    async def test_version_id_updated_on_value_change(self):
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.sensitive = False
        var.category = "terraform"

        await update_variable(db, var, value="v2")
        assert var.version_id == _version_hash("k", "v2", "terraform")


# ── resolve_variables ──────────────────────────────────────────────────


class TestResolveVariables:
    @patch("terrapod.services.variable_service._get_applicable_varsets")
    @patch("terrapod.services.variable_service.list_variables")
    async def test_workspace_vars_override_non_priority_varsets(
        self, mock_list_vars, mock_get_varsets
    ):
        """Layer 2 (workspace vars) overrides Layer 1 (non-priority varsets)."""
        ws_id = uuid.uuid4()

        # Non-priority varset with region=us-west-2
        vsv = MagicMock()
        vsv.key = "region"
        vsv.value = "us-west-2"
        vsv.sensitive = False
        vsv.category = "terraform"
        vsv.hcl = False

        varset = MagicMock()
        varset.variables = [vsv]

        mock_get_varsets.side_effect = [
            [varset],  # non-priority
            [],  # priority
        ]

        # Workspace var overrides to us-east-1
        ws_var = MagicMock()
        ws_var.key = "region"
        ws_var.value = "us-east-1"
        ws_var.sensitive = False
        ws_var.category = "terraform"
        ws_var.hcl = False
        mock_list_vars.return_value = [ws_var]

        result = await resolve_variables(AsyncMock(spec=AsyncSession), ws_id)

        by_key = {r.key: r for r in result}
        assert by_key["region"].value == "us-east-1"

    @patch("terrapod.services.variable_service._get_applicable_varsets")
    @patch("terrapod.services.variable_service.list_variables")
    async def test_priority_varsets_override_workspace_vars(self, mock_list_vars, mock_get_varsets):
        """Layer 3 (priority varsets) overrides Layer 2 (workspace vars)."""
        ws_id = uuid.uuid4()

        # Workspace var
        ws_var = MagicMock()
        ws_var.key = "env"
        ws_var.value = "dev"
        ws_var.sensitive = False
        ws_var.category = "terraform"
        ws_var.hcl = False
        mock_list_vars.return_value = [ws_var]

        # Priority varset overrides
        vsv = MagicMock()
        vsv.key = "env"
        vsv.value = "prod"
        vsv.sensitive = False
        vsv.category = "terraform"
        vsv.hcl = False

        priority_varset = MagicMock()
        priority_varset.variables = [vsv]

        mock_get_varsets.side_effect = [
            [],  # non-priority
            [priority_varset],  # priority
        ]

        result = await resolve_variables(AsyncMock(spec=AsyncSession), ws_id)
        by_key = {r.key: r for r in result}
        assert by_key["env"].value == "prod"

    @patch("terrapod.services.variable_service._get_applicable_varsets")
    @patch("terrapod.services.variable_service.list_variables")
    async def test_sensitive_vars_resolved(self, mock_list_vars, mock_get_varsets):
        ws_id = uuid.uuid4()
        mock_get_varsets.side_effect = [[], []]

        ws_var = MagicMock()
        ws_var.key = "secret"
        ws_var.value = "s3cret"
        ws_var.sensitive = True
        ws_var.category = "env"
        ws_var.hcl = False
        mock_list_vars.return_value = [ws_var]

        result = await resolve_variables(AsyncMock(spec=AsyncSession), ws_id)
        by_key = {r.key: r for r in result}
        assert by_key["secret"].value == "s3cret"
        assert by_key["secret"].sensitive is True

    @patch("terrapod.services.variable_service._get_applicable_varsets")
    @patch("terrapod.services.variable_service.list_variables")
    async def test_multiple_vars_from_all_layers(self, mock_list_vars, mock_get_varsets):
        """Vars from all three layers are merged correctly."""
        ws_id = uuid.uuid4()

        # Non-priority varset: base_url
        vsv_base = MagicMock()
        vsv_base.key = "base_url"
        vsv_base.value = "https://api.dev"
        vsv_base.sensitive = False
        vsv_base.category = "env"
        vsv_base.hcl = False

        non_priority = MagicMock()
        non_priority.variables = [vsv_base]

        # Workspace: region
        ws_var = MagicMock()
        ws_var.key = "region"
        ws_var.value = "eu-west-1"
        ws_var.sensitive = False
        ws_var.category = "terraform"
        ws_var.hcl = False
        mock_list_vars.return_value = [ws_var]

        # Priority varset: override_key
        vsv_override = MagicMock()
        vsv_override.key = "override_key"
        vsv_override.value = "forced"
        vsv_override.sensitive = False
        vsv_override.category = "terraform"
        vsv_override.hcl = False

        priority = MagicMock()
        priority.variables = [vsv_override]

        mock_get_varsets.side_effect = [[non_priority], [priority]]

        result = await resolve_variables(AsyncMock(spec=AsyncSession), ws_id)
        by_key = {r.key: r for r in result}
        assert len(by_key) == 3
        assert by_key["base_url"].value == "https://api.dev"
        assert by_key["region"].value == "eu-west-1"
        assert by_key["override_key"].value == "forced"

    @patch("terrapod.services.variable_service._get_applicable_varsets")
    @patch("terrapod.services.variable_service.list_variables")
    async def test_empty_workspace_returns_empty(self, mock_list_vars, mock_get_varsets):
        mock_get_varsets.side_effect = [[], []]
        mock_list_vars.return_value = []
        result = await resolve_variables(AsyncMock(spec=AsyncSession), uuid.uuid4())
        assert result == []


# ── delete_variable ────────────────────────────────────────────────────


class TestDeleteVariable:
    async def test_deletes_and_flushes(self):
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        await delete_variable(db, var)
        db.delete.assert_called_once_with(var)
        db.flush.assert_called_once()
