"""Auto-apply on the two admin write paths (#1274 / #1276, narrowed by #2503).

These two paths are here because neither goes through `_FIELD_MAP`. That used
to be because one input key wrote TWO columns, and this file existed to prove
the pair could not drift apart. **There is no pair any more** — `auto_apply` is
gone and `auto_apply_mode` is the whole setting — so the invariant those tests
guarded cannot be violated and the tests asserting it have been replaced rather
than repaired. What is still worth pinning on exactly these two paths:

  - one key writes exactly one column, and the column is the mode;
  - the retired boolean is **refused**, not ignored and not quietly mapped;
  - an absent key touches nothing, because a bulk update of unrelated fields
    silently resetting auto-apply across a fleet is the failure this path is
    most exposed to;
  - the `apply_then_merge` cross-check still reads the RESULTING mode.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from terrapod.api.routers.autodiscovery_rules import _coerce_attrs
from terrapod.api.routers.workspace_bulk import _validate_auto_apply


class TestBulkUpdate:
    def test_the_mode_writes_the_one_column(self):
        assert _validate_auto_apply({"auto-apply-mode": "create_update"}, {}) == {
            "auto_apply_mode": "create_update"
        }

    def test_never_is_a_value_like_any_other(self):
        # It used to have to clear a sibling boolean as well. Now it is one
        # write, and this asserts there is no second column coming back.
        assert _validate_auto_apply({"auto-apply-mode": "never"}, {}) == {
            "auto_apply_mode": "never"
        }

    @pytest.mark.parametrize("boolean", [True, False])
    def test_the_retired_boolean_is_refused_not_mapped(self, boolean):
        """Refused rather than silently honoured or silently dropped.

        A bulk update reaches a hundred workspaces at once, so accepting a key
        this surface no longer honours is how a caller finds out by discovering
        their fleet in a state they did not ask for. The message names the
        replacement and both of the old boolean's values.
        """
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply": boolean}, {})
        assert e.value.status_code == 422
        detail = str(e.value.detail)
        assert "auto-apply-mode" in detail
        assert "always" in detail and "never" in detail

    def test_a_non_boolean_is_refused_too(self):
        """`bool("false")` is True (#1301), and that trap is closed by the key
        being refused outright rather than by type-checking its value."""
        with pytest.raises(HTTPException) as e:
            _validate_auto_apply({"auto-apply": "false"}, {})
        assert e.value.status_code == 422

    def test_both_keys_is_still_refused(self):
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
    def test_the_mode_writes_the_one_column(self):
        out = _coerce_attrs({"auto-apply-mode": "create"}, on_create=False)
        assert out["auto_apply_mode"] == "create"
        assert "auto_apply" not in out

    def test_never_is_a_value_like_any_other(self):
        out = _coerce_attrs({"auto-apply-mode": "never"}, on_create=False)
        assert out["auto_apply_mode"] == "never"
        assert "auto_apply" not in out

    @pytest.mark.parametrize("boolean", [True, False])
    def test_the_retired_boolean_is_refused_not_mapped(self, boolean):
        """A rule TEMPLATES future workspaces, so a silently accepted key would
        materialise a setting the caller cannot see in the rule they wrote."""
        with pytest.raises(HTTPException) as e:
            _coerce_attrs({"auto-apply": boolean}, on_create=False)
        assert e.value.status_code == 422
        assert "auto-apply-mode" in str(e.value.detail)

    def test_both_keys_is_still_refused(self):
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


class TestTheTfeSurfaceIsUnaffected:
    """The boolean is dropped from the NATIVE surfaces only.

    `/api/tfe/v2` keeps speaking `auto-apply` because that is `go-tfe`'s own
    field name rather than ours to drop, so a `tofu` cloud backend and `tfci`
    see no change. Asserted here, beside the refusals, because the two
    behaviours only make sense as a pair -- reading either one alone invites
    "surely this should be consistent", and the inconsistency is the point.
    """

    def test_the_tfe_mapper_still_accepts_the_boolean(self):
        from terrapod.services import run_service

        ws = MagicMock()
        ws.auto_apply_mode = "always"
        assert run_service.resolve_auto_apply_mode(ws) != "never"

        ws.auto_apply_mode = "never"
        assert run_service.resolve_auto_apply_mode(ws) == "never"


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

    # The cross-check reads the RESULTING MODE rather than a boolean (#2503).
    # `never` is the off value; anything else means the workspace will apply on
    # its own, which is the thing `apply_then_merge` cannot tolerate.
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
        """The old boolean could only say on or off. A conditional mode still
        applies without a human, so the same refusal has to cover it — reading
        the mode as a boolean would have let these two through."""
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
