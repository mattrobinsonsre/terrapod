"""Tests for variable CRUD and resolution service."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_invalid_category_raises(self):
        import pytest

        db = AsyncMock(spec=AsyncSession)
        with pytest.raises(ValueError, match="invalid variable category"):
            await create_variable(db, uuid.uuid4(), key="k", value="v", category="bogus")

    @patch("terrapod.services.variable_service.Variable")
    async def test_git_http_auth_forces_sensitive(self, MockVar):
        # git-auth categories hold secrets — sensitive is forced True even if the
        # caller passed False (#1028).
        db = AsyncMock(spec=AsyncSession)
        await create_variable(
            db,
            uuid.uuid4(),
            key="github.com",
            value="{}",
            category="git_http_auth",
            sensitive=False,
        )
        assert MockVar.call_args[1]["sensitive"] is True
        assert MockVar.call_args[1]["category"] == "git_http_auth"

    @patch("terrapod.services.variable_service.Variable")
    async def test_git_ssh_auth_forces_sensitive(self, MockVar):
        db = AsyncMock(spec=AsyncSession)
        await create_variable(
            db, uuid.uuid4(), key="gitlab.com", value="{}", category="git_ssh_auth", sensitive=False
        )
        assert MockVar.call_args[1]["sensitive"] is True

    @patch("terrapod.services.variable_service.Variable")
    async def test_version_id_set(self, MockVar):
        """Hashed over the CANONICAL category, so `terraform` and `native` on
        input produce one identity (#1898). It is written, never compared, so a
        row stamped before the rename keeps its old hash and nothing notices."""
        db = AsyncMock(spec=AsyncSession)
        await create_variable(db, uuid.uuid4(), key="k", value="v")
        call_kwargs = MockVar.call_args[1]
        assert call_kwargs["version_id"] == _version_hash("k", "v", "native")

    @patch("terrapod.services.variable_service.Variable")
    async def test_an_aliased_category_hashes_the_same(self, MockVar):
        db = AsyncMock(spec=AsyncSession)
        seen = []
        for written_as in ("native", "terraform", "pulumi_config"):
            await create_variable(db, uuid.uuid4(), key="k", value="v", category=written_as)
            seen.append(MockVar.call_args[1]["version_id"])
        assert len(set(seen)) == 1, "the alias must not produce a different version id"


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

    async def test_downgrade_without_value_clears_secret(self):
        """sensitive → non-sensitive with no new value must wipe the old value
        so the previously-hidden secret can't be returned in plaintext."""
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.value = "the-secret"
        var.sensitive = True
        var.category = "terraform"

        await update_variable(db, var, sensitive=False)
        assert var.sensitive is False
        assert var.value == ""
        assert var.version_id == _version_hash("k", "", "terraform")

    async def test_downgrade_with_fresh_value_keeps_it(self):
        """A downgrade that supplies a new value keeps that value (re-submit)."""
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.value = "the-secret"
        var.sensitive = True
        var.category = "terraform"

        await update_variable(db, var, value="now-public", sensitive=False)
        assert var.sensitive is False
        assert var.value == "now-public"

    async def test_non_sensitive_update_does_not_clear_value(self):
        """Editing an already-non-sensitive var without a value leaves it intact."""
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "k"
        var.value = "keep-me"
        var.sensitive = False
        var.category = "terraform"

        await update_variable(db, var, description="new desc")
        assert var.value == "keep-me"

    async def test_git_auth_update_cannot_downgrade_sensitive_or_clear_value(self):
        """#1028 audit C-3: a git-auth credential is force-sensitive — an update
        with sensitive=False must keep it sensitive AND preserve its value (the
        downgrade-clears-value path must NOT fire for git categories, else a valid
        credential would be silently wiped)."""
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "github.com"
        var.value = '{"token":"ghp_secret"}'
        var.sensitive = True
        var.category = "git_http_auth"

        await update_variable(db, var, sensitive=False)  # attempt downgrade
        assert var.sensitive is True  # forced back on
        assert var.value == '{"token":"ghp_secret"}'  # value preserved, not cleared


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


class TestTheBlastRadiusViewAgreesWithTheMatcher:
    """GHSA-49q6-pm68-3xgw refused two assignment-rule dimensions, and only the
    matcher learned about it.

    `_rule_matches` decides delivery and returns False for a rule selecting on
    `drift_status` or `locked`. `workspaces_for_varset` answers "who currently receives
    this variable set" — the screen an operator reads before rotating a credential —
    and applied the rule anyway. So a set whose rule used a refused dimension reached
    NOTHING while the view listed every workspace the rule would have selected.

    The direction matters: the view over-reported reach, which reads as "this
    credential is in use in twenty places" when it is in use in none. It is also
    exactly the wrong report about the effect of the fix.
    """

    @staticmethod
    def _varset(rule):
        return SimpleNamespace(
            id=uuid.uuid4(), global_set=False, assignment_rule=rule, name="creds"
        )

    @staticmethod
    def _result(rows):
        return MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        )

    @classmethod
    def _db(cls):
        """No explicit assignments, but the RULE query would return a workspace.

        The second half is load-bearing and the first version of these tests did not
        have it: a mock that returns nothing for every query makes the guard unfirable,
        so `out == []` held whether or not the refusal was applied and the tests passed
        under the very mutation they exist to catch. With a row behind the rule query,
        applying the rule yields one entry and refusing it yields none.
        """
        db = AsyncMock()
        ws = SimpleNamespace(id=uuid.uuid4(), name="would-have-matched")
        db.execute = AsyncMock(side_effect=[cls._result([]), cls._result([ws])])
        return db

    async def test_a_refused_dimension_reports_no_rule_derived_workspaces(self):
        from terrapod.services.variable_service import workspaces_for_varset

        db = self._db()
        out = await workspaces_for_varset(db, self._varset({"drift_status": "drifted"}))
        assert out == []

    async def test_the_other_refused_dimension_too(self):
        from terrapod.services.variable_service import workspaces_for_varset

        db = self._db()
        out = await workspaces_for_varset(db, self._varset({"locked": "true"}))
        assert out == []

    async def test_a_refused_dimension_mixed_with_a_good_one_still_reports_nothing(self):
        """The matcher refuses the whole rule, not the refused clause — so must this."""
        from terrapod.services.variable_service import workspaces_for_varset

        db = self._db()
        out = await workspaces_for_varset(
            db, self._varset({"labels": {"team": "net"}, "locked": "true"})
        )
        assert out == []

    async def test_an_ordinary_rule_is_unaffected(self):
        """The guard must not turn every rule-assigned set into an empty report."""
        from terrapod.services.variable_service import workspaces_for_varset

        db = self._db()
        with patch("terrapod.services.workspace_search_service.parse_filter") as pf:
            pf.side_effect = RuntimeError("reached the rule path")
            # Reaching the rule path at all is the assertion; the unusable-rule guard
            # inside swallows it and returns [], so no raise escapes.
            out = await workspaces_for_varset(db, self._varset({"labels": {"team": "net"}}))
            assert pf.called, "an ordinary rule must still be evaluated"
        assert out == []

    def test_every_assignment_rule_consumer_refuses_the_same_dimensions(self):
        """A fourth consumer added later would diverge in silence, exactly as the view
        did — and no behavioural test can see a reader that does not exist yet."""
        import pathlib
        import re

        from terrapod.services import varset_self_join

        #: Files that mention an assignment rule without DECIDING anything from it.
        #: Each needs a reason, or this becomes the place a diverging consumer hides.
        not_deciders = {
            "models.py": "declares the column; stores and returns it, never evaluates it",
        }

        root = pathlib.Path(varset_self_join.__file__).resolve().parents[1]
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "varset_self_join.py" or path.name in not_deciders:
                continue
            src = path.read_text()
            # A consumer is a file that reads an assignment rule to decide something.
            if not re.search(r"assignment_rule|_rule_matches\(", src):
                continue
            if "rule_refused_dimensions" in src or "_rule_refused" in src:
                continue
            # Writers that only store or serialize the rule are not deciders.
            if re.search(r"rule_refused_dimensions|RULE_DIMENSIONS_REFUSED", src):
                continue
            offenders.append(path.name)
        assert not offenders, (
            "these read an assignment rule without applying the refused-dimension "
            "predicate, so they can disagree with the matcher about who receives a "
            "credential:\n  " + "\n  ".join(sorted(offenders))
        )
