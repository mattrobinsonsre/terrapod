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

# Reserved label keys: derived workspace attributes that are (or will be)
# exposed as virtual filter fields. We're aggressive about reservations
# because the cost of reserving a key today is near-zero (no virtual
# implementation required) but the cost of NOT reserving and later wanting
# the key as a virtual is a migration. Boolean predicates (`vcs`, `locked`)
# will resolve as `key:true` / `key:false` when implemented; `version` will
# match against `terraform_version` once we have a value comparison plan.
#
# CHANGE-CONTROL: adding to this set is a behaviour change for any deployment
# that already has labels with the new key. Update `docs/rbac.md` and the
# frontend filter parser comment when extending.
RESERVED_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "status",  # derived run status (errored, needs-confirm, drifted, …)
        "pool",  # agent_pool_name
        "mode",  # execution_mode (local/agent)
        "backend",  # execution_backend (tofu/terraform)
        "owner",  # owner_email
        "drift",  # drift_status
        "version",  # terraform_version
        "vcs",  # has VCS connection
        "locked",  # locked boolean
        "branch",  # vcs_branch
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
