"""Parse Terraform plan JSON (`tofu show -json tfplan`) into resource-change counts.

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
    doesn't look like a Terraform plan (so the caller can leave the
    DB columns null rather than write zeros that would imply
    "definitely no changes").
    """
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    resource_changes = data.get("resource_changes")
    if not isinstance(resource_changes, list):
        return None
    return _count_changes(resource_changes)


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
