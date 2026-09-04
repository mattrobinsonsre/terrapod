"""Shared validation for labels on workspaces, agent pools, registry modules/providers.

Labels are arbitrary string→string maps used by the label-based RBAC system
and exposed in the workspace-list filter UI. To keep the filter language
unambiguous, a small set of label keys are reserved for *virtual* fields —
filter terms like `status:errored` resolve against a workspace's derived
status, not against a literal label called `status`. Allowing literal labels
with reserved keys would make the filter ambiguous.

This module is web-framework-agnostic: it raises `ValueError` on violations
so it can be called from FastAPI routers, CLI tools, migration scripts, or
background tasks. Routers translate `ValueError` to HTTP 422 via the wrapper
in `terrapod.api.labels`.

Keep `RESERVED_LABEL_KEYS` in lockstep with the filter parser in
`web/src/lib/workspace-filter.ts`. Each reserved key listed here either is
already implemented as a virtual filter term, or is reserved for a planned
one — see `docs/rbac.md` for the user-facing list.
"""

MAX_LABELS = 50
MAX_LABEL_KEY_LEN = 63
MAX_LABEL_VALUE_LEN = 255

# Reserved label keys. Two, and the line between them and the rest is
# deliberate: a key is reserved when using it as a label would MISLEAD ABOUT A
# DECISION THE SYSTEM ACTUALLY MAKES.
#
#   `status` — parsing. It is the only key the workspace filter bar treats as a
#     built-in virtual term (`parseFilterQuery` in
#     `web/src/lib/workspace-filter.ts` special-cases exactly this one), so a
#     literal `status` label would make `status:errored` ambiguous.
#   `owner`  — authorization. `workspace_rbac_service` passes `labels` and
#     `owner_email` into the SAME permission call as adjacent inputs, and
#     ownership grants `admin`. An operator writing `owner: alice` could
#     reasonably believe they had granted alice admin. They have not. That is a
#     security-shaped misunderstanding, and it is the only one in the set.
#
# This set held ten keys until v1.6.0 (#1450). The other eight — `pool`, `mode`,
# `backend`, `drift`, `version`, `vcs`, `locked`, `branch` — were reserved for
# merely DESCRIPTIVE overlap with a column, which is not enough to take a common
# word away from operators: `version` legitimately means an app or module
# version, not only `terraform_version`, and none of the eight was ever a
# built-in filter term, so each refused a label AND gave the filter nothing in
# return. `drift` and `locked` are answerable as `status:drifted` /
# `status:locked` anyway.
#
# The house convention for a NEW virtual facet is to ride `status:` as a value
# (`status:locked`, `status:unhealthy` both did) precisely so no new key has to
# be reserved. If one ever genuinely needs its own key it takes a distinct name
# (`pool-name:`) rather than breaking labels already in use.
#
# CHANGE-CONTROL: adding to this set is a behaviour change for any deployment
# already using the key as a label — removing from it is additive and safe.
# Update `docs/rbac.md` and the frontend filter parser comment either way.
RESERVED_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "status",  # virtual filter term (errored, needs-confirm, drifted, locked, …)
        "owner",  # real app concept: `owner_email`, which grants workspace admin
    }
)


class LabelValidationError(ValueError):
    """Raised when a labels payload fails shape, size, or reserved-key checks.

    A `ValueError` subclass so callers using a bare `except ValueError`
    still catch it; the explicit type lets routers translate to HTTP 422
    without catching unrelated `ValueError`s.
    """


def validate_labels(labels: dict | None) -> dict:
    """Validate labels: shape, size limits, and reserved-key check.

    Returns a clean dict (or {} for None/empty input). Raises
    `LabelValidationError` (a `ValueError`) with a user-readable message
    on any violation. The caller is responsible for translating to an
    HTTP status code — see `terrapod.api.labels.validate_labels_or_422`
    for the FastAPI helper.
    """
    if not labels:
        return {}
    if not isinstance(labels, dict):
        raise LabelValidationError("labels must be an object")
    if len(labels) > MAX_LABELS:
        raise LabelValidationError(f"labels cannot exceed {MAX_LABELS} entries")
    clean: dict[str, str] = {}
    for k, v in labels.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise LabelValidationError("label keys and values must be strings")
        if len(k) > MAX_LABEL_KEY_LEN:
            raise LabelValidationError(f"label key exceeds {MAX_LABEL_KEY_LEN} characters")
        if len(v) > MAX_LABEL_VALUE_LEN:
            raise LabelValidationError(f"label value exceeds {MAX_LABEL_VALUE_LEN} characters")
        if k in RESERVED_LABEL_KEYS:
            raise LabelValidationError(
                f'label key "{k}" is reserved for filter syntax. '
                f"Reserved keys: {', '.join(sorted(RESERVED_LABEL_KEYS))}."
            )
        clean[k] = v
    return clean


def sanitize_labels(labels: dict | None) -> tuple[dict[str, str], list[str]]:
    """Best-effort variant of `validate_labels` for non-interactive
    label-write paths that must not abort on bad input.

    Returns `(clean_labels, dropped_keys)` — entries that fail the
    shape/size/reserved checks are dropped rather than raising. Use this
    only where rejecting would break an automated flow (e.g. autodiscovery
    materialising a workspace from a rule that predates the create-time
    reserved-key guard, #316). Interactive create/update paths must keep
    using `validate_labels` so the operator is told to fix the input.
    """
    if not labels or not isinstance(labels, dict):
        return {}, []
    clean: dict[str, str] = {}
    dropped: list[str] = []
    for k, v in list(labels.items())[:MAX_LABELS]:
        if (
            not isinstance(k, str)
            or not isinstance(v, str)
            or len(k) > MAX_LABEL_KEY_LEN
            or len(v) > MAX_LABEL_VALUE_LEN
            or k in RESERVED_LABEL_KEYS
        ):
            dropped.append(str(k))
            continue
        clean[k] = v
    return clean, dropped
