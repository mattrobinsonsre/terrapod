"""Auto-apply mode on the two admin write paths (#1274 / #1276 / #2503).

Two INPUT spellings, one column: `auto-apply-mode` is the setting and
`auto-apply` is the boolean the compatibility surface still sends. Neither can
go through the ordinary field map, which maps one key to one column verbatim —
so these are the paths where the two spellings could come to mean different
things, and they get their own tests rather than riding on the single-workspace
ones.

The pair used to write two COLUMNS, and #2503 removed the boolean one: a
contradiction a caller could state was being resolved by the server, towards
more automation than was asked for. What is asserted here now is that the
boolean input still maps onto a mode, that it writes no second column, and
that sending both spellings is still a 422 rather than a guess.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from terrapod.api.routers.autodiscovery_rules import _coerce_attrs
from terrapod.api.routers.workspace_bulk import _validate_auto_apply


class TestBulkUpdate:
    @pytest.mark.parametrize("mode", ["never", "always", "create", "create_update"])
    def test_the_mode_writes_one_column_and_only_one(self, mode):
        # EXACT equality, in both directions: a second key here would be the
        # dropped column coming back, and the mode's own value must survive
        # verbatim rather than being flattened onto a boolean (#2503).
        assert _validate_auto_apply({"auto-apply-mode": mode}) == {"auto_apply_mode": mode}

    @pytest.mark.parametrize(("boolean", "expected"), [(True, "always"), (False, "never")])
    def test_the_superseded_boolean_still_maps(self, boolean, expected):
        # A caller written against the compatibility surface keeps working.
        got = _validate_auto_apply({"auto-apply": boolean})
        assert got == {"auto_apply_mode": expected}

    def test_both_keys_is_422_not_a_guess(self):
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply": True, "auto-apply-mode": "create"})
        assert e.value.status_code == 422

    def test_unknown_mode_is_422(self):
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply-mode": "sometimes"})
        assert e.value.status_code == 422

    def test_absent_touches_nothing(self):
        # A bulk update of unrelated fields must not write auto-apply at all,
        # or every such update would silently reset it.
        assert _validate_auto_apply({"terraform-version": "1.12"}) == {}


class TestAutodiscoveryRuleTemplate:
    @pytest.mark.parametrize("mode", ["never", "always", "create", "create_update"])
    def test_the_mode_writes_one_column_and_only_one(self, mode):
        out = _coerce_attrs({"auto-apply-mode": mode}, on_create=False)
        assert out["auto_apply_mode"] == mode
        assert "auto_apply" not in out

    def test_the_superseded_boolean_still_maps(self):
        out = _coerce_attrs({"auto-apply": True}, on_create=False)
        assert out["auto_apply_mode"] == "always"
        assert "auto_apply" not in out

    def test_both_keys_is_422(self):
        with pytest.raises(HTTPException) as e:
            _coerce_attrs({"auto-apply": False, "auto-apply-mode": "create"}, on_create=False)
        assert e.value.status_code == 422

    def test_unknown_mode_is_422(self):
        with pytest.raises(HTTPException) as e:
            _coerce_attrs({"auto-apply-mode": "occasionally"}, on_create=False)
        assert e.value.status_code == 422

    def test_absent_touches_nothing(self):
        out = _coerce_attrs({"name-prefix": "svc-"}, on_create=False)
        assert "auto_apply" not in out and "auto_apply_mode" not in out


class TestBulkUpdateTypeChecksTheBoolean:
    """`bool("false")` is True (#1301).

    A JSON string sailed through as an *enable*, so a caller who typed the
    value wrong got the opposite of what they asked for, on every matched
    workspace at once.

    Driven through `_validate_update_fields_for_test` -- the real scalar loop
    plus the validator chain -- rather than through `_validate_auto_apply`
    directly, because the check MOVED when the column went (#2503): it used to
    sit in the `_FIELD_MAP` loop, which no longer sees the key at all. A test
    calling the validator by hand would pass either way.
    """

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, "", None, []])
    def test_a_non_boolean_is_refused(self, bad):
        from terrapod.api.routers.workspace_bulk import _validate_update_fields_for_test

        with pytest.raises(HTTPException) as e:
            _validate_update_fields_for_test({"auto-apply": bad})
        assert e.value.status_code == 422
        assert "true or false" in str(e.value.detail)

    @pytest.mark.parametrize(("val", "mode"), [(True, "always"), (False, "never")])
    def test_real_booleans_still_work(self, val, mode):
        from terrapod.api.routers.workspace_bulk import _validate_update_fields_for_test

        got = _validate_update_fields_for_test({"auto-apply": val})
        assert got["auto_apply_mode"] == mode
        assert "auto_apply" not in got


class TestTheRuleTemplateTypeChecksTheBoolean:
    """The same `bool("false")` refusal as bulk update, on the rule template.

    This path wrote `bool(attrs["auto-apply"])` into a Boolean column before
    #2503, so a string meant `always` there too. With the column gone the
    coercion would be silent, so it is a refusal.
    """

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, "", None, []])
    def test_a_non_boolean_is_refused(self, bad):
        with pytest.raises(HTTPException) as e:
            _coerce_attrs({"auto-apply": bad}, on_create=False)
        assert e.value.status_code == 422
        assert "true or false" in str(e.value.detail)

    @pytest.mark.parametrize(("val", "mode"), [(True, "always"), (False, "never")])
    def test_real_booleans_still_map(self, val, mode):
        out = _coerce_attrs({"auto-apply": val}, on_create=False)
        assert out["auto_apply_mode"] == mode


class TestBulkUpdateApplyThenMergeCrossCheck:
    """The single-workspace PATCH has always refused auto-apply on an
    `apply_then_merge` workspace — under that workflow the apply runs BEFORE
    the PR merges, so auto-applying would apply changes from a branch nobody
    approved. Bulk-update had no such check, so the one path that can hit a
    hundred workspaces at once was the one that could set it (#1301)."""

    @staticmethod
    def _ws(name, workflow):
        w = MagicMock()
        w.name = name
        w.vcs_workflow = workflow
        return w

    def test_enabling_auto_apply_over_an_apply_then_merge_workspace_is_refused(self):
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        with pytest.raises(HTTPException) as e:
            _reject_auto_apply_on_apply_then_merge(
                {"auto_apply_mode": "always"},
                [self._ws("safe", "merge_then_apply"), self._ws("risky", "apply_then_merge")],
            )
        assert e.value.status_code == 422
        # Names the offender — the operator has to know which to exclude.
        assert "risky" in str(e.value.detail)
        assert "safe" not in str(e.value.detail)

    @pytest.mark.parametrize("mode", ["create", "create_update"])
    def test_a_conditional_mode_is_refused_too(self, mode):
        """`create` and `create_update` still auto-apply, so they are refused.

        A truthiness test over the old boolean caught them by accident; a mode
        has to be compared against `never` explicitly, and `"never"` is a
        truthy string -- so getting this wrong in either direction is easy and
        silent. Both halves are asserted: this one, and the `never` case below.
        """
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        with pytest.raises(HTTPException) as e:
            _reject_auto_apply_on_apply_then_merge(
                {"auto_apply_mode": mode}, [self._ws("risky", "apply_then_merge")]
            )
        assert e.value.status_code == 422

    def test_disabling_auto_apply_is_always_allowed(self):
        """Turning it OFF is the safe direction and must never be blocked."""
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        _reject_auto_apply_on_apply_then_merge(
            {"auto_apply_mode": "never"}, [self._ws("risky", "apply_then_merge")]
        )

    def test_an_update_that_does_not_touch_auto_apply_is_allowed(self):
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        _reject_auto_apply_on_apply_then_merge(
            {"terraform_version": "1.12"}, [self._ws("risky", "apply_then_merge")]
        )

    def test_no_offenders_means_no_refusal(self):
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        _reject_auto_apply_on_apply_then_merge(
            {"auto_apply_mode": "always"},
            [self._ws("a", "merge_then_apply"), self._ws("b", "")],
        )

    def test_a_long_offender_list_is_truncated_but_counted(self):
        """A filter matching hundreds must not produce an unreadable error —
        but the count has to be honest."""
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        many = [self._ws(f"ws-{i:03d}", "apply_then_merge") for i in range(25)]
        with pytest.raises(HTTPException) as e:
            _reject_auto_apply_on_apply_then_merge({"auto_apply_mode": "always"}, many)
        detail = str(e.value.detail)
        assert "25 matched workspace(s)" in detail
        assert "and 15 more" in detail
