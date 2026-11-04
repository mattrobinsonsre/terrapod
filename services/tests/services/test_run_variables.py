"""Per-run `-var` values (#2377).

`tofu plan -var k=v` was accepted and dropped: the run planned against the
workspace's value, and the engine then told the practitioner to pass the flag
they had just passed. Refusing would have made that legible and left the
workflow equally broken, so it is implemented.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.variable_service import (
    RunVariableRejected,
    decoded_run_variables,
    resolve_variables,
    validated_run_variables,
)


def _row(key: str, value: str, *, category: str = "native", sensitive: bool = False) -> MagicMock:
    row = MagicMock()
    row.key = key
    row.value = value
    row.category = category
    row.sensitive = sensitive
    row.structured = False
    row.value_source = "static"
    row.apply_only = False
    row.configure_only = False
    return row


def _varset(*rows: MagicMock) -> MagicMock:
    vs = MagicMock()
    vs.variables = list(rows)
    return vs


RUN_VARS = [{"key": "region", "value": "from-the-cli"}]


@patch("terrapod.services.variable_service._get_applicable_varsets")
@patch("terrapod.services.variable_service.list_variables")
class TestRunVariablePrecedence:
    """The property is a RELATIONSHIP between four layers.

    A fixture carrying one of them passes whichever way the code is written, so
    each test instantiates the layer it is comparing against AND the run value.
    """

    async def test_a_run_variable_beats_a_workspace_variable(self, mock_list, mock_sets):
        mock_sets.side_effect = [[], []]
        mock_list.return_value = [_row("region", "from-the-workspace")]

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), run_variables=RUN_VARS
        )
        assert {r.key: r.value for r in out}["region"] == "from-the-cli"

    async def test_a_run_variable_beats_a_non_priority_variable_set(self, mock_list, mock_sets):
        mock_sets.side_effect = [[_varset(_row("region", "from-the-set"))], []]
        mock_list.return_value = []

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), run_variables=RUN_VARS
        )
        assert {r.key: r.value for r in out}["region"] == "from-the-cli"

    async def test_a_PRIORITY_variable_set_still_beats_a_run_variable(self, mock_list, mock_sets):
        """The guardrail survives.

        A priority set is how an operator pins a region or a role ARN, so a
        `-var` must not override it. `validated_run_variables` refuses the
        collision at creation, which is what stops this being a silent discard
        -- but a priority set created AFTER the run still wins here, and that is
        the operator's own later act on an already-queued run.
        """
        mock_sets.side_effect = [[], [_varset(_row("region", "pinned-by-the-operator"))]]
        mock_list.return_value = []

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), run_variables=RUN_VARS
        )
        assert {r.key: r.value for r in out}["region"] == "pinned-by-the-operator"

    async def test_precedence_is_still_per_category(self, mock_list, mock_sets):
        """A run variable must not reach across categories (#1898).

        Keying on `key` alone once made a `native` variable *remove* an `env`
        one of the same name from the run. A `-var` is a native value and has
        nothing to say about an env variable that happens to share its name.
        """
        mock_sets.side_effect = [[], []]
        mock_list.return_value = [_row("region", "env-value", category="env")]

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), run_variables=RUN_VARS
        )
        by_cat = {(r.category, r.key): r.value for r in out}
        assert by_cat[("env", "region")] == "env-value"
        assert by_cat[("native", "region")] == "from-the-cli"

    async def test_no_run_variables_changes_nothing(self, mock_list, mock_sets):
        mock_sets.side_effect = [[], []]
        mock_list.return_value = [_row("region", "from-the-workspace")]

        out = await resolve_variables(AsyncMock(spec=AsyncSession), uuid.uuid4())
        assert {r.key: r.value for r in out}["region"] == "from-the-workspace"

    async def test_a_run_variable_is_not_marked_sensitive_on_the_wire(self, mock_list, mock_sets):
        """Delivery stays unmarked, deliberately.

        On this list `sensitive` is a DELIVERY instruction, not a
        classification: `pulumi_config` turns it into `config set --secret`,
        which writes the value into the stack's own state -- somewhere a
        transient per-run override would outlive the run. Redaction does not
        ride on this flag; the summariser scrubs these values unconditionally.
        """
        mock_sets.side_effect = [[], []]
        mock_list.return_value = []

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), run_variables=RUN_VARS
        )
        entry = {r.key: r for r in out}["region"]
        assert entry.sensitive is False
        assert entry.category == "native"

    async def test_the_value_is_delivered_as_an_HCL_EXPRESSION(self, mock_list, mock_sets):
        """Measured, not assumed: `-var msg=OVERRIDE` arrives as '"OVERRIDE"'.

        OpenTofu's cloud backend sends the HCL SOURCE TEXT of the value, quotes
        included -- that is `go-tfe`'s `RunVariable` contract, and what lets
        `-var 'list=["a"]'` work at all. So the entry must be `structured`, or
        `render_tfvars` JSON-quotes it a second time and the run plans against a
        value WITH the quotes in it -- wrong, and it plans and applies cleanly,
        which is why only a live run found it.
        """
        mock_sets.side_effect = [[], []]
        mock_list.return_value = []

        out = await resolve_variables(
            AsyncMock(spec=AsyncSession),
            uuid.uuid4(),
            run_variables=[{"key": "msg", "value": '"OVERRIDE"'}],
        )
        entry = {r.key: r for r in out}["msg"]
        assert entry.structured is True, (
            "a run variable is an HCL expression; delivered unstructured it is "
            "quoted a second time and the run plans against the wrong value"
        )
        assert entry.value == '"OVERRIDE"'


class TestValidatedRunVariables:
    async def test_an_empty_list_is_a_no_op(self):
        """Not a refusal, and this is the one that would break every CLI plan.

        OpenTofu sends `variables` on EVERY `POST /runs`, as an empty array when
        no `-var` was given, so refusing on presence would 422 every plan in
        existence rather than only the ones asking for something.
        """
        db = AsyncMock(spec=AsyncSession)
        assert await validated_run_variables(db, uuid.uuid4(), []) == []
        assert await validated_run_variables(db, uuid.uuid4(), None) == []

    @pytest.mark.parametrize(
        ("entries", "message"),
        [
            ("region=x", "must be a list"),
            ([["region", "x"]], "{key, value} object"),
            ([{"value": "x"}], "non-empty string 'key'"),
            ([{"key": "   ", "value": "x"}], "non-empty string 'key'"),
            ([{"key": "region", "value": 7}], "must be a string"),
            ([{"key": "region", "value": "a"}, {"key": "region", "value": "b"}], "more than once"),
        ],
    )
    @patch("terrapod.services.variable_service.priority_varset_keys", return_value=set())
    async def test_bad_shapes_are_refused_by_reason(self, _pinned, entries, message):
        with pytest.raises(RunVariableRejected, match=message):
            await validated_run_variables(AsyncMock(spec=AsyncSession), uuid.uuid4(), entries)

    @patch("terrapod.services.variable_service.priority_varset_keys", return_value=set())
    async def test_a_good_entry_round_trips_byte_for_byte(self, _pinned):
        out = await validated_run_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), [{"key": " region ", "value": "  us-1  "}]
        )
        # The key is trimmed (a stray space is a typo); the VALUE is not, because
        # trailing whitespace can be load-bearing and altering what a caller sent
        # is how a provider gets an inconsistent-result error.
        assert out == [{"key": "region", "value": "  us-1  "}]

    @patch("terrapod.services.variable_service.priority_varset_keys")
    async def test_a_priority_set_collision_is_refused_naming_the_key(self, pinned):
        pinned.return_value = {"region"}
        with pytest.raises(RunVariableRejected, match="'region'"):
            await validated_run_variables(
                AsyncMock(spec=AsyncSession), uuid.uuid4(), [{"key": "region", "value": "x"}]
            )

    @patch("terrapod.services.variable_service.priority_varset_keys")
    async def test_a_key_no_priority_set_pins_is_accepted(self, pinned):
        pinned.return_value = {"something_else"}
        out = await validated_run_variables(
            AsyncMock(spec=AsyncSession), uuid.uuid4(), [{"key": "region", "value": "x"}]
        )
        assert out == [{"key": "region", "value": "x"}]


class TestDecodedRunVariables:
    """Never raises: two consumers read this for opposite reasons."""

    def test_round_trip(self):
        assert decoded_run_variables(json.dumps(RUN_VARS)) == RUN_VARS

    @pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"key": "x"}', "[1, 2]"])
    def test_anything_unreadable_is_empty_not_an_exception(self, raw):
        # Safe in BOTH directions: deliver nothing, redact nothing. A raise here
        # would fail a run on the delivery side and suppress a summary on the
        # redaction side.
        assert decoded_run_variables(raw) == []

    def test_a_malformed_entry_is_dropped_rather_than_half_read(self):
        raw = json.dumps([{"key": "ok", "value": "v"}, {"key": "no-value"}, {"value": "no-key"}])
        assert decoded_run_variables(raw) == [{"key": "ok", "value": "v"}]
