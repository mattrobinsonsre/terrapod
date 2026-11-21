"""Tests for variable CRUD and resolution service."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services import variable_service
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

    async def test_the_credential_categories_are_exactly_the_four(self):
        """The floor the two derived tests below cannot be.

        Measured: narrowing `CREDENTIAL_CATEGORIES` back to the git pair left
        all 80 tests in this file green, because a test parametrized over the
        set simply runs over fewer categories -- which is the whole of the
        "a derived gate can pass on nothing" trap, in a gate written to catch
        a related one. The provider's own map carries the same floor for the
        same reason.

        Stated as the union of its two halves rather than as four literals, so
        a category added to either half must be added here deliberately.
        """
        assert variable_service.CREDENTIAL_CATEGORIES == (
            variable_service.GIT_AUTH_CATEGORIES | variable_service.ANSIBLE_AUTH_CATEGORIES
        )
        assert variable_service.GIT_AUTH_CATEGORIES == {"git_http_auth", "git_ssh_auth"}
        assert variable_service.ANSIBLE_AUTH_CATEGORIES == {
            "ansible_machine_auth",
            "ansible_vault_auth",
        }
        # And every one of them is a category the API will actually accept.
        assert variable_service.CREDENTIAL_CATEGORIES <= variable_service.VALID_CATEGORIES

    @pytest.mark.parametrize(
        "category",
        sorted(variable_service.CREDENTIAL_CATEGORIES),
    )
    async def test_every_credential_category_is_forced_sensitive_on_create(self, category):
        """DERIVED from `CREDENTIAL_CATEGORIES`, not a list of names.

        The rule is "this value is a JSON credential envelope, so it cannot be
        anything but a secret". A test listing the categories would have passed
        unchanged when #1933 added two more -- which is the shape that let the
        provider's own always-sensitive map go stale at the same time.
        """
        db = AsyncMock(spec=AsyncSession)
        var = await variable_service.create_variable(
            db,
            uuid.uuid4(),
            key="scope",
            value="{}",
            category=category,
            sensitive=False,
        )
        assert var.sensitive is True, f"{category} must be forced sensitive"

    @pytest.mark.parametrize(
        "category",
        sorted(variable_service.CREDENTIAL_CATEGORIES),
    )
    async def test_every_credential_category_resists_a_downgrade_on_update(self, category):
        """And the value survives: the downgrade-clears-value path must not fire
        for a credential, or a valid one would be silently wiped."""
        db = AsyncMock(spec=AsyncSession)
        var = MagicMock()
        var.key = "scope"
        var.value = '{"password":"p"}'
        var.sensitive = True
        var.category = category
        var.value_source = "static"

        await update_variable(db, var, sensitive=False)
        assert var.sensitive is True
        assert var.value == '{"password":"p"}'

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

    async def test_the_autodiscovery_rule_dimension_too_in_either_spelling(self):
        from terrapod.services.variable_service import workspaces_for_varset

        for key in ("autodiscovery_rule_id", "autodiscovery-rule-id"):
            out = await workspaces_for_varset(self._db(), self._varset({key: str(uuid.uuid4())}))
            assert out == [], key

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

    @staticmethod
    def _valid_value(dim):
        """A value the selector would PARSE for this field. A value that fails to parse
        makes a rule match nothing whether or not the refusal ran, hiding a consumer
        that skips the check."""
        from terrapod.services.workspace_search_service import WorkspaceFilter

        annotation = str(WorkspaceFilter.model_fields[dim].annotation)
        if "bool" in annotation:
            return True
        # An id dimension is parsed through `uuid.UUID`, so "x" raises
        # WorkspaceFilterError and the shape would match nothing for a reason
        # that has nothing to do with the refusal -- which is exactly what this
        # guard exists to rule out. Derived from the name rather than listed, so
        # the next id dimension is covered without anyone remembering.
        if dim.endswith("_id"):
            return "00000000-0000-4000-8000-000000000000"
        return "x"

    @classmethod
    def _positions(cls, dim):
        """Every shape a refused dimension can take in a rule, positive and excluded."""
        h = dim.replace("_", "-")
        v = cls._valid_value(dim)
        return [
            {"labels": {"t": "x"}, dim: v},
            {"labels": {"t": "x"}, h: v},
            {"labels": {"t": "x"}, "exclude": [{dim: v}]},
            {"labels": {"t": "x"}, "exclude": [{h: v}]},
            {
                "labels": {"t": "x"},
                "exclude": [{"labels": {"a": "b"}}, {"labels": {"c": "d"}, dim: v}],
            },
            {"labels": {"t": "x"}, "exclude": [{"labels": {"a": "b"}, "exclude": [{dim: v}]}]},
        ]

    def test_every_refused_shape_parses_when_the_refusal_is_ignored(self):
        """Guards the test below: each shape must be a rule that WOULD match, so the
        refusal is the only thing stopping it. The nested shape fails to parse by
        design, so it is exempt."""
        from terrapod.services.varset_self_join import RULE_DIMENSIONS_REFUSED
        from terrapod.services.workspace_search_service import build_workspace_query, parse_filter

        for dim in RULE_DIMENSIONS_REFUSED:
            for rule in self._positions(dim)[:5]:
                build_workspace_query(parse_filter(rule))

    def test_an_exclusion_entry_is_refused_exactly_as_a_positive_dimension_is(self):
        """Parametrised over the denylist, so a dimension added to it is covered in
        every position without anyone remembering to extend this test."""
        from terrapod.services.varset_self_join import (
            RULE_DIMENSIONS_REFUSED,
            rule_refused_dimensions,
        )

        assert RULE_DIMENSIONS_REFUSED
        for dim in RULE_DIMENSIONS_REFUSED:
            for rule in self._positions(dim):
                assert rule_refused_dimensions(rule) == [dim], rule

    async def test_every_consumer_refuses_every_position_of_every_refused_dimension(self):
        """The matcher, the blast-radius view and the write path each go through the
        predicate. A consumer that read only top-level keys would pass the test above
        and still let an exclusion entry through, so ask each of them."""
        from fastapi import HTTPException

        from terrapod.api.routers.variables import _exclusion_meta, _validated_assignment_rule
        from terrapod.services.variable_service import (
            _rule_matches,
            workspaces_for_varset,
        )
        from terrapod.services.varset_self_join import RULE_DIMENSIONS_REFUSED

        for dim in RULE_DIMENSIONS_REFUSED:
            for rule in self._positions(dim):
                with pytest.raises(HTTPException) as e:
                    _validated_assignment_rule({"assignment-rule": rule})
                assert e.value.status_code == 422
                assert RULE_DIMENSIONS_REFUSED[dim] in e.value.detail
                assert not await _rule_matches(None, rule, uuid.uuid4()), rule
                db = self._db()
                assert await workspaces_for_varset(db, self._varset(rule)) == [], rule
                # The blast-radius view's exclusion summary is the fourth consumer, and
                # it was the one with no test: deleting its guard left the whole suite
                # green (#2621). Without it the view reports `meta.exclusion` as though
                # the rule had carved workspaces out of a match that is in fact EMPTY --
                # the view-vs-matcher divergence GHSA-49q6-pm68-3xgw was about, in the
                # same view, one release later.
                assert await _exclusion_meta(self._db(), self._varset(rule)) is None, rule

    def test_the_refusal_predicate_sees_a_dimension_under_its_legacy_spelling(self):
        """The latent half of the four-copy divergence (#2621).

        The predicate and the selector must agree about a SPELLING. Before this
        the predicate folded no alias while `_parse_entry` did, so the day
        `engine_version` joins the denylist a rule stored as `terraform-version`
        would be refused by NOTHING and matched perfectly well -- the
        refusal/matcher disagreement GHSA-49q6-pm68-3xgw was about, reintroduced
        by an alias rather than by a nesting.

        Driven with the denylist patched rather than waiting for that day: the
        property is that the predicate reads the canonical spelling, and asserting
        it on a dimension that is not yet refused would assert nothing.
        """
        from unittest.mock import patch

        from terrapod.services import varset_self_join as vsj

        with patch.dict(vsj.RULE_DIMENSIONS_REFUSED, {"engine_version": "a reason"}):
            for spelling in ("engine_version", "terraform_version", "terraform-version"):
                assert vsj.rule_refused_dimensions({spelling: "1.9.0"}) == ["engine_version"], (
                    spelling
                )

    def test_the_write_path_stores_the_rule_byte_identically(self):
        """The other side of that fix, and the reason it is two functions.

        `assignmentRuleModel` maps `engine_version` and `terraform_version` to
        separate provider attributes because "a stored rule must round-trip
        byte-identical or every Read produces drift". Canonicalising here turns
        one `terraform_version` into a permanent diff on every plan.
        """
        from terrapod.api.routers.variables import _validated_assignment_rule

        stored = _validated_assignment_rule({"assignment-rule": {"terraform_version": "1.9.0"}})
        assert stored == {"terraform_version": "1.9.0"}, (
            "the write path canonicalised the alias; the provider would see permanent drift"
        )
        # A hyphen IS folded here, and always was: it is a wire spelling of the
        # same key rather than a different selector.
        assert _validated_assignment_rule({"assignment-rule": {"name-prefix": "p"}}) == {
            "name_prefix": "p"
        }

    def test_no_storing_path_reaches_the_canonicalising_form(self):
        """Structural, because the behavioural test above pins one attribute.

        A second write path -- or this one widened -- would reintroduce the drift
        silently, and no round-trip test exists for a rule key nobody has added
        yet.
        """
        import ast
        import pathlib

        from terrapod.api.routers import variables as mod

        tree = ast.parse(pathlib.Path(mod.__file__).read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            seg = ast.get_source_segment(pathlib.Path(mod.__file__).read_text(), node) or ""
            if "canonical_filter_keys" in seg:
                offenders.append(node.name)
        assert not offenders, (
            "these are on the router that STORES an assignment rule and reach the "
            "canonicalising normaliser, which breaks the provider's byte-identical "
            f"round-trip: {offenders}"
        )

    def test_every_assignment_rule_consumer_refuses_the_same_dimensions(self):
        """A fourth consumer added later would diverge in silence, exactly as the view
        did -- and no behavioural test can see a reader that does not exist yet.

        PER FUNCTION, not per file (#2621). This used to skip any file whose source
        merely CONTAINED `rule_refused_dimensions`, so once `variables.py` mentioned
        the predicate anywhere, every later consumer added to that file passed
        trivially -- presence-not-application, in the gate written to prevent exactly
        that. `_exclusion_meta` was added to that file and the gate could not see it.

        A function "reaches" the predicate if its own body applies it, or if it calls
        something in the same module that does. That indirection is real and has to be
        followed: `applicable_varsets` goes through `_rule_matches`,
        `workspaces_for_varset` through `_rule_refused`, and both `create_varset` and
        `update_varset` through `_validated_assignment_rule`. A gate that demanded a
        direct call would report four correct functions.
        """
        import ast
        import pathlib
        import re

        from terrapod.services import varset_self_join

        #: Functions that read an assignment rule without DECIDING anything about its
        #: refused dimensions. Each needs a reason, or this becomes the place a
        #: diverging consumer hides.
        not_deciders = {
            "_varset_json": "serialises the rule for the wire; decides nothing from it",
            "_refuse_unusable_exclude_entries": (
                "refuses an entry for a DIFFERENT property -- an entry that could never "
                "match -- and is called by `_validated_assignment_rule`, which does apply "
                "the predicate"
            ),
        }

        READS = re.compile(r"assignment_rule|assignment-rule|_rule_matches\(")
        APPLIED = {"rule_refused_dimensions"}
        root = pathlib.Path(varset_self_join.__file__).resolve().parents[1]

        def applies(node: ast.AST) -> bool:
            """Whether this function CALLS the predicate -- not merely names it.

            A substring match counts the `from ... import rule_refused_dimensions`
            line, so deleting the guard below it leaves the import and the gate
            passes. That is presence-not-application inside the gate written
            against presence-not-application, and a mutation found it (#2621).
            """
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Name) and f.id in APPLIED:
                        return True
                    if isinstance(f, ast.Attribute) and f.attr in APPLIED:
                        return True
                # Reading the denylist directly is applying it; an `import` of the
                # name is an ast.alias, never a Name load, so it cannot match here.
                if isinstance(n, ast.Name) and n.id == "RULE_DIMENSIONS_REFUSED":
                    return True
            return False

        offenders: list[str] = []
        examined = 0
        for path in sorted(root.rglob("*.py")):
            if path.name == "varset_self_join.py":
                continue
            src = path.read_text()
            if not READS.search(src):
                continue
            tree = ast.parse(src)
            funcs: dict[str, tuple[bool, set[str]]] = {}
            readers: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                seg = ast.get_source_segment(src, node) or ""
                called = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                funcs[node.name] = (applies(node), called)
                if READS.search(seg):
                    readers.add(node.name)

            # Transitive closure within the module: a caller of a function that
            # applies the predicate has applied it.
            reaching = {n for n, (direct, _) in funcs.items() if direct}
            changed = True
            while changed:
                changed = False
                for name, (_, called) in funcs.items():
                    if name not in reaching and called & reaching:
                        reaching.add(name)
                        changed = True

            for name in sorted(readers):
                examined += 1
                if name in reaching or name in not_deciders:
                    continue
                offenders.append(f"{path.name}::{name}")

        assert examined >= 9, (
            f"only {examined} rule-reading function(s) examined; the matcher stopped "
            "matching rather than the tree losing consumers"
        )
        assert not offenders, (
            "these read an assignment rule without applying the refused-dimension "
            "predicate, so they can disagree with the matcher about who receives a "
            "credential:\n  " + "\n  ".join(sorted(offenders))
        )
