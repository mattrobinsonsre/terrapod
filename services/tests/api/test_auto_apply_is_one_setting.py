"""`auto_apply` is gone; `auto_apply_mode` is the whole setting (#2503).

The boolean was a denormalised projection of the mode, written as
`mode != "never"` at five sites and kept "so an un-upgraded client keeps
working". Two columns for one setting is what let `auto_apply = false` arrive
with `auto_apply_mode = "always"`, be resolved towards MORE automation than the
configuration asked for, and then fail after the workspace had already changed.

The TFE-compatible surface still speaks `auto-apply`, because that is `go-tfe`'s
own field name rather than ours to drop -- so the boolean is now INFERRED there
and absent from the native surface, the same split `native` vs `terraform` makes
for the variable category (#1898).

**Why this file exists rather than the attribute snapshot.** The key is
conditional on the surface now, and the snapshot captures ONE branch -- it
records the native shape, so it can no longer see that TFE emits the boolean at
all. Every compat guarantee below is therefore pinned here or nowhere.
"""

from unittest.mock import MagicMock

import pytest

from terrapod.api.routers import autodiscovery_rules as rules_mod
from terrapod.api.routers import runs as runs_mod
from terrapod.api.routers import tfe_v2 as tfe_mod
from terrapod.services.run_service import AUTO_APPLY_MODES, resolve_auto_apply_mode


def _ws(mode="never"):
    ws = MagicMock()
    ws.id = "11111111-1111-4111-8111-111111111111"
    ws.auto_apply_mode = mode
    return ws


def _attrs_ws(mode, *, tfe):
    return tfe_mod._workspace_json(_ws(mode), tfe=tfe)["data"]["attributes"]


def _attrs_run(mode, *, tfe):
    run = MagicMock()
    run.id = "22222222-2222-4222-8222-222222222222"
    run.auto_apply_mode = mode
    return runs_mod._run_json(run, engine="terraform", tfe=tfe)["data"]["attributes"]


class TestTheBooleanIsTFEOnly:
    """Present on the frozen surface, absent from ours -- on both documents."""

    @pytest.mark.parametrize("attrs_of", [_attrs_ws, _attrs_run])
    def test_the_frozen_surface_still_carries_it(self, attrs_of):
        assert "auto-apply" in attrs_of("always", tfe=True)

    @pytest.mark.parametrize("attrs_of", [_attrs_ws, _attrs_run])
    def test_the_native_surface_does_not(self, attrs_of):
        """`auto-apply-mode` is the setting's name on our own API."""
        assert "auto-apply" not in attrs_of("always", tfe=False)

    @pytest.mark.parametrize("attrs_of", [_attrs_ws, _attrs_run])
    @pytest.mark.parametrize("tfe", [True, False])
    def test_the_mode_is_on_both(self, attrs_of, tfe):
        assert attrs_of("create_update", tfe=tfe)["auto-apply-mode"] == "create_update"


class TestTheInference:
    """`go-tfe` must see exactly what the boolean always meant.

    Driven for EVERY mode rather than the two obvious ones: a conditional mode
    does auto-apply, so collapsing it to `False` would tell a `tofu` cloud
    backend that a workspace applies manually when it does not.
    """

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("never", False), ("always", True), ("create", True), ("create_update", True)],
    )
    @pytest.mark.parametrize("attrs_of", [_attrs_ws, _attrs_run])
    def test_the_boolean_is_the_mode_not_being_never(self, attrs_of, mode, expected):
        assert attrs_of(mode, tfe=True)["auto-apply"] is expected

    def test_every_mode_is_covered_by_the_case_above(self):
        """A floor, so a new mode cannot be added without deciding its boolean."""
        covered = {"never", "always", "create", "create_update"}
        assert set(AUTO_APPLY_MODES) == covered, (
            f"AUTO_APPLY_MODES is {AUTO_APPLY_MODES}; the inference test above "
            "enumerates the modes, so a new one needs a case there"
        )


class TestTheRuleDocumentDropsItOutright:
    """The autodiscovery rule is served on no TFE route, so there is no client."""

    def test_the_rule_never_carries_the_boolean(self):
        rule = MagicMock()
        rule.id = "33333333-3333-4333-8333-333333333333"
        rule.auto_apply_mode = "always"
        rule.oidc_audiences = {}
        rule.labels = {}
        # No `data` envelope: this helper returns the resource and the route
        # wraps it.
        doc = rules_mod._rule_json(rule)
        attrs = doc.get("attributes", doc.get("data", {}).get("attributes", {}))
        assert "auto-apply" not in attrs
        assert attrs["auto-apply-mode"] == "always"


class TestTheResolverKeepsTheSafetyProperty:
    """An unrecognised value reads as `never`, as it did with two columns.

    The two-column version was deliberately asymmetric so the worst a skewed
    write could do was auto-apply LESS than asked for. One column must not lose
    that: a value nothing recognises has to fail towards "a human looks at it".
    """

    @pytest.mark.parametrize("mode", list(AUTO_APPLY_MODES))
    def test_a_recognised_mode_is_returned_as_it_is(self, mode):
        assert resolve_auto_apply_mode(_ws(mode)) == mode

    @pytest.mark.parametrize("mode", ["", None, "ALWAYS", "sometimes", "true"])
    def test_anything_else_is_never(self, mode):
        assert resolve_auto_apply_mode(_ws(mode)) == "never"

    def test_an_object_with_no_mode_at_all_is_never(self):
        class Bare:
            pass

        assert resolve_auto_apply_mode(Bare()) == "never"
