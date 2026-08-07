"""Auto-apply mode on the two admin write paths (#1274 / #1276).

Both bulk-update and the autodiscovery rule template write TWO columns from
one input key, which is why neither goes through the ordinary field map. That
makes them the paths where the pair could silently drift apart — the exact
failure the mode's whole skew story is built to prevent — so they get their
own tests rather than riding on the single-workspace ones.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from terrapod.api.routers.autodiscovery_rules import _coerce_attrs
from terrapod.api.routers.workspace_bulk import _validate_auto_apply


class TestBulkUpdate:
    def test_mode_writes_both_columns(self):
        got = _validate_auto_apply({"auto-apply-mode": "create_update"}, {})
        assert got == {"auto_apply_mode": "create_update", "auto_apply": True}

    def test_never_clears_the_boolean(self):
        # The pair must stay consistent in BOTH directions, or a workspace
        # reading the projection would think it still auto-applies.
        got = _validate_auto_apply({"auto-apply-mode": "never"}, {})
        assert got == {"auto_apply_mode": "never", "auto_apply": False}

    @pytest.mark.parametrize(("boolean", "expected"), [(True, "always"), (False, "never")])
    def test_legacy_boolean_still_maps(self, boolean, expected):
        # An un-upgraded caller sending only the boolean keeps working.
        got = _validate_auto_apply({"auto-apply": boolean}, {"auto_apply": boolean})
        assert got == {"auto_apply_mode": expected}

    def test_both_keys_is_422_not_a_guess(self):
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply": True, "auto-apply-mode": "create"}, {})
        assert e.value.status_code == 422

    def test_unknown_mode_is_422(self):
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply-mode": "sometimes"}, {})
        assert e.value.status_code == 422

    def test_absent_touches_nothing(self):
        # A bulk update of unrelated fields must not write auto-apply at all,
        # or every such update would silently reset it.
        assert _validate_auto_apply({"terraform-version": "1.12"}, {}) == {}


class TestAutodiscoveryRuleTemplate:
    def test_mode_writes_both_columns(self):
        out = _coerce_attrs({"auto-apply-mode": "create"}, on_create=False)
        assert out["auto_apply_mode"] == "create"
        assert out["auto_apply"] is True

    def test_never_clears_the_boolean(self):
        out = _coerce_attrs({"auto-apply-mode": "never"}, on_create=False)
        assert out["auto_apply_mode"] == "never"
        assert out["auto_apply"] is False

    def test_legacy_boolean_still_maps(self):
        out = _coerce_attrs({"auto-apply": True}, on_create=False)
        assert out["auto_apply"] is True
        assert out["auto_apply_mode"] == "always"

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

    A JSON string sailed through as an *enable* — and once the columns were
    paired, wrote the string itself into a Boolean column while setting
    `auto_apply_mode` to "always". A caller who typed the value wrong got the
    opposite of what they asked for, on every matched workspace at once.
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
        assert got["auto_apply"] is val
        assert got["auto_apply_mode"] == mode


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
                {"auto_apply": True},
                [self._ws("safe", "merge_then_apply"), self._ws("risky", "apply_then_merge")],
            )
        assert e.value.status_code == 422
        # Names the offender — the operator has to know which to exclude.
        assert "risky" in str(e.value.detail)
        assert "safe" not in str(e.value.detail)

    def test_disabling_auto_apply_is_always_allowed(self):
        """Turning it OFF is the safe direction and must never be blocked."""
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        _reject_auto_apply_on_apply_then_merge(
            {"auto_apply": False}, [self._ws("risky", "apply_then_merge")]
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
            {"auto_apply": True}, [self._ws("a", "merge_then_apply"), self._ws("b", "")]
        )

    def test_a_long_offender_list_is_truncated_but_counted(self):
        """A filter matching hundreds must not produce an unreadable error —
        but the count has to be honest."""
        from terrapod.api.routers.workspace_bulk import (
            _reject_auto_apply_on_apply_then_merge,
        )

        many = [self._ws(f"ws-{i:03d}", "apply_then_merge") for i in range(25)]
        with pytest.raises(HTTPException) as e:
            _reject_auto_apply_on_apply_then_merge({"auto_apply": True}, many)
        detail = str(e.value.detail)
        assert "25 matched workspace(s)" in detail
        assert "and 15 more" in detail
