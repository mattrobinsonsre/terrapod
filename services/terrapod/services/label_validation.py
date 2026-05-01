"""Shared validation for labels on workspaces, agent pools, registry modules/providers.

Labels are arbitrary string→string maps used by the label-based RBAC system
and exposed in the workspace-list filter UI. To keep the filter language
unambiguous, a small set of label keys are reserved for *virtual* fields —
filter terms like `status:errored` resolve against a workspace's derived
status, not against a literal label called `status`. Allowing literal labels
with reserved keys would make the filter ambiguous.

Keep `RESERVED_LABEL_KEYS` in lockstep with the filter parser in
`web/src/lib/workspace-filter.ts`. Each reserved key listed here either is
already implemented as a virtual filter term, or is reserved for an upcoming
one — see `docs/rbac.md` for the user-facing list.
"""

from fastapi import HTTPException

MAX_LABELS = 50
MAX_LABEL_KEY_LEN = 63
MAX_LABEL_VALUE_LEN = 255

# Reserved label keys: derived workspace attributes that are (or will be)
# exposed as virtual filter fields.
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


def validate_labels(labels: dict | None) -> dict:
    """Validate labels: shape, size limits, and reserved-key check.

    Returns a clean dict (or {} for None/empty input). Raises HTTPException
    422 with a specific detail message on any violation.
    """
    if not labels:
        return {}
    if not isinstance(labels, dict):
        raise HTTPException(status_code=422, detail="labels must be an object")
    if len(labels) > MAX_LABELS:
        raise HTTPException(status_code=422, detail=f"labels cannot exceed {MAX_LABELS} entries")
    clean: dict[str, str] = {}
    for k, v in labels.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(status_code=422, detail="label keys and values must be strings")
        if len(k) > MAX_LABEL_KEY_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"label key exceeds {MAX_LABEL_KEY_LEN} characters",
            )
        if len(v) > MAX_LABEL_VALUE_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"label value exceeds {MAX_LABEL_VALUE_LEN} characters",
            )
        if k in RESERVED_LABEL_KEYS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f'label key "{k}" is reserved for filter syntax. '
                    f"Reserved keys: {', '.join(sorted(RESERVED_LABEL_KEYS))}."
                ),
            )
        clean[k] = v
    return clean
