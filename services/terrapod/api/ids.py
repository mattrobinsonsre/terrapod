"""Typed resource ids, accepted in either spelling.

The API emits typed-prefixed ids -- `ws-{uuid}`, `run-{uuid}`, `apool-{uuid}`
-- and the house style says an endpoint accepts the id "both prefixed and raw
on input where practical". Historically that was applied one call site at a
time, which left two problems this module exists to end:

1.  **A bad id was reported as a server fault.** `uuid.UUID(x)` raises
    `ValueError`, and where the call was not wrapped, the global handler in
    `app.py` turned it into `500 Internal server error`. Nothing had failed;
    the input simply was not a uuid.

2.  **Some endpoints accepted one spelling and not the other**, so an id
    copied out of the UI or an MCP response failed against a different
    endpoint of the same API -- which is what the convention was introduced
    to prevent in the first place (#1299).

Strictly additive, and that constraint shapes the design:

*   **The set of accepted inputs only grows.** Every id that worked before
    still works, spelled exactly as before.
*   **Emitted ids do not change at all.** Serializers keep their existing
    prefixes; nothing that reads an id out of a response sees any difference.
*   **An existing client-error status is never changed.** Where an endpoint
    already answers 400 or 422 for a bad id, it still does -- hence the
    `status` argument rather than a hardcoded 404. Only the 500s move, and
    each moves to whatever its own router already returns for the same
    mistake. Converging all four statuses on one answer would be a behaviour
    change, not a fix, and no contract gate would catch it: the snapshots pin
    routes and attribute names, not status codes.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

#: Every typed id prefix the API emits, keyed by the JSON:API resource type.
#:
#: `roles._ACCESS_KINDS` and `role_reach_service._ID_PREFIX` each hold a
#: partial copy of this, and both record `""` for the registry and catalog
#: resources -- which is why `/registry-modules/{id}/access` rejects a
#: prefixed id while the otherwise identical `/workspaces/{id}/access`
#: accepts one.
#:
#: A resource whose ids are emitted bare has no entry here. `parse_id` is
#: still the right way to read one: the prefix tolerance is then a no-op, and
#: the point is the error handling.
ID_PREFIXES: dict[str, str] = {
    "agent-pools": "apool-",
    "applies": "apply-",
    "configuration-versions": "cv-",
    "execution-hooks": "hook-",
    "listeners": "listener-",
    "notification-configurations": "nc-",
    "plans": "plan-",
    "policies": "pol-",
    "policy-evaluations": "pe-",
    "policy-sets": "polset-",
    "registry-module-versions": "modver-",
    "remote-state-consumers": "rsc-",
    "run-tasks": "task-",
    "run-triggers": "rt-",
    "runs": "run-",
    "security-scans": "ss-",
    "slack-links": "slk-",
    "state-versions": "sv-",
    "task-stage-results": "tsr-",
    "task-stages": "ts-",
    "variable-sets": "varset-",
    "variables": "var-",
    "vcs-connections": "vcs-",
    "workspaces": "ws-",
}


def strip_id_prefix(value: str, *prefixes: str) -> str:
    """The id with any one of `prefixes` removed.

    Several prefixes may be given where more than one names the same row: a
    plan and an apply are both views of a run, so `plan-{uuid}`, `apply-{uuid}`
    and `run-{uuid}` all identify it and all must be accepted.

    Only the first matching prefix is removed. Stripping repeatedly would
    accept nonsense like `ws-ws-{uuid}`, and would corrupt an id that
    legitimately began with the prefix's letters.
    """
    if not isinstance(value, str):
        return value
    for prefix in prefixes:
        if prefix and value.startswith(prefix):
            return value[len(prefix) :]
    return value


def parse_id(
    value: str,
    *prefixes: str,
    detail: str = "Resource not found",
    status: int = 404,
) -> uuid.UUID:
    """Parse a typed or bare id, or raise an HTTP error.

    This is the only place in the API that should turn a caller-supplied id
    string into a `uuid.UUID`. Calling `uuid.UUID(...)` directly on request
    input is what produced the 500s.

        run_uuid = parse_id(run_id, "run-", "plan-", detail="Run not found")

    `status` exists so a call site keeps the answer it already gives. Pass the
    status that endpoint returns today; do not "tidy" it to 404, which would
    change behaviour for a caller that handles the current one.
    """
    raw = strip_id_prefix(value, *prefixes)
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        # AttributeError and TypeError cover a caller that passed something
        # other than a string -- a body field that arrived as a list or an
        # int. Still a bad request, still not a server fault.
        raise HTTPException(status_code=status, detail=detail) from None


def parse_id_for(
    value: str,
    resource_type: str,
    *,
    detail: str | None = None,
    status: int = 404,
) -> uuid.UUID:
    """`parse_id`, taking the prefix from `ID_PREFIXES` by resource type.

    Preferred where the resource type is to hand, because it cannot drift from
    the table the serializers use.
    """
    prefix = ID_PREFIXES.get(resource_type, "")
    return parse_id(value, prefix, detail=detail or f"{resource_type} not found", status=status)
