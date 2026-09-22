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
    type. The event also carries the resource's old and new state, and while the
    engine redacts marked secrets there before writing the log (see
    `build_policy_input`), that state is the whole provider-returned shape of
    every resource: it dwarfs the change it describes, and the digest exists to
    be read.
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


# ── OPA policy input (#1567) ──────────────────────────────────────────


def _resource_name(urn: str) -> str:
    """The resource's own name, the last `::`-delimited segment of its URN.

    A URN reads `urn:pulumi:<stack>::<project>::<type-chain>::<name>`, and the
    name is what a policy is most likely to match on after the type.
    """
    return urn.rsplit("::", 1)[-1] if "::" in urn else ""


def _policy_resource(metadata: dict[str, Any]) -> dict[str, Any]:
    """One resource change, in the shape a policy reads.

    `inputs` is taken from the step's NEW state, which is the analogue of
    Terraform's `change.after`: what the program declared, before the provider
    answers. The OLD state is not carried -- a policy decides about what is
    being asked for.

    `outputs` is deliberately absent. It is the provider's complete returned
    state rather than anything the program said, so it adds little to a policy
    decision and a great deal to the document's size.
    """
    new = metadata.get("new")
    new = new if isinstance(new, dict) else {}
    inputs = new.get("inputs")
    detailed = metadata.get("detailedDiff")
    urn = str(metadata.get("urn") or "")

    return {
        "op": str(metadata.get("op") or ""),
        "urn": urn,
        "type": str(metadata.get("type") or ""),
        "name": _resource_name(urn),
        "parent": str(new.get("parent") or ""),
        "provider": str(new.get("provider") or ""),
        "custom": bool(new.get("custom", False)),
        "protect": bool(new.get("protect", False)),
        "inputs": inputs if isinstance(inputs, dict) else {},
        # Which property paths this step changes, and how. `diffs` names them;
        # `detailedDiff` classifies each one. Both are how a policy asks "did
        # anything under `tags` change?" without diffing the states itself.
        "diffs": [str(d) for d in metadata.get("diffs") or [] if isinstance(d, str)],
        "detailed_diff": detailed if isinstance(detailed, dict) else {},
    }


def build_policy_input(path: Path) -> dict[str, Any] | None:
    """The event log as an OPA input document, or None if the preview did not finish.

    Terraform hands OPA its plan JSON, so a policy reads real property values
    and decides about the change being proposed. Pulumi's runner had nothing to
    hand over, which is why policy sets were reported as out of scope for a
    Pulumi workspace rather than enforced (#1567).

    This is that missing document. It is built from the same engine event log
    the digest reads, but it is NOT the digest: the digest is a summary capped
    at `MAX_STEPS` for readability, and a gate evaluated over a truncated list
    of resources can pass because the offending one fell off the end. Every
    step is carried here for that reason.

    **On secrets.** The values here are what Pulumi's engine wrote, and the
    engine redacts before writing: `makeStepEventStateMetadata` passes both
    inputs and outputs through `filterResourceProperties`, which replaces every
    property marked secret with the literal string `"[secret]"` unless the CLI
    was given `--show-secrets`. The preview never is -- Terrapod passes that
    flag only to `pulumi stack export`, where the API seals the result. What
    this does NOT protect against is a value that is sensitive but was never
    marked secret in the program; Terraform's plan JSON has exactly the same
    gap, so this is parity rather than a new exposure, and the docs say so.

    The shape deliberately echoes Terraform's `resource_changes`, so a policy
    author moving between engines meets a familiar key rather than a new
    vocabulary for the same idea.
    """
    if not path.exists():
        return None

    summary: dict[str, Any] | None = None
    resources: list[dict[str, Any]] = []
    unparseable = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                unparseable += 1
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("summaryEvent"), dict):
                summary = event["summaryEvent"]
            elif isinstance(event.get("resourcePreEvent"), dict):
                metadata = event["resourcePreEvent"].get("metadata")
                if isinstance(metadata, dict):
                    resources.append(_policy_resource(metadata))

    if unparseable:
        logger.warning("pulumi event log had unparseable lines", lines=unparseable)
    if summary is None:
        # Same rule the digest follows: no summary event means the preview did
        # not finish, and a gate must not decide on a partial account of it.
        return None

    changes = summary.get("resourceChanges")
    changes = changes if isinstance(changes, dict) else {}
    return {
        "engine": "pulumi",
        "change_summary": {k: int(v) for k, v in changes.items() if isinstance(v, int)},
        "has_changes": has_changes(changes),
        "resource_changes": resources,
    }


def write_policy_input(policy_input: dict[str, Any], path: Path) -> Path:
    """Write the policy input where `opa eval --stdin-input` can read it."""
    path.write_text(json.dumps(policy_input, indent=2), encoding="utf-8")
    return path
