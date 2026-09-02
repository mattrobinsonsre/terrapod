"""Server-side workspace search/selection (#318).

The UI's rich filter bar is client-side only (`web/src/lib/workspace-filter.ts`);
the only pre-existing server-side filtering was the go-tfe cloud-block
`search[tags]`/`search[name]` on the workspace list. This service is the
canonical server-side selector: a structured, AND-combined filter that
backs the bulk-update endpoint (and is reusable by a future UI migration).

Design notes:
- Dimensions are AND-combined — narrower is safer.
- An empty/omitted filter is a caller error (`WorkspaceFilterError`), never
  an implicit match-all. Matching the whole fleet requires explicit
  `all: true` (operators are trusted to ask for it on purpose).
- `all: true` short-circuits — other dimensions are ignored.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Select, select

from terrapod.db.models import Workspace, WorkspaceAgentPool


class WorkspaceFilterError(ValueError):
    """Raised when a filter is empty or structurally invalid. Routers
    translate this to HTTP 422.
    """


def _strip_uuid(value: str, prefix: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value).removeprefix(prefix))
    except ValueError as e:
        raise WorkspaceFilterError(f"{value!r} is not a valid id") from e


def _glob_to_like(pattern: str) -> str:
    """Translate a simple `*`/`?` glob to a SQL LIKE pattern, escaping
    LIKE metacharacters in the literal portions.
    """
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in ("%", "_", "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


#: Dimensions whose query clause is gated on truthiness rather than
#: `is not None` — a blank value contributes no WHERE clause, so counting it as
#: a selector lets an otherwise-empty filter match the whole estate.
_BLANK_IS_NOT_A_SELECTOR = {"name_prefix", "name_glob"}


class WorkspaceFilter(BaseModel):
    """Structured workspace selector. All present dimensions are
    AND-combined unless `all` is true.
    """

    workspace_ids: list[str] | None = None
    labels: dict[str, str] | None = None
    name_prefix: str | None = None
    name_glob: str | None = None
    execution_backend: str | None = None
    execution_mode: str | None = None
    terraform_version: str | None = None
    agent_pool_id: str | None = None
    vcs_connection_id: str | None = None
    owner_email: str | None = None
    drift_status: str | None = None
    locked: bool | None = None
    has_vcs: bool | None = None
    all: bool = Field(default=False)

    def _explicit_dimensions(self) -> list[str]:
        return [
            k
            for k in (
                "workspace_ids",
                "labels",
                "name_prefix",
                "name_glob",
                "execution_backend",
                "execution_mode",
                "terraform_version",
                "agent_pool_id",
                "vcs_connection_id",
                "owner_email",
                "drift_status",
                "locked",
                "has_vcs",
            )
            # A blank value counts as a dimension ONLY where the builder still
            # emits a clause for it. `name_prefix` and `name_glob` are gated on
            # truthiness (`if f.name_prefix:`), so a blank one produced a filter
            # that passed the "at least one selector" check and then built a
            # query with NO WHERE clause — matching every workspace.
            #
            # The rest are gated on `is not None`, and a blank value is a real
            # selector there: `owner_email` is NOT NULL defaulting to "", so
            # `{"owner_email": ""}` is the only way to select unowned
            # workspaces. Excluding it wholesale would have rejected a payload
            # that worked before — a breaking change to the bulk-update API.
            if getattr(self, k) not in (None, [], {})
            and not (k in _BLANK_IS_NOT_A_SELECTOR and getattr(self, k) == "")
        ]


def build_workspace_query(f: WorkspaceFilter) -> Select[tuple[Workspace]]:
    """Build the workspace SELECT for a filter.

    Raises `WorkspaceFilterError` (→ HTTP 422) if the filter neither sets
    `all: true` nor provides at least one dimension — this is the only
    blast-radius guard: it stops a typo'd/empty filter from silently
    matching the whole fleet, while still letting an operator who means
    it pass `all: true`.
    """
    q: Select[tuple[Workspace]] = select(Workspace).order_by(Workspace.name)

    if f.all:
        return q

    dims = f._explicit_dimensions()
    if not dims:
        raise WorkspaceFilterError(
            "filter must set at least one selector, or 'all': true to match every workspace"
        )

    if f.workspace_ids is not None:
        ids = [_strip_uuid(w, "ws-") for w in f.workspace_ids]
        q = q.where(Workspace.id.in_(ids))
    if f.labels:
        for k, v in f.labels.items():
            q = q.where(Workspace.labels.contains({k: v}))
    if f.name_prefix:
        q = q.where(Workspace.name.ilike(_glob_to_like(f.name_prefix) + "%", escape="\\"))
    if f.name_glob:
        q = q.where(Workspace.name.ilike(_glob_to_like(f.name_glob), escape="\\"))
    if f.execution_backend is not None:
        q = q.where(Workspace.execution_backend == f.execution_backend)
    if f.execution_mode is not None:
        q = q.where(Workspace.execution_mode == f.execution_mode)
    if f.terraform_version is not None:
        q = q.where(Workspace.terraform_version == f.terraform_version)
    if f.agent_pool_id is not None:
        # Matches a workspace that names this pool ANYWHERE in its pool set
        # (#1085) — "which workspaces would this pool have to run?" is the
        # question an operator is asking, and a workspace that lists the pool
        # as a fallback is just as much its responsibility.
        pool_uuid = _strip_uuid(f.agent_pool_id, "apool-")
        q = q.where(
            select(WorkspaceAgentPool.workspace_id)
            .where(
                WorkspaceAgentPool.workspace_id == Workspace.id,
                WorkspaceAgentPool.agent_pool_id == pool_uuid,
            )
            .exists()
        )
    if f.vcs_connection_id is not None:
        q = q.where(Workspace.vcs_connection_id == _strip_uuid(f.vcs_connection_id, "vcs-"))
    if f.owner_email is not None:
        q = q.where(Workspace.owner_email == f.owner_email)
    if f.drift_status is not None:
        q = q.where(Workspace.drift_status == f.drift_status)
    if f.locked is not None:
        q = q.where(Workspace.locked.is_(f.locked))
    if f.has_vcs is not None:
        if f.has_vcs:
            q = q.where(Workspace.vcs_connection_id.is_not(None))
        else:
            q = q.where(Workspace.vcs_connection_id.is_(None))

    return q


def parse_filter(raw: dict[str, Any] | None) -> WorkspaceFilter:
    """Parse a JSON:API-ish filter dict (hyphenated keys accepted) into a
    `WorkspaceFilter`. Unknown keys are rejected so a typo'd selector
    can't silently widen the match.
    """
    if not raw or not isinstance(raw, dict):
        raise WorkspaceFilterError(
            "a non-empty 'filter' is required (use 'all': true to match all)"
        )
    norm = {k.replace("-", "_"): v for k, v in raw.items()}
    allowed = set(WorkspaceFilter.model_fields)
    unknown = set(norm) - allowed
    if unknown:
        raise WorkspaceFilterError(f"unknown filter key(s): {', '.join(sorted(unknown))}")
    try:
        return WorkspaceFilter(**norm)
    except WorkspaceFilterError:
        raise
    except Exception as e:  # pydantic validation
        raise WorkspaceFilterError(str(e)) from e
