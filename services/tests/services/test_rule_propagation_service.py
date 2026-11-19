"""Gate for which autodiscovery-rule fields propagate to existing workspaces (#2549)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers import workspace_bulk
from terrapod.db.models import AuditLog, AutodiscoveryRule, RunTask
from terrapod.services.rule_propagation_service import (
    NOT_PROPAGATED,
    PROPAGATED_FIELDS,
    FieldVerdict,
    PropagationResult,
    classify,
    is_changed,
    propagate_rule_update,
)

#: Rule PATCH spellings that are not columns but write one, so they map like a column.
ALIASES = frozenset({"terraform-version", "auto-apply"})

#: Bulk-update keys that `_validate_update` accepts outside `_FIELD_MAP`.
_SPECIAL_BULK_KEYS = frozenset({"agent-pool-ids", "run-tasks", "notification-configurations"})


def rule_attributes() -> set[str]:
    """Every rule column in the API's kebab-case, so a new column must be classified."""
    return {c.key.replace("_", "-") for c in AutodiscoveryRule.__table__.columns}


def classification_problems(
    names: set[str],
    propagated: dict[str, str],
    not_propagated: dict[str, str],
) -> list[str]:
    problems = [
        f"{n}: not classified" for n in sorted(names - propagated.keys() - not_propagated.keys())
    ]
    problems += [f"{n}: in both maps" for n in sorted(propagated.keys() & not_propagated.keys())]
    unknown = (propagated.keys() | not_propagated.keys()) - names - ALIASES
    problems += [f"{n}: not a rule attribute" for n in sorted(unknown)]
    problems += [
        f"{n}: alias cannot be unpropagated" for n in sorted(ALIASES & not_propagated.keys())
    ]
    return problems


class TestEveryRuleSettingIsClassified:
    def test_every_rule_attribute_is_in_exactly_one_map(self):
        problems = classification_problems(rule_attributes(), PROPAGATED_FIELDS, NOT_PROPAGATED)
        assert not problems, (
            f"{problems} — classify each AutodiscoveryRule attribute in "
            "PROPAGATED_FIELDS (with its bulk-update key) or NOT_PROPAGATED (with the reason)."
        )

    def test_a_new_rule_setting_without_a_classification_fails_the_gate(self):
        names = rule_attributes() | {"brand-new-setting"}
        problems = classification_problems(names, PROPAGATED_FIELDS, NOT_PROPAGATED)
        assert problems == ["brand-new-setting: not classified"]

    def test_a_setting_in_both_maps_fails_the_gate(self):
        problems = classification_problems(
            rule_attributes(), PROPAGATED_FIELDS, {**NOT_PROPAGATED, "labels": "because"}
        )
        assert problems == ["labels: in both maps"]

    def test_a_stale_name_fails_the_gate(self):
        problems = classification_problems(
            rule_attributes(), {**PROPAGATED_FIELDS, "gone-setting": "labels"}, NOT_PROPAGATED
        )
        assert problems == ["gone-setting: not a rule attribute"]

    def test_the_aliases_resolve_to_their_canonical_attribute(self):
        assert PROPAGATED_FIELDS["terraform-version"] == PROPAGATED_FIELDS["engine-version"]
        assert PROPAGATED_FIELDS["auto-apply"] == PROPAGATED_FIELDS["auto-apply-mode"]


class TestPropagatedFieldsAreAcceptedByBulkUpdate:
    def test_every_target_is_a_key_bulk_update_accepts(self):
        accepted = (
            set(workspace_bulk._FIELD_MAP)
            | set(workspace_bulk._FIELDS_HANDLED_SEPARATELY)
            | _SPECIAL_BULK_KEYS
        )
        unknown = {k: v for k, v in PROPAGATED_FIELDS.items() if v not in accepted}
        assert not unknown, f"PROPAGATED_FIELDS targets bulk-update does not accept: {unknown}"

    def test_the_renamed_fields_map_to_their_bulk_keys(self):
        assert PROPAGATED_FIELDS["run-task-templates"] == "run-tasks"
        assert PROPAGATED_FIELDS["notification-templates"] == "notification-configurations"
        assert PROPAGATED_FIELDS["agent-pool-id"] == "agent-pool-ids"
        assert PROPAGATED_FIELDS["terraform-version"] == "engine-version"


class TestNotPropagated:
    def test_every_reason_is_a_non_empty_string(self):
        bad = [k for k, v in NOT_PROPAGATED.items() if not isinstance(v, str) or not v.strip()]
        assert not bad, f"NOT_PROPAGATED entries without a reason: {bad}"

    def test_owner_hooks_engine_and_storage_are_never_propagated(self):
        for name in ("owner-email", "execution-hook-templates", "engine", "storage-location"):
            assert name in NOT_PROPAGATED
            assert name not in PROPAGATED_FIELDS


def make_ws(pools: list[uuid.UUID] | None = None, **columns: Any) -> SimpleNamespace:
    links = [SimpleNamespace(agent_pool_id=p) for p in pools or []]
    return SimpleNamespace(agent_pool_links=links, **columns)


def verdicts(ws: Any, previous: dict, new: dict, templates: dict | None = None) -> dict[str, str]:
    return {v.field: v.verdict for v in classify(ws, previous, new, templates)}


NOTIFICATION = {
    "name": "n",
    "destination_type": "slack",
    "url": "https://s",
    "token": None,
    "triggers": ["run:errored"],
    "email_addresses": [],
    "enabled": True,
}


def task(**overrides: Any) -> dict[str, Any]:
    spec = {
        "name": "scan",
        "url": "https://hooks.example/a",
        "hmac_key": None,
        "stage": "pre_plan",
        "enforcement_level": "mandatory",
        "enabled": True,
    }
    return {**spec, **overrides}


class TestClassifyScalars:
    def test_a_value_equal_to_the_previous_rule_value_is_applied(self):
        ws = make_ws(resource_cpu="1")
        [v] = classify(ws, {"resource-cpu": "1"}, {"resource-cpu": "2"})
        assert v == FieldVerdict("resource-cpu", "1", "1", "2", "apply")

    def test_a_rule_edit_that_leaves_the_value_alone_is_unchanged(self):
        ws = make_ws(resource_cpu="2")
        [v] = classify(ws, {"resource-cpu": "2"}, {"resource-cpu": "2"})
        assert v.verdict == "unchanged"

    def test_a_value_equal_to_the_new_rule_value_is_unchanged(self):
        ws = make_ws(resource_cpu="2")
        [v] = classify(ws, {"resource-cpu": "1"}, {"resource-cpu": "2"})
        assert v == FieldVerdict("resource-cpu", "2", "1", "2", "unchanged")

    def test_any_other_value_is_customised(self):
        ws = make_ws(resource_cpu="4")
        [v] = classify(ws, {"resource-cpu": "1"}, {"resource-cpu": "2"})
        assert v == FieldVerdict("resource-cpu", "4", "1", "2", "customised")

    def test_each_field_is_judged_on_its_own_on_one_workspace(self):
        ws = make_ws(resource_cpu="1", resource_memory="2Gi", parallelism=20)
        got = verdicts(
            ws,
            {"resource-cpu": "1", "resource-memory": "1Gi", "parallelism": 10},
            {"resource-cpu": "2", "resource-memory": "2Gi", "parallelism": 5},
        )
        assert got == {
            "resource-cpu": "apply",
            "resource-memory": "unchanged",
            "parallelism": "customised",
        }

    def test_a_field_not_in_new_is_not_classified(self):
        ws = make_ws(resource_cpu="1", resource_memory="2Gi")
        got = verdicts(ws, {"resource-cpu": "1", "resource-memory": "2Gi"}, {"resource-cpu": "2"})
        assert list(got) == ["resource-cpu"]

    def test_the_result_follows_the_order_of_propagated_fields(self):
        ws = make_ws(resource_cpu="1", parallelism=1, execution_mode="agent")
        previous = {"parallelism": 1, "resource-cpu": "1", "execution-mode": "agent"}
        new = {"parallelism": 2, "resource-cpu": "2", "execution-mode": "local"}
        expected = [f for f in PROPAGATED_FIELDS if f in new]
        assert [v.field for v in classify(ws, previous, new)] == expected

    def test_an_unknown_field_is_refused_rather_than_dropped(self):
        with pytest.raises(ValueError, match="no-such-field"):
            classify(make_ws(), {"no-such-field": 1}, {"no-such-field": 2})

    def test_the_renamed_fields_read_the_workspace_column(self):
        ws = make_ws(engine_version="1.12", auto_apply_mode="never")
        got = verdicts(
            ws,
            {"terraform-version": "1.12", "auto-apply-mode": "never"},
            {"terraform-version": "1.13", "auto-apply-mode": "always"},
        )
        assert got == {"terraform-version": "apply", "auto-apply-mode": "apply"}

    def test_the_whole_value_of_a_list_field_is_compared(self):
        previous = {"var-files": ["a.tfvars"]}
        new = {"var-files": ["a.tfvars", "b.tfvars"]}
        assert verdicts(make_ws(var_files=["a.tfvars"]), previous, new) == {"var-files": "apply"}
        assert verdicts(make_ws(var_files=["a.tfvars", "b.tfvars"]), previous, new) == {
            "var-files": "unchanged"
        }
        assert verdicts(make_ws(var_files=["a.tfvars", "mine.tfvars"]), previous, new) == {
            "var-files": "customised"
        }


class TestClassifyLabels:
    previous = {"labels": {"team": "a"}}
    new = {"labels": {"team": "b"}}

    def test_labels_equal_to_the_previous_rule_labels_are_applied(self):
        assert verdicts(make_ws(labels={"team": "a"}), self.previous, self.new) == {
            "labels": "apply"
        }

    def test_labels_equal_to_the_new_rule_labels_are_unchanged(self):
        assert verdicts(make_ws(labels={"team": "b"}), self.previous, self.new) == {
            "labels": "unchanged"
        }

    def test_a_workspace_with_an_extra_label_is_customised_never_merged(self):
        ws = make_ws(labels={"team": "a", "env": "prod"})
        [v] = classify(ws, self.previous, self.new)
        assert v.verdict == "customised"
        assert v.current == {"team": "a", "env": "prod"}

    def test_a_workspace_with_no_labels_reads_as_empty(self):
        got = verdicts(make_ws(labels=None), {"labels": {}}, {"labels": {"team": "b"}})
        assert got == {"labels": "apply"}


class TestClassifyNoneAndEmptyStrings:
    @pytest.mark.parametrize("field", ["slack-channel", "default-run-kind"])
    def test_none_and_empty_are_the_same_unset_string(self, field):
        column = field.replace("-", "_")
        assert verdicts(make_ws(**{column: ""}), {field: None}, {field: "x"}) == {field: "apply"}
        assert verdicts(make_ws(**{column: None}), {field: ""}, {field: "x"}) == {field: "apply"}
        assert verdicts(make_ws(**{column: ""}), {field: "x"}, {field: None}) == {
            field: "unchanged"
        }

    def test_an_unset_string_is_not_equal_to_a_value(self):
        assert verdicts(
            make_ws(slack_channel="#ops"), {"slack-channel": ""}, {"slack-channel": "#x"}
        ) == {"slack-channel": "customised"}

    def test_none_stays_distinct_from_zero_for_a_number(self):
        ws = make_ws(configure_parallelism=0)
        got = verdicts(ws, {"configure-parallelism": None}, {"configure-parallelism": 4})
        assert got == {"configure-parallelism": "customised"}


class TestIsChanged:
    @pytest.mark.parametrize(
        ("field", "previous", "new"),
        [
            ("slack-channel", None, ""),
            ("default-limit", "", None),
            ("ee-resource-cpu", None, ""),
            ("var-files", None, []),
            ("security-scan-skip-rules", [], None),
            ("labels", None, {}),
            ("oidc-audiences", {}, None),
            ("parallelism", 10, 10),
            ("engine-version", "1.9", "1.9"),
            ("agent-pool-id", None, None),
        ],
    )
    def test_an_unset_spelling_or_an_equal_value_is_not_a_change(self, field, previous, new):
        assert is_changed(field, previous, new) is False

    @pytest.mark.parametrize(
        ("field", "previous", "new"),
        [
            ("parallelism", 0, None),
            ("configure-parallelism", None, 0),
            ("slack-channel", "", "#ops"),
            ("engine-version", "1.8", "1.9"),
            ("var-files", [], ["a.tfvars"]),
            ("labels", {}, {"team": "a"}),
            ("labels", {"team": "a"}, {"team": "b"}),
            ("agent-pool-id", None, uuid.uuid4()),
            ("auto-apply-mode", "never", "always"),
        ],
    )
    def test_a_real_change_is_reported(self, field, previous, new):
        assert is_changed(field, previous, new) is True

    def test_a_pool_is_the_same_whichever_way_it_is_spelled(self):
        pool = uuid.uuid4()

        assert is_changed("agent-pool-id", pool, f"apool-{pool}") is False

    def test_an_unknown_field_falls_back_to_plain_inequality(self):
        assert is_changed("pattern", None, "") is True
        assert is_changed("pattern", "a", "a") is False


class TestClassifyPoolSet:
    pool_a, pool_b, pool_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def test_the_workspace_pool_set_is_compared_as_a_list_of_ids(self):
        previous = {"agent-pool-id": str(self.pool_a)}
        new = {"agent-pool-id": str(self.pool_b)}
        assert verdicts(make_ws([self.pool_a]), previous, new) == {"agent-pool-id": "apply"}
        assert verdicts(make_ws([self.pool_b]), previous, new) == {"agent-pool-id": "unchanged"}
        assert verdicts(make_ws([self.pool_c]), previous, new) == {"agent-pool-id": "customised"}

    def test_a_workspace_with_extra_pools_is_customised(self):
        ws = make_ws([self.pool_a, self.pool_c])
        got = verdicts(ws, {"agent-pool-id": str(self.pool_a)}, {"agent-pool-id": str(self.pool_b)})
        assert got == {"agent-pool-id": "customised"}

    def test_no_previous_pool_means_an_empty_set(self):
        [v] = classify(make_ws([]), {"agent-pool-id": None}, {"agent-pool-id": str(self.pool_b)})
        assert v == FieldVerdict("agent-pool-id", [], [], [self.pool_b], "apply")

    def test_clearing_the_pool_means_an_empty_set(self):
        got = verdicts(make_ws([]), {"agent-pool-id": str(self.pool_a)}, {"agent-pool-id": None})
        assert got == {"agent-pool-id": "unchanged"}

    def test_the_prefixed_and_raw_ids_are_the_same_pool(self):
        previous = {"agent-pool-id": f"apool-{self.pool_a}"}
        got = verdicts(make_ws([self.pool_a]), previous, {"agent-pool-id": str(self.pool_b)})
        assert got == {"agent-pool-id": "apply"}


class TestClassifyTemplates:
    field = "run-task-templates"

    def run(self, rows: dict, previous: list, new: list) -> FieldVerdict:
        [v] = classify(make_ws(), {self.field: previous}, {self.field: new}, {self.field: rows})
        return v

    def test_a_name_the_workspace_lacks_is_inserted(self):
        v = self.run({}, [], [task()])
        assert v.verdict == "apply"
        assert v.current == {}
        assert v.new == {"scan": task(hmac_key="<unset>")}

    def test_a_row_equal_to_the_previous_spec_is_updated(self):
        v = self.run({"scan": task(url="https://old")}, [task(url="https://old")], [task()])
        assert v.verdict == "apply"
        assert v.current == {"scan": task(url="https://old", hmac_key="<unset>")}
        assert v.previous == {"scan": task(url="https://old", hmac_key="<unset>")}

    def test_a_row_equal_to_the_new_spec_is_unchanged(self):
        v = self.run({"scan": task()}, [task(url="https://old")], [task()])
        assert v.verdict == "unchanged"

    def test_a_row_equal_to_neither_is_customised(self):
        v = self.run({"scan": task(url="https://mine")}, [task(url="https://old")], [task()])
        assert v.verdict == "customised"

    def test_a_new_name_with_a_different_existing_row_is_customised(self):
        v = self.run({"scan": task(url="https://mine")}, [], [task()])
        assert v.verdict == "customised"
        assert v.previous == {}

    def test_one_customised_name_skips_the_whole_field(self):
        rows = {"a": task(name="a", url="https://mine")}
        v = self.run(rows, [task(name="a", url="https://old")], [task(name="a"), task(name="b")])
        assert v.verdict == "customised"

    def test_one_name_to_apply_and_the_rest_unchanged_applies(self):
        rows = {"a": task(name="a")}
        v = self.run(rows, [], [task(name="a"), task(name="b")])
        assert v.verdict == "apply"

    def test_every_name_already_current_is_unchanged(self):
        rows = {"a": task(name="a"), "b": task(name="b")}
        v = self.run(rows, [task(name="a", url="https://old")], [task(name="a"), task(name="b")])
        assert v.verdict == "unchanged"

    def test_a_name_the_rule_left_alone_is_unchanged_while_a_missing_one_applies(self):
        v = self.run({"scan": task()}, [task()], [task(), task(name="other")])
        assert v.verdict == "apply"
        assert self.run({"scan": task()}, [task()], [task()]).verdict == "unchanged"

    def test_a_name_the_rule_already_had_but_the_workspace_lacks_is_customised(self):
        v = self.run({}, [task()], [task(url="https://new")])
        assert v.verdict == "customised"
        assert v.current == {}
        assert v.previous == {"scan": task(hmac_key="<unset>")}

    def test_an_unchanged_name_the_workspace_lacks_is_customised_too(self):
        assert self.run({}, [task()], [task()]).verdict == "customised"

    def test_a_deleted_name_blocks_a_new_name_on_the_same_field(self):
        v = self.run({}, [task(name="a")], [task(name="a"), task(name="b")])
        assert v.verdict == "customised"

    def test_a_name_new_to_the_rule_is_inserted_beside_a_present_one(self):
        v = self.run({"a": task(name="a")}, [task(name="a")], [task(name="a"), task(name="b")])
        assert v.verdict == "apply"

    def test_an_unset_secret_is_the_same_whether_none_or_empty(self):
        v = self.run({"scan": task(hmac_key="")}, [task(url="https://old")], [task()])
        assert v.verdict == "unchanged"

    def test_extra_columns_on_the_workspace_row_are_ignored(self):
        row = {**task(), "id": "x", "created_at": "t"}
        assert self.run({"scan": row}, [task(url="https://old")], [task()]).verdict == "unchanged"

    def test_notification_templates_compare_the_same_way(self):
        spec = {
            "name": "n",
            "destination_type": "slack",
            "url": "https://s",
            "token": None,
            "triggers": ["run:errored"],
            "email_addresses": [],
            "enabled": True,
        }
        field = "notification-templates"
        old = {**spec, "triggers": ["run:completed"]}
        got = classify(make_ws(), {field: [old]}, {field: [spec]}, {field: {"n": old}})
        assert [v.verdict for v in got] == ["apply"]

    def test_a_template_name_the_rule_did_not_change_is_unchanged(self):
        assert self.run({"scan": task()}, [task()], [task()]).verdict == "unchanged"

    def test_a_customised_name_the_rule_never_touched_blocks_the_whole_field(self):
        # Deliberate: bulk applies every spec in the list, so applying "b" would overwrite it.
        rows = {"a": task(name="a", url="https://mine")}
        v = self.run(rows, [task(name="a")], [task(name="a"), task(name="b")])
        assert v.verdict == "customised"

    def test_a_differing_secret_makes_the_name_customised(self):
        rows = {"scan": task(hmac_key="workspace-secret")}
        previous = [task(url="https://old", hmac_key="old-secret")]
        assert self.run(rows, previous, [task(hmac_key="new-secret")]).verdict == "customised"

    def test_a_differing_notification_token_makes_the_name_customised(self):
        field = "notification-templates"
        old = {**NOTIFICATION, "token": "old-token"}
        new = {**NOTIFICATION, "token": "new-token"}
        row = {**NOTIFICATION, "token": "workspace-token"}
        [v] = classify(make_ws(), {field: [old]}, {field: [new]}, {field: {"n": row}})
        assert v.verdict == "customised"

    def test_secrets_show_only_whether_they_are_set(self):
        rows = {"scan": task(hmac_key="workspace-secret")}
        v = self.run(rows, [task(hmac_key=None)], [task(hmac_key="new-secret")])
        assert v.current["scan"]["hmac_key"] == "<set>"
        assert v.previous["scan"]["hmac_key"] == "<unset>"
        assert v.new["scan"]["hmac_key"] == "<set>"

    def test_only_the_rule_spec_keys_are_shown_for_a_workspace_row(self):
        row = {**task(), "id": "x", "delivery_responses": ["boom"]}
        v = self.run({"scan": row}, [task(url="https://old")], [task()])
        assert set(v.current["scan"]) == set(task())

    @pytest.mark.parametrize("rows_match", ["previous", "new", "neither", "missing"])
    def test_no_secret_value_appears_in_any_verdict(self, rows_match):
        secrets = ["row-secret", "old-secret", "new-secret", "row-token", "old-token", "new-token"]
        old_task = task(url="https://old", hmac_key="old-secret")
        new_task = task(hmac_key="new-secret")
        old_note = {**NOTIFICATION, "triggers": ["run:completed"], "token": "old-token"}
        new_note = {**NOTIFICATION, "token": "new-token"}
        rows = {
            "previous": ({**old_task, "delivery_responses": ["row-secret"]}, old_note),
            "new": (new_task, new_note),
            "neither": (task(hmac_key="row-secret"), {**NOTIFICATION, "token": "row-token"}),
        }
        task_row, note_row = rows.get(rows_match, (None, None))
        found = {
            "run-task-templates": {"scan": task_row} if task_row else {},
            "notification-templates": {"n": note_row} if note_row else {},
        }
        got = classify(
            make_ws(),
            {"run-task-templates": [old_task], "notification-templates": [old_note]},
            {"run-task-templates": [new_task], "notification-templates": [new_note]},
            found,
        )
        assert not [s for s in secrets if s in repr(got)]

    def test_the_workspace_rows_must_be_supplied_for_a_template_field(self):
        with pytest.raises(ValueError, match=self.field):
            classify(make_ws(), {self.field: []}, {self.field: [task()]})
        with pytest.raises(ValueError, match=self.field):
            classify(make_ws(), {self.field: []}, {self.field: [task()]}, {})

    def test_template_rows_are_not_needed_for_other_fields(self):
        assert verdicts(make_ws(resource_cpu="1"), {"resource-cpu": "1"}, {"resource-cpu": "2"})


SCHEDULE_ON = {
    "scheduled-configure-enabled": True,
    "scheduled-configure-cron": "0 2 * * *",
    "scheduled-configure-timezone": "UTC",
}


def schedule_columns(**overrides: Any) -> dict[str, Any]:
    return {k.replace("-", "_"): v for k, v in {**SCHEDULE_ON, **overrides}.items()}


def schedule_ws(**overrides: Any) -> SimpleNamespace:
    return make_ws(**schedule_columns(**overrides))


class TestClassifyScheduledConfigure:
    def test_the_three_settings_propagate(self):
        for name in SCHEDULE_ON:
            assert name in PROPAGATED_FIELDS
            assert name not in NOT_PROPAGATED

    def test_a_workspace_on_the_previous_schedule_is_updated(self):
        previous, new = dict(SCHEDULE_ON), {**SCHEDULE_ON, "scheduled-configure-cron": "0 3 * * *"}
        assert verdicts(schedule_ws(), previous, new) == {
            "scheduled-configure-enabled": "unchanged",
            "scheduled-configure-cron": "apply",
            "scheduled-configure-timezone": "unchanged",
        }

    def test_a_workspace_with_its_own_cron_is_customised(self):
        previous, new = dict(SCHEDULE_ON), {**SCHEDULE_ON, "scheduled-configure-cron": "0 3 * * *"}
        got = verdicts(schedule_ws(**{"scheduled-configure-cron": "30 4 * * 1"}), previous, new)
        assert got["scheduled-configure-cron"] == "customised"

    def test_a_zone_is_never_moved_under_a_workspace_with_its_own_cron(self):
        previous = dict(SCHEDULE_ON)
        new = {**SCHEDULE_ON, "scheduled-configure-timezone": "Europe/London"}
        ws = schedule_ws(**{"scheduled-configure-cron": "30 4 * * 1"})
        got = verdicts(ws, previous, new)
        assert got["scheduled-configure-timezone"] == "customised"
        assert got["scheduled-configure-cron"] == "customised"

    def test_a_schedule_is_never_switched_on_under_a_workspace_with_its_own_cron(self):
        previous = {**SCHEDULE_ON, "scheduled-configure-enabled": False}
        ws = schedule_ws(**{"scheduled-configure-enabled": False, "scheduled-configure-cron": "x"})
        got = verdicts(ws, previous, dict(SCHEDULE_ON))
        assert got["scheduled-configure-enabled"] == "customised"

    def test_a_disabled_workspace_is_not_handed_a_new_cron(self):
        previous, new = dict(SCHEDULE_ON), {**SCHEDULE_ON, "scheduled-configure-cron": "0 3 * * *"}
        got = verdicts(schedule_ws(**{"scheduled-configure-enabled": False}), previous, new)
        assert got == {
            "scheduled-configure-enabled": "customised",
            "scheduled-configure-cron": "customised",
            "scheduled-configure-timezone": "unchanged",
        }

    def test_a_workspace_on_the_previous_trio_takes_all_three(self):
        previous = {
            "scheduled-configure-enabled": False,
            "scheduled-configure-cron": "",
            "scheduled-configure-timezone": "UTC",
        }
        new = {
            "scheduled-configure-enabled": True,
            "scheduled-configure-cron": "0 2 * * *",
            "scheduled-configure-timezone": "Europe/London",
        }
        ws = schedule_ws(**previous)
        assert set(verdicts(ws, previous, new).values()) == {"apply"}

    def test_an_unset_cron_is_the_same_whether_none_or_empty(self):
        previous = {**SCHEDULE_ON, "scheduled-configure-cron": ""}
        ws = schedule_ws(**{"scheduled-configure-cron": None})
        new = {**previous, "scheduled-configure-cron": "0 2 * * *"}
        assert verdicts(ws, previous, new)["scheduled-configure-cron"] == "apply"


RULE = SimpleNamespace(id=uuid.UUID("01890000-0000-7000-8000-000000002549"))


def rule_ws(name: str, **columns: Any) -> SimpleNamespace:
    ops = {"default_run_kind": "", "plan_apply_enabled": True, "configure_enabled": False}
    return make_ws(id=uuid.uuid4(), name=name, **{**ops, **columns})


def rows_result(rows: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


class FakeBulk:
    """Stands in for `apply_update_to_workspaces`, recording each group it is given."""

    def __init__(self, clear_kind: bool = False) -> None:
        self.calls: list[tuple[list[str], dict, bool]] = []
        self.clear_kind = clear_kind

    async def __call__(self, db, workspaces, update, user, *, write_audit=True):
        self.calls.append(([w.name for w in workspaces], update, write_audit))
        for w in workspaces if self.clear_kind else []:
            w.default_run_kind = ""
        diff = {k.replace("-", "_"): {"from": None, "to": v} for k, v in update.items()}
        return [{"id": f"ws-{w.id}", "name": w.name, "diff": diff} for w in workspaces], []


async def propagate(
    workspaces: list,
    previous: dict,
    changed: dict,
    *extra_results: Any,
    dry_run: bool = False,
    skipped: int = 0,
    fake: FakeBulk | None = None,
    rule: Any = RULE,
) -> tuple[PropagationResult, FakeBulk, MagicMock]:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[rows_result(workspaces), *extra_results])
    db.scalar = AsyncMock(return_value=skipped)
    fake = fake or FakeBulk()
    with patch.object(workspace_bulk, "apply_update_to_workspaces", fake):
        result = await propagate_rule_update(
            db, rule, previous, changed, admin_principal(), "req-2549", dry_run=dry_run
        )
    return result, fake, db


def compiled(statement: Any) -> str:
    from sqlalchemy.dialects import postgresql

    return str(statement.compile(dialect=postgresql.dialect()))


class TestPropagateOidcAudiences:
    """The propagated value is validated by the rule every other write path uses."""

    FIELD = "oidc-audiences"
    VALID = {"aws": ["sts.amazonaws.example"]}
    BAD = [{"aws/../vault": ["x"]}, {"aws": []}, {"a b": ["x"]}, ["aws"], "aws"]

    @pytest.mark.parametrize("bad", BAD)
    @pytest.mark.parametrize("dry_run", [False, True])
    async def test_an_invalid_value_is_refused_before_any_workspace_is_judged(self, bad, dry_run):
        ws = rule_ws("a", oidc_audiences={})

        with pytest.raises(HTTPException) as exc:
            await propagate([ws], {self.FIELD: {}}, {self.FIELD: bad}, dry_run=dry_run)

        assert exc.value.status_code == 422
        assert self.FIELD in exc.value.detail

    async def test_an_invalid_value_is_refused_even_when_no_workspace_would_take_it(self):
        with pytest.raises(HTTPException) as exc:
            await propagate([], {self.FIELD: {}}, {self.FIELD: {"aws/../vault": ["x"]}})

        assert exc.value.status_code == 422

    async def test_the_refusal_applies_nothing(self):
        fake = FakeBulk()
        ws = rule_ws("a", oidc_audiences={})

        with pytest.raises(HTTPException):
            await propagate([ws], {self.FIELD: {}}, {self.FIELD: {"aws": []}}, fake=fake)

        assert fake.calls == []

    @pytest.mark.parametrize("dry_run", [False, True])
    async def test_a_valid_value_is_applied_as_stored(self, dry_run):
        ws = rule_ws("a", oidc_audiences={})

        result, fake, _ = await propagate(
            [ws], {self.FIELD: {}}, {self.FIELD: self.VALID}, dry_run=dry_run
        )

        assert fake.calls == [(["a"], {self.FIELD: self.VALID}, False)]
        assert [w["name"] for w in result.would_change] == ["a"]


class TestPropagateScheduledConfigure:
    """The three settings are judged as one unit; an unedited member is read off the rule."""

    @staticmethod
    def rule(**overrides: Any) -> SimpleNamespace:
        return SimpleNamespace(id=RULE.id, **schedule_columns(**overrides))

    async def test_an_edit_of_the_cron_alone_judges_the_whole_trio(self):
        ws = rule_ws("a", **schedule_columns())
        rule = self.rule(**{"scheduled-configure-cron": "0 3 * * *"})

        result, fake, _ = await propagate(
            [ws],
            {"scheduled-configure-cron": "0 2 * * *"},
            {"scheduled-configure-cron": "0 3 * * *"},
            rule=rule,
        )

        assert fake.calls == [(["a"], {"scheduled-configure-cron": "0 3 * * *"}, False)]
        assert result.customised == []

    async def test_a_zone_edit_skips_a_workspace_whose_cron_is_its_own(self):
        own = rule_ws("own", **schedule_columns(**{"scheduled-configure-cron": "30 4 * * 1"}))
        rule = self.rule(**{"scheduled-configure-timezone": "Europe/London"})

        result, fake, _ = await propagate(
            [own],
            {"scheduled-configure-timezone": "UTC"},
            {"scheduled-configure-timezone": "Europe/London"},
            rule=rule,
        )

        assert fake.calls == []
        [entry] = result.customised
        assert set(entry["fields"]) == {
            "scheduled-configure-timezone",
            "scheduled-configure-cron",
        }
        assert entry["fields"]["scheduled-configure-cron"]["current"] == "30 4 * * 1"

    async def test_a_dry_run_reports_the_schedule_without_a_lock_on_the_edit(self):
        ws = rule_ws("a", **schedule_columns())
        rule = self.rule(**{"scheduled-configure-cron": "0 3 * * *"})

        result, _, _ = await propagate(
            [ws],
            {"scheduled-configure-cron": "0 2 * * *"},
            {"scheduled-configure-cron": "0 3 * * *"},
            rule=rule,
            dry_run=True,
        )

        assert result.dry_run is True
        assert [w["name"] for w in result.would_change] == ["a"]

    async def test_an_edit_touching_no_schedule_setting_never_reads_the_rule_schedule(self):
        ws = rule_ws("a", engine_version="1.8")

        _, fake, _ = await propagate(
            [ws], {"engine-version": "1.8"}, {"engine-version": "1.9"}, rule=RULE
        )

        assert fake.calls == [(["a"], {"engine-version": "1.9"}, False)]


class TestPropagationGroups:
    async def test_workspaces_with_the_same_applied_fields_share_one_bulk_call(self):
        a = rule_ws("a", engine_version="1.8", drift_detection_interval_seconds=60)
        b = rule_ws("b", engine_version="1.8", drift_detection_interval_seconds=60)
        c = rule_ws("c", engine_version="1.8", drift_detection_interval_seconds=30)
        previous = {"engine-version": "1.8", "drift-detection-interval-seconds": 60}
        changed = {"engine-version": "1.9", "drift-detection-interval-seconds": 120}

        _, fake, _ = await propagate([a, b, c], previous, changed)

        assert fake.calls == [
            (
                ["a", "b"],
                {"engine-version": "1.9", "drift-detection-interval-seconds": 120},
                False,
            ),
            (["c"], {"engine-version": "1.9"}, False),
        ]

    async def test_a_partly_customised_workspace_is_in_both_lists(self):
        ws = rule_ws("a", engine_version="1.8", drift_detection_interval_seconds=30)
        previous = {"engine-version": "1.8", "drift-detection-interval-seconds": 60}
        changed = {"engine-version": "1.9", "drift-detection-interval-seconds": 120}

        result, _, _ = await propagate([ws], previous, changed)

        assert [w["name"] for w in result.would_change] == ["a"]
        assert result.customised == [
            {
                "id": f"ws-{ws.id}",
                "name": "a",
                "fields": {
                    "drift-detection-interval-seconds": {
                        "current": 30,
                        "previous": 60,
                        "new": 120,
                    }
                },
            }
        ]
        assert result.unchanged == []

    async def test_customised_and_unchanged_workspaces_are_not_applied(self):
        same = rule_ws("same", engine_version="1.9")
        own = rule_ws("own", engine_version="1.5")

        result, fake, _ = await propagate(
            [same, own], {"engine-version": "1.8"}, {"engine-version": "1.9"}
        )

        assert fake.calls == []
        assert result.unchanged == [{"id": f"ws-{same.id}", "name": "same"}]
        assert [w["name"] for w in result.customised] == ["own"]
        assert result.would_change == []

    async def test_only_active_workspaces_are_loaded_and_the_rest_counted(self):
        live = rule_ws("live", engine_version="1.8")

        result, fake, db = await propagate(
            [live], {"engine-version": "1.8"}, {"engine-version": "1.9"}, skipped=2
        )

        assert [names for names, _, _ in fake.calls] == [["live"]]
        assert (result.matched, result.skipped_lifecycle) == (1, 2)
        loaded = compiled(db.execute.await_args_list[0].args[0])
        counted = compiled(db.scalar.await_args.args[0])
        assert "workspaces.lifecycle_state = " in loaded
        assert "count(" in counted and "workspaces.lifecycle_state != " in counted

    async def test_an_apply_locks_the_workspaces_it_reads(self):
        _, _, db = await propagate([], {"engine-version": "1.8"}, {"engine-version": "1.9"})
        statement = db.execute.await_args_list[0].args[0]
        assert "FOR UPDATE OF workspaces" in compiled(statement)
        assert statement.get_execution_options()["populate_existing"] is True

    async def test_a_dry_run_takes_no_lock(self):
        _, _, db = await propagate(
            [], {"engine-version": "1.8"}, {"engine-version": "1.9"}, dry_run=True
        )
        statement = db.execute.await_args_list[0].args[0]
        assert "FOR UPDATE" not in compiled(statement)
        assert "populate_existing" not in statement.get_execution_options()

    async def test_an_empty_change_set_needs_no_previous_values(self):
        result, fake, _ = await propagate([], {}, {})
        assert (fake.calls, result.would_change) == ([], [])

    async def test_a_changed_field_missing_from_previous_is_named(self):
        with pytest.raises(ValueError, match="engine-version"):
            await propagate([], {}, {"engine-version": "1.9"})

    async def test_fields_that_do_not_propagate_are_ignored(self):
        ws = rule_ws("a", engine_version="1.8")
        previous = {"engine-version": "1.8", "name": "old", "pattern": "a/**"}
        changed = {"engine-version": "1.9", "name": "new", "pattern": "b/**"}

        _, fake, _ = await propagate([ws], previous, changed)

        assert fake.calls == [(["a"], {"engine-version": "1.9"}, False)]

    @pytest.mark.parametrize("alias", ["terraform-version", "auto-apply"])
    async def test_an_alias_spelling_is_refused(self, alias):
        with pytest.raises(ValueError, match=alias):
            await propagate([], {alias: "x"}, {alias: "y"})

    async def test_the_result_echoes_dry_run(self):
        result, _, _ = await propagate([], {}, {}, dry_run=True)
        assert result == PropagationResult(True, 0, [], [], [], 0)


class TestPropagationPayload:
    async def test_the_single_rule_pool_becomes_the_bulk_pool_list(self):
        old, new = uuid.uuid4(), uuid.uuid4()
        set_ws = rule_ws("a", pools=[old])
        cleared_ws = rule_ws("b", pools=[new])

        _, fake, _ = await propagate([set_ws], {"agent-pool-id": old}, {"agent-pool-id": new})
        _, cleared, _ = await propagate(
            [cleared_ws], {"agent-pool-id": new}, {"agent-pool-id": None}
        )

        assert fake.calls[0][1] == {"agent-pool-ids": [str(new)]}
        assert cleared.calls[0][1] == {"agent-pool-ids": []}

    async def test_a_customised_pool_is_reported_with_prefixed_ids(self):
        mine, old, new = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        result, _, _ = await propagate(
            [rule_ws("a", pools=[mine])], {"agent-pool-id": old}, {"agent-pool-id": new}
        )
        assert result.customised[0]["fields"]["agent-pool-id"] == {
            "current": [f"apool-{mine}"],
            "previous": [f"apool-{old}"],
            "new": [f"apool-{new}"],
        }

    async def test_run_tasks_reach_bulk_in_its_input_shape_with_the_real_secret(self):
        ws = rule_ws("a")
        spec = task(hmac_key="s3cret", enforcement_level="advisory")
        field = "run-task-templates"

        _, fake, _ = await propagate([ws], {field: []}, {field: [spec]}, rows_result([]))

        [(_, update, _)] = fake.calls
        assert update["run-tasks"] == [
            {
                "name": "scan",
                "url": "https://hooks.example/a",
                "hmac-key": "s3cret",
                "stage": "pre_plan",
                "enforcement-level": "advisory",
                "enabled": True,
            }
        ]
        assert workspace_bulk.validate_run_task_specs(update["run-tasks"]) == [spec]

    async def test_notifications_reach_bulk_in_its_input_shape_with_the_real_token(self):
        ws = rule_ws("a")
        spec = {**NOTIFICATION, "destination_type": "email", "token": "t0ken"}
        spec["email_addresses"] = ["ops@example.com"]
        field = "notification-templates"

        _, fake, _ = await propagate([ws], {field: []}, {field: [spec]}, rows_result([]))

        [(_, update, _)] = fake.calls
        [sent] = update["notification-configurations"]
        assert sent["destination-type"] == "email"
        assert sent["token"] == "t0ken"
        assert sent["email-addresses"] == ["ops@example.com"]
        assert workspace_bulk.validate_notification_specs([sent]) == [spec]

    async def test_only_the_template_names_to_apply_are_sent(self):
        field = "run-task-templates"
        kept = task(name="kept")
        moved_old, moved_new = task(name="moved", url="https://old"), task(name="moved")
        a = rule_ws("a")
        rows = [RunTask(workspace_id=a.id, **kept), RunTask(workspace_id=a.id, **moved_old)]

        _, fake, _ = await propagate(
            [a], {field: [kept, moved_old]}, {field: [kept, moved_new]}, rows_result(rows)
        )

        [(_, update, _)] = fake.calls
        assert [s["name"] for s in update["run-tasks"]] == ["moved"]

    @pytest.mark.parametrize("dry_run", [False, True])
    async def test_template_rows_are_locked_only_when_applying(self, dry_run):
        previous = {"run-task-templates": [], "notification-templates": []}
        changed = {"run-task-templates": [task()], "notification-templates": [NOTIFICATION]}

        _, _, db = await propagate(
            [rule_ws("a")], previous, changed, rows_result([]), rows_result([]), dry_run=dry_run
        )

        loads = [compiled(c.args[0]) for c in db.execute.await_args_list[1:]]
        assert len(loads) == 2
        assert all(("FOR UPDATE" in sql) is (not dry_run) for sql in loads)

    async def test_templates_are_loaded_once_per_field_for_every_workspace(self):
        workspaces = [rule_ws(n) for n in ("a", "b", "c")]
        previous = {"run-task-templates": [], "notification-templates": []}
        changed = {"run-task-templates": [task()], "notification-templates": [NOTIFICATION]}

        _, fake, db = await propagate(
            workspaces, previous, changed, rows_result([]), rows_result([])
        )

        assert db.execute.await_count == 3
        assert [names for names, _, _ in fake.calls] == [["a", "b", "c"]]


class TestPropagationAudit:
    async def test_one_row_per_changed_workspace_naming_the_rule_and_request(self):
        a = rule_ws("a", engine_version="1.8")
        b = rule_ws("b", engine_version="1.8")
        c = rule_ws("c", engine_version="1.9")

        _, _, db = await propagate([a, b, c], {"engine-version": "1.8"}, {"engine-version": "1.9"})

        [(rows,), _] = db.add_all.call_args
        assert [r.resource_id for r in rows] == [f"ws-{a.id}", f"ws-{b.id}"]
        for row in rows:
            assert isinstance(row, AuditLog)
            assert row.action == "workspace.rule_propagate"
            assert len(row.action) <= 40
            assert row.request_id == "req-2549"
            assert row.actor_email == "admin@test.com"
            assert row.resource_type == "workspace"
            assert row.status_code == 200
            assert row.detail == f"autodiscovery-rule {RULE.id}: ['engine_version']"


def bulk_ws(**columns: Any) -> SimpleNamespace:
    defaults = {
        "id": uuid.uuid4(),
        "name": "w",
        "engine": "terraform",
        "engine_version": "1.8",
        "default_run_kind": "",
        "plan_apply_enabled": True,
        "configure_enabled": False,
        "vcs_workflow": "merge_then_apply",
    }
    return make_ws(**{**defaults, **columns})


ENGINE_PLAN = {"fields": {"engine_version": "1.9"}, "run_tasks": None, "notifications": None}


class TestBulkApplyAudit:
    async def test_bulk_update_still_writes_its_own_rows_by_default(self):
        db, ws = MagicMock(), bulk_ws()
        changed, _ = await workspace_bulk._apply(db, [ws], ENGINE_PLAN, "admin@test.com")
        [(row,)] = [c.args for c in db.add.call_args_list]
        assert (row.action, row.detail) == (
            "workspace.bulk_update",
            "bulk-update: ['engine_version']",
        )
        assert (row.actor_email, row.resource_id) == ("admin@test.com", f"ws-{ws.id}")
        assert row.request_id is None
        assert changed[0]["diff"] == {"engine_version": {"from": "1.8", "to": "1.9"}}

    async def test_write_audit_false_applies_without_a_row(self):
        db, ws = MagicMock(), bulk_ws()
        changed, _ = await workspace_bulk._apply(
            db, [ws], ENGINE_PLAN, "admin@test.com", write_audit=False
        )
        db.add.assert_not_called()
        assert ws.engine_version == "1.9"
        assert [c["name"] for c in changed] == ["w"]


class TestApplyUpdateToWorkspaces:
    async def test_a_guard_refusal_raises_before_anything_is_applied(self):
        db, ws = MagicMock(), bulk_ws(name="tf")
        with pytest.raises(HTTPException) as exc:
            await workspace_bulk.apply_update_to_workspaces(
                db, [ws], {"pulumi-bind-plan": True}, admin_principal()
            )
        assert exc.value.status_code == 422
        assert "tf" in exc.value.detail
        db.add.assert_not_called()

    async def test_a_valid_update_is_applied_and_reported(self):
        db, ws = MagicMock(), bulk_ws()
        changed, unchanged = await workspace_bulk.apply_update_to_workspaces(
            db, [ws], {"engine-version": "1.9"}, admin_principal(), write_audit=False
        )
        assert changed == [
            {
                "id": f"ws-{ws.id}",
                "name": "w",
                "diff": {"engine_version": {"from": "1.8", "to": "1.9"}},
            }
        ]
        assert unchanged == []
        db.add.assert_not_called()


def admin_principal():
    from terrapod.api.dependencies import AuthenticatedUser

    return AuthenticatedUser(
        email="admin@test.com",
        display_name=None,
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


#: Configure switched off by the rule, so a stored `configure` preference is orphaned.
OPS_PREVIOUS = {"configure-enabled": True, "default-run-kind": "plan_apply"}
OPS_CHANGED = {"configure-enabled": False, "default-run-kind": "plan"}


class TestPropagationRunKind:
    async def test_a_customised_kind_the_flags_would_clear_refuses_before_applying(self):
        early = rule_ws("a", configure_enabled=True, default_run_kind="plan_apply")
        ws = rule_ws("own", configure_enabled=True, default_run_kind="configure")
        fake = FakeBulk()

        with pytest.raises(HTTPException) as exc:
            await propagate([early, ws], OPS_PREVIOUS, OPS_CHANGED, fake=fake)

        assert fake.calls == []
        assert exc.value.status_code == 422
        assert "own" in exc.value.detail and "default-run-kind" in exc.value.detail

    async def test_a_customised_kind_the_flags_keep_is_applied(self):
        ws = rule_ws(
            "own", plan_apply_enabled=False, configure_enabled=True, default_run_kind="full"
        )
        previous = {"plan-apply-enabled": False, "default-run-kind": "configure"}
        changed = {"plan-apply-enabled": True, "default-run-kind": "plan"}

        _, fake, _ = await propagate([ws], previous, changed)

        assert [names for names, _, _ in fake.calls] == [["own"]]

    async def test_a_kind_the_reconcile_changes_is_reported_in_the_diff_and_audit(self):
        ws = rule_ws("w", configure_enabled=True, default_run_kind="configure")

        result, _, db = await propagate(
            [ws], {"configure-enabled": True}, {"configure-enabled": False}, fake=FakeBulk(True)
        )

        diff = result.would_change[0]["diff"]
        assert diff["default_run_kind"] == {"from": "configure", "to": ""}
        [(rows,), _] = db.add_all.call_args
        assert "default_run_kind" in rows[0].detail
