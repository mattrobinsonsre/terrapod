"""Parse a run's plan artifact into resource-change counts.

Two shapes, one answer. Terraform's plan JSON (`tofu show -json tfplan`), and
the digest a Pulumi preview's engine events are reduced to (#1560) --
`{"engine": "pulumi", "change_summary": {"create": 2, "same": 9}}`. Both are
counted into the same five columns, so everything downstream (the change badges,
conditional auto-apply, the AI summary) reads one shape whatever produced it.

Pure function — called from the runner artifact upload handler. Keep it
small and side-effect-free so a future backfill script can reuse it.

The interesting field is `resource_changes[]`, where each entry has:
- `change.actions: list[str]` — one of `no-op`, `read`, `create`,
  `update`, `delete`, or a `[create, delete]` / `[delete, create]` pair
  for replacements.
- `change.importing.id` (optional) — set when this resource is being
  imported as part of this plan.

Counting rules:
- `[create]` → additions
- `[update]` → changes
- `[delete]` → destructions
- Any 2-element actions list containing both `create` and `delete` → replacements
  (NOT counted as additions or destructions — TFE/HCP shows replaces
  as a separate column for the same reason)
- `[no-op]`, `[read]`, unknown shapes → ignored
- `change.importing.id` non-null → imports (counted independently from the
  action-based bucket; an imported resource also has e.g. `[update]`)
"""

import json
from typing import Any


def summarize_plan_json(body: bytes) -> dict[str, int] | None:
    """Return additions/changes/destructions/replacements/imports counts.

    Returns None if the body isn't parseable JSON or the structure
    doesn't look like a plan either engine produced (so the caller can
    leave the DB columns null rather than write zeros that would imply
    "definitely no changes").
    """
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    resource_changes = data.get("resource_changes")
    if isinstance(resource_changes, list):
        return _count_changes(resource_changes)
    return _count_pulumi_preview(data)


#: Pulumi's operations, mapped onto the columns Terraform's actions fill.
#: `same` is not a change and has no column; an operation not listed here is
#: counted as a change rather than ignored, so a newer Pulumi reporting one
#: cannot make a run look emptier than it is.
_PULUMI_OPS = {
    "create": "additions",
    "update": "changes",
    "delete": "destructions",
    "replace": "replacements",
    "create-replacement": None,  # counted by `replace`; not double-counted
    "delete-replaced": None,
    "import": "imports",
    "import-replacement": "imports",
    "refresh": None,
    "read": None,
    "read-replacement": None,
    "discard": None,
    "remove-pending-replace": None,
    "same": None,
}


def _count_pulumi_preview(data: dict[str, Any]) -> dict[str, int] | None:
    """Count a Pulumi preview digest, or None if this is not one."""
    summary = data.get("change_summary")
    if data.get("engine") != "pulumi" or not isinstance(summary, dict):
        return None

    counts = {
        "additions": 0,
        "changes": 0,
        "destructions": 0,
        "replacements": 0,
        "imports": 0,
    }
    for op, raw in summary.items():
        if not isinstance(raw, int) or raw <= 0:
            continue
        column = _PULUMI_OPS.get(op, "changes") if op in _PULUMI_OPS else "changes"
        if column is not None:
            counts[column] += raw
    return counts


def _count_changes(resource_changes: list[Any]) -> dict[str, int]:
    additions = 0
    changes = 0
    destructions = 0
    replacements = 0
    imports = 0

    for entry in resource_changes:
        if not isinstance(entry, dict):
            continue
        change = entry.get("change")
        if not isinstance(change, dict):
            continue
        actions = change.get("actions")
        if not isinstance(actions, list):
            continue

        action_set = {a for a in actions if isinstance(a, str)}

        if len(action_set) == 2 and {"create", "delete"} <= action_set:
            replacements += 1
        elif action_set == {"create"}:
            additions += 1
        elif action_set == {"update"}:
            changes += 1
        elif action_set == {"delete"}:
            destructions += 1
        # else: no-op, read, unknown — ignore

        importing = change.get("importing")
        if isinstance(importing, dict) and importing.get("id"):
            imports += 1

    return {
        "additions": additions,
        "changes": changes,
        "destructions": destructions,
        "replacements": replacements,
        "imports": imports,
    }
