"""Auto-apply mode on the two admin write paths (#1274 / #1276).

Both bulk-update and the autodiscovery rule template write TWO columns from
one input key, which is why neither goes through the ordinary field map. That
makes them the paths where the pair could silently drift apart — the exact
failure the mode's whole skew story is built to prevent — so they get their
own tests rather than riding on the single-workspace ones.
"""

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
