"""The typed reader behind the TFE state-version-output routes (#2376).

Every fixture below is the shape a REAL `tofu apply` wrote, copied out of a
state file rather than invented. That matters: two of the obvious guesses about
state v4 are wrong, and a test written from the guess would have passed while
the route handed the CLI something it cannot parse.
"""

from __future__ import annotations

import pytest

from terrapod.services.state_graph_service import (
    coarse_output_type,
    terraform_outputs,
    typed_terraform_outputs,
)

#: Verbatim from a state file written by OpenTofu v1.13.1. Note what is NOT
#: here: `sensitive` is absent on every output except the sensitive one.
REAL_STATE = {
    "version": 4,
    "outputs": {
        "a_string": {"value": "hello", "type": "string"},
        "a_number": {"value": 42, "type": "number"},
        "a_bool": {"value": True, "type": "bool"},
        "a_list": {"value": ["a", "b"], "type": ["tuple", ["string", "string"]]},
        "a_map": {"value": {"k": "v"}, "type": ["object", {"k": "string"}]},
        "an_object": {
            "value": {"name": "n", "count": 3},
            "type": ["object", {"count": "number", "name": "string"}],
        },
        "a_secret": {"value": "shh", "type": "string", "sensitive": True},
    },
}


class TestItReadsWhatStateActuallyRecords:
    def test_sensitive_is_absent_not_false_for_a_normal_output(self):
        """The fact that would have raised a KeyError if assumed present.

        State v4 omits `sensitive` entirely unless the output is sensitive --
        measured, not inferred from the docs.
        """
        assert "sensitive" not in REAL_STATE["outputs"]["a_string"]
        by_name = {o.name: o for o in typed_terraform_outputs(REAL_STATE)}
        assert by_name["a_string"].sensitive is False
        assert by_name["a_secret"].sensitive is True

    def test_the_value_is_unmasked(self):
        """The whole reason this exists beside `terraform_outputs`."""
        by_name = {o.name: o for o in typed_terraform_outputs(REAL_STATE)}
        assert by_name["a_secret"].value == "shh"
        # ...where the masking reader says the opposite, and must keep doing so.
        assert terraform_outputs(REAL_STATE)["a_secret"] == "(sensitive)"

    def test_the_detailed_type_is_passed_through_verbatim(self):
        """`detailed-type` is the cty type; the CLI parses it as one.

        Reshaping it would be inventing a type the engine never wrote.
        """
        by_name = {o.name: o for o in typed_terraform_outputs(REAL_STATE)}
        assert by_name["a_list"].detailed_type == ["tuple", ["string", "string"]]
        assert by_name["an_object"].detailed_type == [
            "object",
            {"count": "number", "name": "string"},
        ]

    def test_every_output_has_a_detailed_type(self):
        """A nil on ANY output makes the CLI abandon the whole path.

        `GetRootOutputValues` falls back to reading full state the moment one is
        nil, so a reader that dropped the type for one output would silently
        change the behaviour of all of them.
        """
        assert all(o.detailed_type is not None for o in typed_terraform_outputs(REAL_STATE))

    def test_outputs_come_back_in_a_stable_order(self):
        names = [o.name for o in typed_terraform_outputs(REAL_STATE)]
        assert names == sorted(names)


class TestTheCoarseType:
    @pytest.mark.parametrize(
        ("detailed", "expected"),
        [
            # Primitives name themselves.
            ("string", "string"),
            ("number", "number"),
            ("bool", "bool"),
            # The two that a guess gets wrong: an HCL list literal is a TUPLE
            # and a map literal is an OBJECT, so both heads must fold.
            (["tuple", ["string", "string"]], "array"),
            (["object", {"k": "string"}], "object"),
            # ...and the explicitly-declared forms still occur.
            (["list", "string"], "array"),
            (["set", "string"], "array"),
            (["map", "string"], "object"),
        ],
    )
    def test_each_head_folds_to_go_tfes_name(self, detailed, expected):
        assert coarse_output_type(detailed) == expected

    def test_something_unnameable_is_empty_rather_than_a_guess(self):
        for bad in (None, 42, [], [7], {}):
            assert coarse_output_type(bad) == ""


class TestDegradation:
    def test_a_state_with_no_outputs_is_empty_not_an_error(self):
        assert typed_terraform_outputs({"version": 4, "outputs": {}}) == []
        assert typed_terraform_outputs({"version": 4}) == []
        assert typed_terraform_outputs({"outputs": "nonsense"}) == []

    def test_an_output_of_an_unexpected_shape_is_reported_not_dropped(self):
        """Dropping it would read as "no such output" to someone looking at the
        state file, which is a worse answer than an untyped one."""
        out = typed_terraform_outputs({"outputs": {"odd": "bare-string"}})
        assert len(out) == 1
        assert out[0].name == "odd"
        assert out[0].value == "bare-string"
        assert out[0].detailed_type is None
