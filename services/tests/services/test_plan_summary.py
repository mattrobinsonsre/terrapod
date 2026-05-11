"""Tests for plan_summary.summarize_plan_json (#301)."""

import json

from terrapod.services.plan_summary import summarize_plan_json


def _plan(resource_changes: list[dict]) -> bytes:
    return json.dumps({"resource_changes": resource_changes}).encode()


def _change(actions: list[str], importing_id: str | None = None) -> dict:
    change: dict = {"actions": actions}
    if importing_id is not None:
        change["importing"] = {"id": importing_id}
    return {"change": change}


def test_empty_plan_zeros_everything():
    summary = summarize_plan_json(_plan([]))
    assert summary == {
        "additions": 0,
        "changes": 0,
        "destructions": 0,
        "replacements": 0,
        "imports": 0,
    }


def test_create_update_delete_counted_independently():
    body = _plan(
        [
            _change(["create"]),
            _change(["create"]),
            _change(["update"]),
            _change(["delete"]),
        ]
    )
    summary = summarize_plan_json(body)
    assert summary is not None
    assert summary["additions"] == 2
    assert summary["changes"] == 1
    assert summary["destructions"] == 1
    assert summary["replacements"] == 0


def test_replace_counts_as_replacement_not_add_plus_delete():
    """A `[create, delete]` pair is one replacement, not one add + one destroy."""
    body = _plan(
        [
            _change(["create", "delete"]),
            _change(["delete", "create"]),
        ]
    )
    summary = summarize_plan_json(body)
    assert summary is not None
    assert summary["replacements"] == 2
    assert summary["additions"] == 0
    assert summary["destructions"] == 0


def test_no_op_and_read_ignored():
    body = _plan(
        [
            _change(["no-op"]),
            _change(["read"]),
            _change(["create"]),
        ]
    )
    summary = summarize_plan_json(body)
    assert summary is not None
    assert summary["additions"] == 1
    assert summary["changes"] == 0
    assert summary["destructions"] == 0


def test_importing_counted_separately_from_action():
    """An imported resource also has a regular action — count both."""
    body = _plan(
        [
            _change(["update"], importing_id="i-abc123"),
            _change(["no-op"], importing_id="i-def456"),
            _change(["update"]),
        ]
    )
    summary = summarize_plan_json(body)
    assert summary is not None
    assert summary["imports"] == 2
    assert summary["changes"] == 2  # both update entries, the import flag is independent


def test_malformed_json_returns_none():
    assert summarize_plan_json(b"not valid json") is None


def test_missing_resource_changes_returns_none():
    """A JSON document that doesn't look like a Terraform plan."""
    assert summarize_plan_json(b'{"version": 1}') is None


def test_resource_changes_not_a_list_returns_none():
    assert summarize_plan_json(b'{"resource_changes": "oops"}') is None


def test_entries_with_unexpected_shape_skipped():
    body = json.dumps(
        {
            "resource_changes": [
                "not a dict",
                {"no_change_field": True},
                {"change": "not a dict"},
                {"change": {"actions": "not a list"}},
                {"change": {"actions": ["create"]}},
            ]
        }
    ).encode()
    summary = summarize_plan_json(body)
    assert summary is not None
    assert summary["additions"] == 1
    assert summary["changes"] == 0


def test_unknown_action_shape_ignored():
    body = _plan([_change(["mystery"]), _change(["create", "update"])])
    summary = summarize_plan_json(body)
    # mystery is unknown; create+update is also unknown (not the replace pair)
    assert summary == {
        "additions": 0,
        "changes": 0,
        "destructions": 0,
        "replacements": 0,
        "imports": 0,
    }
