"""What a Pulumi preview found, in the shape the platform already reads (#1560).

Terraform's runner hands back two things after a plan: `has_changes`, which
drives the no-op short-circuit, drift and conditional auto-apply, and the plan
JSON, which the change badges, the AI summary and the post-plan gates read. The
Pulumi path handed back neither, so every Pulumi run had `has_changes` unknown
and nothing to read.

`pulumi preview` can report the same facts, but not while also printing the
output a person reads: `--json` replaces the human output with a JSON document.
So the preview keeps its normal output and is additionally told to write its
**engine event log** (`--event-log`), a JSON object per line. This module turns
that log into a compact digest:

    {"engine": "pulumi", "change_summary": {...}, "steps": [...], "has_changes": bool}

which is uploaded as the run's plan artifact and summarised server-side by
`services/plan_summary.py`. The digest, not the raw log, because the log holds
every diff of every property -- including resource outputs, which may be secret
-- and grows with the size of the stack rather than the size of the change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("runner.pulumi_preview")

#: Operations Pulumi reports in a preview's summary. `same` is the one that
#: means "nothing to do"; the rest are changes.
_UNCHANGED = "same"

#: How many steps the digest keeps. A preview of a large stack can hold
#: thousands, and the digest exists to be read, not to be complete: the counts
#: come from the summary, which is exact however many steps are kept.
MAX_STEPS = 500


def parse_event_log(path: Path) -> dict[str, Any] | None:
    """Read a preview's event log into a digest, or None if it has no summary.

    Tolerant by design: the log is written line by line while the preview runs,
    so a killed preview leaves a truncated final line, and an unknown event type
    from a newer CLI is simply not one of the two this reads. Neither is a
    reason to discard what did arrive -- but a log with no summary event means
    the preview did not finish, and no counts may be inferred from it.
    """
    if not path.exists():
        return None

    summary: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []
    truncated = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                truncated += 1
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("summaryEvent"), dict):
                summary = event["summaryEvent"]
            elif isinstance(event.get("resourcePreEvent"), dict):
                if len(steps) < MAX_STEPS:
                    steps.append(_step(event["resourcePreEvent"]))

    if truncated:
        logger.warning("pulumi event log had unparseable lines", lines=truncated)
    if summary is None:
        return None

    changes = summary.get("resourceChanges")
    changes = changes if isinstance(changes, dict) else {}
    return {
        "engine": "pulumi",
        "change_summary": {k: int(v) for k, v in changes.items() if isinstance(v, int)},
        "has_changes": has_changes(changes),
        "steps": steps,
        "steps_truncated": len(steps) == MAX_STEPS,
    }


def has_changes(change_summary: dict[str, Any]) -> bool:
    """Whether a preview's summary describes any work.

    Anything that is not `same` counts, including an operation this code has
    never heard of: a newer Pulumi reporting a new one must read as "something
    will happen", never as "nothing will".
    """
    return any(
        isinstance(count, int) and count > 0
        for op, count in change_summary.items()
        if op != _UNCHANGED
    )


def _step(event: dict[str, Any]) -> dict[str, Any]:
    """One resource step: what happens to what.

    Deliberately the metadata only -- the operation, the resource's URN and its
    type. The event also carries the resource's old and new state, which is
    where a stack's secrets live.
    """
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "op": str(metadata.get("op") or ""),
        "urn": str(metadata.get("urn") or ""),
        "type": str(metadata.get("type") or ""),
    }


def write_digest(digest: dict[str, Any], path: Path) -> Path:
    """Write the digest where the artifact upload expects a file."""
    path.write_text(json.dumps(digest, indent=2), encoding="utf-8")
    return path
