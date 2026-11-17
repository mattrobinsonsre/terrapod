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
- `all: true` ignores the other positive dimensions; `exclude` still applies.
- `exclude` is a list of selectors (same dimensions, AND-combined within an
  entry). A workspace matching ANY entry is removed. `exclude` alone is never a
  selector, and entries cannot nest `exclude` or set `all`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    #: Named for the column it filters (#1559). `terraform_version` is still
    #: accepted on the wire and normalised to this by the router.
    engine_version: str | None = None
    agent_pool_id: str | None = None
    vcs_connection_id: str | None = None
    owner_email: str | None = None
    drift_status: str | None = None
    locked: bool | None = None
    has_vcs: bool | None = None
    all: bool = Field(default=False)
    exclude: list[WorkspaceFilter] = Field(default_factory=list)

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
                "engine_version",
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


def _dimension_clauses(f: WorkspaceFilter) -> list[ColumnElement[bool]]:
    """WHERE clauses for the positive dimensions of `f` (AND them together)."""
    clauses: list[ColumnElement[bool]] = []
    if f.workspace_ids is not None:
        ids = [_strip_uuid(w, "ws-") for w in f.workspace_ids]
        clauses.append(Workspace.id.in_(ids))
    if f.labels:
        for k, v in f.labels.items():
            clauses.append(Workspace.labels.contains({k: v}))
    if f.name_prefix:
        clauses.append(Workspace.name.ilike(_glob_to_like(f.name_prefix) + "%", escape="\\"))
    if f.name_glob:
        clauses.append(Workspace.name.ilike(_glob_to_like(f.name_glob), escape="\\"))
    if f.execution_backend is not None:
        clauses.append(Workspace.execution_backend == f.execution_backend)
    if f.execution_mode is not None:
        clauses.append(Workspace.execution_mode == f.execution_mode)
    if f.engine_version is not None:
        clauses.append(Workspace.engine_version == f.engine_version)
    if f.agent_pool_id is not None:
        # Matches a workspace that names this pool ANYWHERE in its pool set
        # (#1085) — "which workspaces would this pool have to run?" is the
        # question an operator is asking, and a workspace that lists the pool
        # as a fallback is just as much its responsibility.
        pool_uuid = _strip_uuid(f.agent_pool_id, "apool-")
        clauses.append(
            select(WorkspaceAgentPool.workspace_id)
            .where(
                WorkspaceAgentPool.workspace_id == Workspace.id,
                WorkspaceAgentPool.agent_pool_id == pool_uuid,
            )
            .exists()
        )
    if f.vcs_connection_id is not None:
        clauses.append(Workspace.vcs_connection_id == _strip_uuid(f.vcs_connection_id, "vcs-"))
    if f.owner_email is not None:
        clauses.append(Workspace.owner_email == f.owner_email)
    if f.drift_status is not None:
        clauses.append(Workspace.drift_status == f.drift_status)
    if f.locked is not None:
        clauses.append(Workspace.locked.is_(f.locked))
    if f.has_vcs is not None:
        if f.has_vcs:
            clauses.append(Workspace.vcs_connection_id.is_not(None))
        else:
            clauses.append(Workspace.vcs_connection_id.is_(None))
    return clauses


def _check_exclude_entry(entry: WorkspaceFilter, index: int) -> None:
    if entry.all or entry.exclude:
        raise WorkspaceFilterError(f"exclude[{index}] cannot set 'all' or nest 'exclude'")
    if not entry._explicit_dimensions():
        raise WorkspaceFilterError(f"exclude[{index}] sets no selector")


def build_workspace_query(f: WorkspaceFilter) -> Select[tuple[Workspace]]:
    """Build the workspace SELECT for a filter.

    Raises `WorkspaceFilterError` (→ HTTP 422) if the filter neither sets
    `all: true` nor provides at least one dimension — this is the only
    blast-radius guard: it stops a typo'd/empty filter from silently
    matching the whole fleet, while still letting an operator who means
    it pass `all: true`.
    """
    q: Select[tuple[Workspace]] = select(Workspace).order_by(Workspace.name)

    if not f.all:
        if not f._explicit_dimensions():
            raise WorkspaceFilterError(
                "filter must set at least one selector, or 'all': true to match every workspace"
            )
        for clause in _dimension_clauses(f):
            q = q.where(clause)

    if f.exclude:
        for i, entry in enumerate(f.exclude):
            _check_exclude_entry(entry, i)
        removed = or_(*[and_(*_dimension_clauses(e)) for e in f.exclude])
        # `IS NOT true` keeps rows where the match is NULL (nullable columns).
        q = q.where(removed.is_not(True))

    return q


@dataclass
class ExclusionReport:
    excluded: int
    #: Indexes of exclude entries that match no workspace anywhere in the fleet.
    empty: list[int]
    #: Per entry index, the `workspace_ids` it names (as written) that do not exist.
    missing_ids: dict[int, list[str]] = field(default_factory=dict)


async def _count(db: AsyncSession, q: Select[Any]) -> int:
    return (
        await db.execute(select(func.count()).select_from(q.order_by(None).subquery()))
    ).scalar_one()


async def exclusion_report(db: AsyncSession, f: WorkspaceFilter) -> ExclusionReport:
    """How many workspaces `f.exclude` removed, and which entries match nothing."""
    if not f.exclude:
        return ExclusionReport(excluded=0, empty=[])

    kept = await _count(db, build_workspace_query(f))
    before = await _count(db, build_workspace_query(f.model_copy(update={"exclude": []})))

    empty: list[int] = []
    for i, entry in enumerate(f.exclude):
        q = select(Workspace.id).where(*_dimension_clauses(entry))
        if await _count(db, q) == 0:
            empty.append(i)

    missing = await _missing_ids(db, f.exclude)
    empty = sorted(set(empty) | set(missing))
    return ExclusionReport(excluded=before - kept, empty=empty, missing_ids=missing)


async def _missing_ids(db: AsyncSession, entries: list[WorkspaceFilter]) -> dict[int, list[str]]:
    """Per entry index, the `workspace_ids` it names that match no workspace."""
    named = {i: e.workspace_ids for i, e in enumerate(entries) if e.workspace_ids}
    wanted = {_strip_uuid(w, "ws-") for ids in named.values() for w in ids}
    if not wanted:
        return {}
    found = set((await db.execute(select(Workspace.id).where(Workspace.id.in_(wanted)))).scalars())
    absent = {i: [w for w in ids if _strip_uuid(w, "ws-") not in found] for i, ids in named.items()}
    return {i: ids for i, ids in absent.items() if ids}


def _parse_entry(raw: Any, label: str | None = None) -> WorkspaceFilter:
    """Parse one filter dict; `label` (e.g. `exclude[0]`) marks an exclude entry."""
    prefix = f"{label}: " if label else ""
    if not isinstance(raw, dict):
        raise WorkspaceFilterError(f"{prefix}must be an object")
    norm = {k.replace("-", "_"): v for k, v in raw.items()}
    # `terraform-version` was this selector's name before the column became
    # `engine_version` (#1559). A stored rule -- a dynamic variable-set
    # assignment, a saved bulk-update selector -- keeps the old spelling, and
    # unknown keys are rejected, so dropping it would turn those rules into
    # errors. The canonical spelling wins where a caller sends both.
    legacy_version = norm.pop("terraform_version", None)
    if legacy_version is not None:
        norm.setdefault("engine_version", legacy_version)
    unknown = set(norm) - set(WorkspaceFilter.model_fields)
    if unknown:
        raise WorkspaceFilterError(f"{prefix}unknown filter key(s): {', '.join(sorted(unknown))}")
    if label:
        nested = {"all", "exclude"} & set(norm)
        if nested:
            raise WorkspaceFilterError(
                f"{prefix}'all' and 'exclude' are not allowed in an exclude entry"
            )
    else:
        raw_exclude = norm.pop("exclude", None)
        if raw_exclude is None:
            raw_exclude = []
        if not isinstance(raw_exclude, list):
            raise WorkspaceFilterError("'exclude' must be a list of selectors")
        norm["exclude"] = [_parse_entry(e, f"exclude[{i}]") for i, e in enumerate(raw_exclude)]
    try:
        entry = WorkspaceFilter(**norm)
    except Exception as e:  # pydantic validation
        raise WorkspaceFilterError(f"{prefix}{e}") from e
    if label and not entry._explicit_dimensions():
        raise WorkspaceFilterError(f"{label} sets no selector")
    return entry


def parse_filter(raw: dict[str, Any] | None) -> WorkspaceFilter:
    """Parse a JSON:API-ish filter dict (hyphenated keys accepted) into a
    `WorkspaceFilter`. Unknown keys are rejected so a typo'd selector
    can't silently widen the match.
    """
    if not raw or not isinstance(raw, dict):
        raise WorkspaceFilterError(
            "a non-empty 'filter' is required (use 'all': true to match all)"
        )
    return _parse_entry(raw)
