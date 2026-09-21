"""Secrets do not reach the model endpoint (GHSA-5mpc-79pv-6mq7).

The summariser is the one path that hands a run's artifacts to a third party, so
it is the one path that redacts. The stored plan JSON and logs stay whole — see
the module docstring for why redacting those instead would break drift detection.
"""

import json

from terrapod.services import summary_redaction as sr
from terrapod.services.summariser import _clean_plan_json_bytes


def _plan_with_secrets() -> bytes:
    return json.dumps(
        {
            "resource_changes": [
                {
                    "address": "aws_db_instance.main",
                    "change": {
                        "actions": ["update"],
                        "before": {"password": "OLD-SECRET-aaaa", "name": "db"},
                        "after": {"password": "SUPERSECRET-bbbb", "name": "db"},
                        "before_sensitive": {"password": True},
                        "after_sensitive": {"password": True},
                    },
                }
            ],
            "output_changes": {
                "db_url": {
                    "actions": ["create"],
                    "after": "postgres://u:HUNTER2SECRET@h/d",
                    "after_sensitive": True,
                }
            },
            "prior_state": {"values": {"root_module": {}}},
        }
    ).encode()


class TestTheFindingItself:
    """Stripping `prior_state` does NOT keep sensitive values out — that was the
    premise of the guard this replaces, and it is measurably false."""

    def test_cleaning_alone_still_leaks_the_changing_value(self):
        cleaned = _clean_plan_json_bytes(_plan_with_secrets()).decode()
        assert "prior_state" not in cleaned
        # The leak: a resource that is changing keeps its value in the clear.
        assert "SUPERSECRET-bbbb" in cleaned

    def test_redaction_closes_it(self):
        out = sr.redact_plan_json(_clean_plan_json_bytes(_plan_with_secrets())).decode()
        for secret in ("SUPERSECRET-bbbb", "OLD-SECRET-aaaa", "HUNTER2SECRET"):
            assert secret not in out, secret
        assert sr.PLACEHOLDER in out


class TestRedactionKeepsTheSummaryWorthReading:
    def test_non_sensitive_values_survive(self):
        out = json.loads(sr.redact_plan_json(_plan_with_secrets()))
        change = out["resource_changes"][0]["change"]
        assert change["after"]["name"] == "db"
        assert change["actions"] == ["update"]

    def test_an_unmarked_resource_is_untouched(self):
        raw = json.dumps(
            {
                "resource_changes": [
                    {"change": {"actions": ["create"], "after": {"bucket": "my-logs"}}}
                ]
            }
        ).encode()
        assert json.loads(sr.redact_plan_json(raw))["resource_changes"][0]["change"]["after"] == {
            "bucket": "my-logs"
        }


class TestMarkerShapes:
    """Terraform's markers mirror the value, so every shape must be handled."""

    def test_whole_node_marked(self):
        assert sr._redact_by_markers({"a": "x"}, True) == sr.PLACEHOLDER

    def test_nested_dict(self):
        out = sr._redact_by_markers({"a": {"b": "secret", "c": "keep"}}, {"a": {"b": True}})
        assert out == {"a": {"b": sr.PLACEHOLDER, "c": "keep"}}

    def test_list_by_position(self):
        out = sr._redact_by_markers(["keep", "secret"], [False, True])
        assert out == ["keep", sr.PLACEHOLDER]

    def test_a_shorter_marker_list_does_not_raise(self):
        # Defensive: the two structures come from the same producer, but an
        # IndexError here would fail the whole summary.
        assert sr._redact_by_markers(["a", "b", "c"], [True]) == [sr.PLACEHOLDER, "b", "c"]

    def test_state_shaped_sensitive_values_are_handled(self):
        raw = json.dumps(
            {
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "values": {"token": "STATE-SECRET-xyz", "id": "i-1"},
                                "sensitive_values": {"token": True},
                            }
                        ]
                    }
                }
            }
        ).encode()
        out = sr.redact_plan_json(raw).decode()
        assert "STATE-SECRET-xyz" not in out
        assert "i-1" in out


class TestItNeverFailsTheCall:
    def test_unparseable_input_is_returned_unchanged(self):
        assert sr.redact_plan_json(b"not json") == b"not json"

    def test_a_non_dict_payload_is_returned_unchanged(self):
        assert sr.redact_plan_json(b"[1,2,3]") == b"[1,2,3]"

    def test_invalid_utf8_is_returned_unchanged(self):
        assert sr.redact_plan_json(b"\xff\xfe") == b"\xff\xfe"


class TestLiteralRedactionForUnstructuredText:
    """Logs and .tf/.tfvars source carry no markers — only the values match."""

    def test_a_secret_is_removed_from_a_log(self):
        log = "Error: authentication failed for token hunter2-long-secret\n"
        assert "hunter2-long-secret" not in sr.redact_text(log, ["hunter2-long-secret"])

    def test_every_occurrence_goes(self):
        out = sr.redact_text("a SEKRIT-VALUE-1 b SEKRIT-VALUE-1 c", ["SEKRIT-VALUE-1"])
        assert "SEKRIT-VALUE-1" not in out
        assert out.count(sr.PLACEHOLDER) == 2

    def test_short_values_are_deliberately_left_alone(self):
        # Redacting "prod" would black out the summary and teach the operator
        # to stop reading it. This is a documented trade, not an oversight.
        text = "deploying to prod with replicas=1"
        assert sr.redact_text(text, ["prod", "1", "true"]) == text

    def test_the_longest_secret_wins_when_one_contains_another(self):
        # Replacing the short one first would leave the long one's tail beside
        # a placeholder — exposed, but looking handled.
        out = sr.redact_text("value=abcdefgh-TAIL", ["abcdefgh", "abcdefgh-TAIL"])
        assert "TAIL" not in out
        assert out == f"value={sr.PLACEHOLDER}"

    def test_empty_text_and_empty_secrets_are_safe(self):
        assert sr.redact_text("", ["abcdefghij"]) == ""
        assert sr.redact_text("hello", []) == "hello"
        assert sr.redact_text("hello", ["", None or ""]) == "hello"

    def test_collect_literals_drops_the_unusable(self):
        assert sr.collect_literals(["", "short", "long-enough-value"]) == ["long-enough-value"]


class TestTheChokepointRedactsEveryArtifact:
    """End-to-end over `_gather_inputs`, because the bug was never in the
    redactor — it was in which artifacts got redacted."""

    async def _gather(self, *, kind, artifact, sensitive_value):
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services import summariser
        from terrapod.services.variable_service import ResolvedVariable

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None
        run.apply_started_at = None

        storage = AsyncMock()
        storage.get = AsyncMock(return_value=artifact)

        resolved = [
            ResolvedVariable(
                key="db_password",
                value=sensitive_value,
                category="terraform",
                structured=False,
                sensitive=True,
            ),
            ResolvedVariable(
                key="region",
                value="eu-west-1-not-secret",
                category="terraform",
                structured=False,
                sensitive=False,
            ),
        ]
        with (
            patch.object(summariser, "get_storage", return_value=storage),
            patch(
                "terrapod.services.variable_service.resolve_variables",
                new=AsyncMock(return_value=resolved),
            ),
        ):
            return await summariser._gather_inputs(AsyncMock(), run, kind)

    async def test_the_plan_json_reaches_the_prompt_redacted(self):
        primary, *_ = await self._gather(
            kind="plan_summary",
            artifact=_plan_with_secrets(),
            sensitive_value="unused-but-long-enough",
        )
        assert "SUPERSECRET-bbbb" not in primary
        assert sr.PLACEHOLDER in primary

    async def test_a_failure_log_is_redacted_by_literal_value(self):
        # The half structural redaction cannot reach: a log is just text.
        secret = "pa55word-from-the-variable"
        log = f"Error: could not connect using {secret}\n".encode()
        primary, label, *_ = await self._gather(
            kind="failure_analysis", artifact=log, sensitive_value=secret
        )
        assert label == "PLAN_LOG"
        assert secret not in primary
        assert sr.PLACEHOLDER in primary

    async def test_a_non_sensitive_variable_value_is_left_in_the_log(self):
        log = b"Error: deploying to eu-west-1-not-secret failed\n"
        primary, *_ = await self._gather(
            kind="failure_analysis", artifact=log, sensitive_value="some-other-long-secret"
        )
        assert "eu-west-1-not-secret" in primary

    async def test_a_variable_resolution_failure_does_not_stop_the_summary(self):
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services import summariser

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None
        run.apply_started_at = None
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=_plan_with_secrets())
        with (
            patch.object(summariser, "get_storage", return_value=storage),
            patch(
                "terrapod.services.variable_service.resolve_variables",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
        ):
            primary, *_ = await summariser._gather_inputs(AsyncMock(), run, "plan_summary")
        # Degrades to the structural pass, which still covers the plan JSON.
        assert "SUPERSECRET-bbbb" not in primary


class TestDerivedSecretsWithNoMarkerOfTheirOwn:
    """Markers alone do not redact safely — the Go MCP redactor found this
    first and the same hole existed here."""

    def _plan_with_copy(self) -> bytes:
        return json.dumps(
            {
                "resource_changes": [
                    {
                        "address": "random_password.db",
                        "change": {
                            "actions": ["create"],
                            "after": {"result": "DERIVED-SECRET-dddd"},
                            "after_sensitive": {"result": True},
                        },
                    },
                    {
                        # terraform_data copies a sensitive input to its output
                        # and the output carries NO marker.
                        "address": "terraform_data.copy",
                        "change": {
                            "actions": ["create"],
                            "after": {"input": "DERIVED-SECRET-dddd"},
                            "after_sensitive": {},
                        },
                    },
                ]
            }
        ).encode()

    def test_the_marked_value_is_collected(self):
        assert "DERIVED-SECRET-dddd" in sr.marked_values(self._plan_with_copy())

    def test_structural_redaction_alone_misses_the_copy(self):
        # Exactly why value-matching is layered on top: the copy has no marker,
        # so a position-based pass cannot see it.
        out = sr.redact_plan_json(self._plan_with_copy()).decode()
        assert "DERIVED-SECRET-dddd" in out

    def test_value_matching_catches_it(self):
        raw = self._plan_with_copy()
        out = sr.redact_text(sr.redact_plan_json(raw).decode(), sr.marked_values(raw))
        assert "DERIVED-SECRET-dddd" not in out

    def test_numbers_and_booleans_are_not_collected(self):
        # Matching them would redact unrelated parts of the plan: True and 0
        # appear everywhere, and as secrets they carry almost nothing.
        raw = json.dumps(
            {
                "resource_changes": [
                    {
                        "change": {
                            "actions": ["create"],
                            "after": {"port": 5432, "enabled": True, "tok": "LONG-SECRET-eeee"},
                            "after_sensitive": {"port": True, "enabled": True, "tok": True},
                        }
                    }
                ]
            }
        ).encode()
        collected = sr.marked_values(raw)
        assert collected == ["LONG-SECRET-eeee"]

    def test_malformed_input_yields_no_values(self):
        assert sr.marked_values(b"not json") == []
        assert sr.marked_values(b"[1,2,3]") == []


class TestTheChokepointAppliesDerivedValues:
    async def test_an_unmarked_copy_does_not_reach_the_prompt(self):
        """The wiring, not just the function.

        Testing `marked_values` alone passes while `_gather_inputs` ignores it —
        which is how the audit-redaction guard in this same advisory set slipped
        through, so it is asserted end to end here.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services import summariser

        plan = json.dumps(
            {
                "resource_changes": [
                    {
                        "address": "random_password.db",
                        "change": {
                            "actions": ["create"],
                            "after": {"result": "CHOKEPOINT-SECRET-ffff"},
                            "after_sensitive": {"result": True},
                        },
                    },
                    {
                        "address": "terraform_data.copy",
                        "change": {
                            "actions": ["create"],
                            "after": {"input": "CHOKEPOINT-SECRET-ffff"},
                            "after_sensitive": {},
                        },
                    },
                ]
            }
        ).encode()

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None
        run.apply_started_at = None
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=plan)
        with (
            patch.object(summariser, "get_storage", return_value=storage),
            patch(
                "terrapod.services.variable_service.resolve_variables",
                new=AsyncMock(return_value=[]),
            ),
        ):
            primary, *_ = await summariser._gather_inputs(AsyncMock(), run, "plan_summary")
        assert "CHOKEPOINT-SECRET-ffff" not in primary


class TestSensitiveRootVariables:
    """The fourth signal `5mpc` names, and the one I missed first time.

    A root variable declared `sensitive = true` has its value in
    `variables[name].value` and its declaration in
    `configuration.root_module.variables[name]`. No marker in any change block
    points at it, so a marker walk cannot see it — and a resource consuming it
    whose provider does not mark the attribute keeps it in the clear.
    """

    def _plan(self) -> bytes:
        return json.dumps(
            {
                "variables": {"db_password": {"value": "ROOTVAR-SECRET-xyz"}},
                "configuration": {
                    "root_module": {"variables": {"db_password": {"sensitive": True}}}
                },
                "resource_changes": [
                    {
                        "address": "aws_db_instance.main",
                        "change": {
                            "actions": ["update"],
                            # The provider does NOT mark it here.
                            "after": {"password": "ROOTVAR-SECRET-xyz", "name": "db"},
                            "after_sensitive": {},
                        },
                    }
                ],
            }
        ).encode()

    def test_the_value_is_collected(self):
        assert "ROOTVAR-SECRET-xyz" in sr.marked_values(self._plan())

    def test_a_non_sensitive_root_variable_is_not_collected(self):
        raw = json.dumps(
            {
                "variables": {"region": {"value": "eu-west-1-not-secret"}},
                "configuration": {"root_module": {"variables": {"region": {"sensitive": False}}}},
            }
        ).encode()
        assert sr.marked_values(raw) == []

    def test_a_malformed_configuration_block_is_survivable(self):
        for raw in (
            b'{"variables": {"a": {"value": "x"}}}',
            b'{"configuration": "not a dict", "variables": {}}',
            b'{"configuration": {"root_module": {}}, "variables": {"a": 1}}',
        ):
            assert sr.marked_values(raw) == []

    async def test_it_does_not_reach_the_prompt(self):
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services import summariser

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None
        run.apply_started_at = None
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=self._plan())
        with (
            patch.object(summariser, "get_storage", return_value=storage),
            patch(
                "terrapod.services.variable_service.resolve_variables",
                new=AsyncMock(return_value=[]),
            ),
        ):
            primary, *_ = await summariser._gather_inputs(AsyncMock(), run, "plan_summary")
        assert "ROOTVAR-SECRET-xyz" not in primary
