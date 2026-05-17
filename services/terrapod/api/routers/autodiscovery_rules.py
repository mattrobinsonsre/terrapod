"""Autodiscovery rule CRUD (terrapod #283).

Connection-scoped rules that auto-create workspaces in monorepos when
a PR or default-branch push touches a path matching `pattern`. See
`docs/autodiscovery.md` for the rule schema and pattern semantics.

UX CONTRACT: consumed by the web frontend at
`web/src/app/admin/autodiscovery/page.tsx`. Changes to response shapes,
attribute names, or status codes MUST be matched there.

Endpoints:
    GET    /api/terrapod/v1/autodiscovery-rules                  (list)
    POST   /api/terrapod/v1/autodiscovery-rules                  (create)
    POST   /api/terrapod/v1/autodiscovery-rules/preview          (dry-run unsaved rule)
    GET    /api/terrapod/v1/autodiscovery-rules/{id}             (show)
    PATCH  /api/terrapod/v1/autodiscovery-rules/{id}             (update)
    DELETE /api/terrapod/v1/autodiscovery-rules/{id}             (delete)
    GET    /api/terrapod/v1/autodiscovery-rules/{id}/preview     (dry-run saved rule)
    POST   /api/terrapod/v1/autodiscovery-rules/{id}/scan        (on-demand scan)
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.labels import validate_labels
from terrapod.db.models import AgentPool, AutodiscoveryRule, VCSConnection
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(tags=["autodiscovery-rules"])
logger = get_logger(__name__)

_VALID_EXEC_MODES = frozenset({"agent"})
_VALID_BACKENDS = frozenset({"tofu", "terraform"})


def _rfc3339(dt) -> str:  # noqa: ANN001
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rule_json(rule: AutodiscoveryRule) -> dict:
    """Serialize an AutodiscoveryRule to JSON:API."""
    return {
        "id": str(rule.id),
        "type": "autodiscovery-rules",
        "attributes": {
            "name": rule.name,
            "name-template": rule.name_template,
            "vcs-connection-id": str(rule.vcs_connection_id),
            "repo-url": rule.repo_url,
            "branch": rule.branch,
            "pattern": rule.pattern,
            "ignore-patterns": list(rule.ignore_patterns or []),
            "enabled": rule.enabled,
            "execution-mode": rule.execution_mode,
            "execution-backend": rule.execution_backend,
            "agent-pool-id": str(rule.agent_pool_id) if rule.agent_pool_id else None,
            "terraform-version": rule.terraform_version,
            "resource-cpu": rule.resource_cpu,
            "resource-memory": rule.resource_memory,
            "auto-apply": rule.auto_apply,
            "labels": dict(rule.labels or {}),
            "owner-email": rule.owner_email or "",
            "created-at": _rfc3339(rule.created_at),
            "updated-at": _rfc3339(rule.updated_at),
        },
        "links": {"self": f"/api/terrapod/v1/autodiscovery-rules/{rule.id}"},
    }


def _strip_uuid_prefix(s: str, prefix: str) -> uuid.UUID:
    """Parse `<prefix>{uuid}` or a bare uuid; raises on invalid."""
    raw = s.removeprefix(prefix)
    return uuid.UUID(raw)


async def _validate_connection(db: AsyncSession, connection_id: uuid.UUID) -> VCSConnection:
    conn = await db.get(VCSConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=422, detail="vcs-connection-id not found")
    return conn


async def _validate_pool(db: AsyncSession, pool_id: uuid.UUID | None) -> None:
    if pool_id is None:
        return
    pool = await db.get(AgentPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=422, detail="agent-pool-id not found")


def _reject_directory_pattern(pattern: str, *, field: str) -> None:
    """Reject patterns that end in `/`.

    File-path matching only sees file paths (`foo/bar/main.tf`), which
    never end in `/`. A trailing-slash glob like `foo/*/` therefore
    matches zero files and silently no-ops — the user typically wants
    `foo/*/*.tf` (one-level subdir + tf file) or `foo/**` (any depth).
    Flagging at rule-create time turns the silent-no-op into a clear
    422 so the next user doesn't trip over the same gotcha. See #309.
    """
    if pattern.endswith("/"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} '{pattern}' ends in '/', which only matches "
                "directory paths — autodiscovery evaluates file paths, "
                "so this pattern would never match. Drop the trailing "
                "slash and either spell out a file glob (e.g. "
                f"'{pattern.rstrip('/')}/*.tf') or widen with '**' "
                f"(e.g. '{pattern.rstrip('/')}/**')."
            ),
        )


def _coerce_attrs(attrs: dict, *, on_create: bool) -> dict[str, Any]:
    """Normalise + validate request attributes. Returns a dict suitable
    for `setattr` onto a model.
    """
    out: dict[str, Any] = {}

    # Required on create
    required = ("name", "vcs-connection-id", "repo-url", "pattern") if on_create else ()
    for k in required:
        if (
            not (attrs.get(k) or "").strip()
            if isinstance(attrs.get(k), str)
            else attrs.get(k) is None
        ):
            raise HTTPException(status_code=422, detail=f"{k} is required")

    if "name" in attrs:
        out["name"] = str(attrs["name"]).strip()
        if not out["name"]:
            raise HTTPException(status_code=422, detail="name must be non-empty")
    if "name-template" in attrs:
        out["name_template"] = str(attrs["name-template"] or "")
    if "vcs-connection-id" in attrs:
        try:
            out["vcs_connection_id"] = _strip_uuid_prefix(str(attrs["vcs-connection-id"]), "vcs-")
        except ValueError as e:
            raise HTTPException(status_code=422, detail="vcs-connection-id is not a UUID") from e
    if "repo-url" in attrs:
        out["repo_url"] = str(attrs["repo-url"]).strip()
        if not out["repo_url"]:
            raise HTTPException(status_code=422, detail="repo-url must be non-empty")
    if "branch" in attrs:
        out["branch"] = str(attrs["branch"] or "")
    if "pattern" in attrs:
        out["pattern"] = str(attrs["pattern"]).strip()
        if not out["pattern"]:
            raise HTTPException(status_code=422, detail="pattern must be non-empty")
        _reject_directory_pattern(out["pattern"], field="pattern")
    if "ignore-patterns" in attrs:
        ip = attrs["ignore-patterns"]
        if not isinstance(ip, list) or not all(isinstance(p, str) for p in ip):
            raise HTTPException(status_code=422, detail="ignore-patterns must be a list of strings")
        for p in ip:
            _reject_directory_pattern(p, field="ignore-patterns")
        out["ignore_patterns"] = ip
    if "enabled" in attrs:
        out["enabled"] = bool(attrs["enabled"])
    if "execution-mode" in attrs:
        em = str(attrs["execution-mode"])
        if em not in _VALID_EXEC_MODES:
            # Autodiscovery is inherently VCS-driven; "local" execution
            # mode would create zombie workspaces with queued runs and
            # no executor.
            raise HTTPException(
                status_code=422,
                detail="execution-mode must be 'agent' (autodiscovery is VCS-driven; local-mode workspaces have no executor for queued runs)",
            )
        out["execution_mode"] = em
    if "execution-backend" in attrs:
        eb = str(attrs["execution-backend"])
        if eb not in _VALID_BACKENDS:
            raise HTTPException(
                status_code=422,
                detail=f"execution-backend must be one of {sorted(_VALID_BACKENDS)}",
            )
        out["execution_backend"] = eb
    if "agent-pool-id" in attrs:
        v = attrs["agent-pool-id"]
        if v in (None, ""):
            out["agent_pool_id"] = None
        else:
            try:
                out["agent_pool_id"] = _strip_uuid_prefix(str(v), "apool-")
            except ValueError as e:
                raise HTTPException(status_code=422, detail="agent-pool-id is not a UUID") from e
    if "terraform-version" in attrs:
        out["terraform_version"] = str(attrs["terraform-version"])
    if "resource-cpu" in attrs:
        out["resource_cpu"] = str(attrs["resource-cpu"])
    if "resource-memory" in attrs:
        out["resource_memory"] = str(attrs["resource-memory"])
    if "auto-apply" in attrs:
        out["auto_apply"] = bool(attrs["auto-apply"])
    if "labels" in attrs:
        # Reserved-key guard at the source: a rule's labels are copied
        # verbatim onto every workspace it materialises, and workspace
        # PATCH re-validates labels — so a reserved key here (e.g.
        # "owner") would create workspaces that are uneditable in the UI
        # (#316). Reject it when the rule is defined instead.
        out["labels"] = validate_labels(attrs["labels"])
    if "owner-email" in attrs:
        out["owner_email"] = (str(attrs["owner-email"]) or "").strip() or None

    return out


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/autodiscovery-rules")
async def list_rules(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all autodiscovery rules. Admin only."""
    result = await db.execute(
        select(AutodiscoveryRule).order_by(AutodiscoveryRule.created_at.desc())
    )
    rules = result.scalars().all()
    return JSONResponse(content={"data": [_rule_json(r) for r in rules]})


# ── Create ───────────────────────────────────────────────────────────────


@router.post("/autodiscovery-rules", status_code=201)
async def create_rule(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an autodiscovery rule. Admin only."""
    attrs = body.get("data", {}).get("attributes", {})
    fields = _coerce_attrs(attrs, on_create=True)

    await _validate_connection(db, fields["vcs_connection_id"])
    await _validate_pool(db, fields.get("agent_pool_id"))

    rule = AutodiscoveryRule(**fields)
    db.add(rule)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An autodiscovery rule with that name already exists for this connection",
        ) from exc
    await db.refresh(rule)
    logger.info(
        "Autodiscovery rule created",
        rule_id=str(rule.id),
        rule_name=rule.name,
        connection_id=str(rule.vcs_connection_id),
        repo_url=rule.repo_url,
        actor=user.email,
    )
    return JSONResponse(status_code=201, content={"data": _rule_json(rule)})


# ── Show ─────────────────────────────────────────────────────────────────


@router.get("/autodiscovery-rules/{rule_id}")
async def show_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show one autodiscovery rule. Admin only."""
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found") from None
    rule = await db.get(AutodiscoveryRule, rid)
    if rule is None:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found")
    return JSONResponse(content={"data": _rule_json(rule)})


# ── Update ───────────────────────────────────────────────────────────────


@router.patch("/autodiscovery-rules/{rule_id}")
async def update_rule(
    rule_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update an autodiscovery rule. Admin only."""
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found") from None
    rule = await db.get(AutodiscoveryRule, rid)
    if rule is None:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found")

    attrs = body.get("data", {}).get("attributes", {})
    fields = _coerce_attrs(attrs, on_create=False)
    if "vcs_connection_id" in fields:
        await _validate_connection(db, fields["vcs_connection_id"])
    if "agent_pool_id" in fields:
        await _validate_pool(db, fields["agent_pool_id"])
    # A disable → enable transition should re-scan the repo: the rule
    # may have missed changes during the disabled window, so treat the
    # re-enable like a fresh rule. NULL `first_scan_at` is the trigger
    # the poll cycle uses for a full-tree walk (#309).
    if "enabled" in fields and fields["enabled"] and not rule.enabled:
        fields["first_scan_at"] = None
    for k, v in fields.items():
        setattr(rule, k, v)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An autodiscovery rule with that name already exists for this connection",
        ) from exc
    await db.refresh(rule)
    logger.info(
        "Autodiscovery rule updated",
        rule_id=str(rule.id),
        actor=user.email,
        changed=sorted(fields.keys()),
    )
    return JSONResponse(content={"data": _rule_json(rule)})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/autodiscovery-rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete an autodiscovery rule. Admin only.

    Workspaces auto-created by this rule keep working — their
    `autodiscovery_rule_id` foreign key is set to NULL on cascade. Future
    poll cycles won't create more workspaces under this rule.
    """
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found") from None
    rule = await db.get(AutodiscoveryRule, rid)
    if rule is None:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found")
    await db.delete(rule)
    await db.commit()
    logger.info(
        "Autodiscovery rule deleted",
        rule_id=rule_id,
        rule_name=rule.name,
        actor=user.email,
    )
    return Response(status_code=204)


# ── Preview / on-demand scan (#311) ──────────────────────────────────────


def _build_transient_rule(fields: dict[str, Any], conn: VCSConnection) -> AutodiscoveryRule:
    """Build an AutodiscoveryRule with the supplied attributes and the
    resolved connection, but never `db.add()` it. The transient rule is
    fed to `_walk_repo_for_rule` + `preview_for_paths` the same way a
    persisted rule would be; nothing is committed.

    Used by `POST /autodiscovery-rules/preview` so operators can iterate
    on pattern + name_template + ignore_patterns against a real repo
    walk *before* saving (otherwise the bug-fix initial-scan path from
    v0.23.4 starts materialising workspaces immediately on save, defeating
    the purpose of a dry-run).
    """
    rule = AutodiscoveryRule(
        # The transient rule still needs an id so `preview_for_paths`
        # can flag the `existing_autodiscovered` case meaningfully when
        # the operator already has a saved rule against the same repo.
        id=uuid.uuid4(),
        name=fields.get("name", "preview"),
        vcs_connection_id=conn.id,
        repo_url=fields["repo_url"],
        branch=fields.get("branch", ""),
        pattern=fields["pattern"],
        ignore_patterns=fields.get("ignore_patterns", []),
        name_template=fields.get("name_template", ""),
        enabled=True,
        # Template fields don't affect the preview itself but the model
        # has NOT NULL constraints on some, so populate with reasonable
        # defaults to satisfy the in-memory construction.
        execution_mode=fields.get("execution_mode", "agent"),
        execution_backend=fields.get("execution_backend", "tofu"),
        terraform_version=fields.get("terraform_version", "1.12"),
        resource_cpu=fields.get("resource_cpu", "1"),
        resource_memory=fields.get("resource_memory", "2Gi"),
        auto_apply=fields.get("auto_apply", False),
        labels=fields.get("labels", {}),
        owner_email=fields.get("owner_email"),
        agent_pool_id=fields.get("agent_pool_id"),
    )
    # `_walk_repo_for_rule` reads `rule.vcs_connection`; set it directly
    # since the rule was never loaded through the ORM relationship.
    rule.vcs_connection = conn
    return rule


@router.post("/autodiscovery-rules/preview")
async def preview_unsaved_rule(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Dry-run for a *prospective* rule — no persistence.

    Body is the same JSON:API attribute shape as `POST /autodiscovery-rules`.
    Validates with the same `_coerce_attrs` path so any validation error
    (trailing-slash pattern, unknown agent pool, etc.) shows up at preview
    time, before the operator commits to creating the rule.

    Admin only.
    """
    from terrapod.services import workspace_autodiscovery_service

    attrs = body.get("data", {}).get("attributes", {})
    fields = _coerce_attrs(attrs, on_create=True)

    # Same connection / pool validation the create path runs.
    conn = await _validate_connection(db, fields["vcs_connection_id"])
    await _validate_pool(db, fields.get("agent_pool_id"))

    rule = _build_transient_rule(fields, conn)
    file_paths, target_branch, _head_sha = await _walk_repo_for_rule(rule)
    preview = await workspace_autodiscovery_service.preview_for_paths(db, rule, file_paths)
    return JSONResponse(
        content={
            "data": {
                "type": "autodiscovery-rule-previews",
                "attributes": {
                    "ref": target_branch,
                    "files-walked": len(file_paths),
                    "entries": preview,
                },
            }
        }
    )


async def _walk_repo_for_rule(rule: AutodiscoveryRule) -> tuple[list[str], str]:
    """Resolve the rule's target branch and return every file path in the
    repo at that branch.

    Lifted out so /preview and /scan share one provider-touching call.
    Raises HTTPException on any user-facing failure (bad repo URL, can't
    reach the provider, provider truncated the tree). Returns
    (file_paths, resolved_branch) on success.
    """
    # Imports local to keep the router's import surface narrow.
    from terrapod.services import github_service, gitlab_service

    conn = rule.vcs_connection
    if conn is None:
        raise HTTPException(
            status_code=422,
            detail="rule has no VCS connection — cannot scan",
        )

    if conn.provider == "gitlab":
        owner_repo = gitlab_service.parse_repo_url(rule.repo_url)
    elif conn.provider == "github":
        owner_repo = github_service.parse_repo_url(rule.repo_url)
    else:
        raise HTTPException(
            status_code=422,
            detail=f"unknown VCS provider: {conn.provider!r}",
        )
    if owner_repo is None:
        raise HTTPException(
            status_code=422,
            detail=f"cannot parse repo URL: {rule.repo_url!r}",
        )
    owner, repo = owner_repo

    # Resolve the rule's target branch — default-branch lookup if unset.
    target_branch = rule.branch
    if not target_branch:
        try:
            if conn.provider == "gitlab":
                target_branch = await gitlab_service.get_default_branch(conn, owner, repo) or ""
            else:
                target_branch = (
                    await github_service.get_repo_default_branch(conn, owner, repo) or ""
                )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"failed to resolve default branch from VCS provider: {exc}",
            ) from exc
        if not target_branch:
            raise HTTPException(
                status_code=502,
                detail="VCS provider returned no default branch",
            )

    try:
        if conn.provider == "gitlab":
            file_paths = await gitlab_service.list_repo_tree(conn, owner, repo, target_branch)
        else:
            file_paths = await github_service.list_repo_tree(conn, owner, repo, target_branch)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to list repository tree: {exc}",
        ) from exc

    if file_paths is None:
        # Provider truncated (GitHub > 100k entries, GitLab > 20k).
        # Distinct from a regular error so the UI can render a specific
        # "too large to scan" hint.
        raise HTTPException(
            status_code=413,
            detail=(
                "repository tree was truncated by the VCS provider — "
                "the repo is too large to scan in one pass. "
                "Autodiscovery will continue to pick up changes as they "
                "land on the tracked branch."
            ),
        )

    # Resolve the tracked-branch HEAD so /scan can baseline new
    # workspaces (#313) — same rationale as the poller. None on
    # failure: callers fall back to the prior NULL-seed behaviour.
    try:
        if conn.provider == "gitlab":
            head_sha = await gitlab_service.get_branch_sha(conn, owner, repo, target_branch)
        else:
            head_sha = await github_service.get_repo_branch_sha(conn, owner, repo, target_branch)
    except Exception:
        head_sha = None

    return file_paths, target_branch, head_sha


@router.get("/autodiscovery-rules/{rule_id}/preview")
async def preview_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Dry-run: walk the repo and return what *would* be created.

    No side effects. Admin only. Used by the UI's "Preview" action to
    let operators see which workspaces a rule would materialise before
    letting it loose on a monorepo.
    """
    from terrapod.services import workspace_autodiscovery_service

    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found") from None
    rule = await db.get(AutodiscoveryRule, rid)
    if rule is None:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found")

    file_paths, target_branch, _head_sha = await _walk_repo_for_rule(rule)
    preview = await workspace_autodiscovery_service.preview_for_paths(db, rule, file_paths)
    return JSONResponse(
        content={
            "data": {
                "type": "autodiscovery-rule-previews",
                "attributes": {
                    "ref": target_branch,
                    "files-walked": len(file_paths),
                    "entries": preview,
                },
            }
        }
    )


@router.post("/autodiscovery-rules/{rule_id}/scan")
async def scan_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """On-demand full-repo scan + materialise.

    Same walk as `/preview` but actually creates workspaces via the
    existing `autodiscover_for_paths` machinery (idempotent, collision-
    safe). Returns counts so the UI can show a clean confirmation.

    Works regardless of the rule's `enabled` flag — this is an explicit
    operator action, not a polling consequence. Admin only.
    """
    from terrapod.services import workspace_autodiscovery_service

    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found") from None
    rule = await db.get(AutodiscoveryRule, rid)
    if rule is None:
        raise HTTPException(status_code=404, detail="autodiscovery rule not found")

    file_paths, target_branch, head_sha = await _walk_repo_for_rule(rule)
    # autodiscover_for_paths skips rules with enabled=False; force-enable
    # locally for the duration of this call so the explicit /scan action
    # doesn't silently no-op on a disabled rule. The returned list is
    # *newly-created* workspaces only — existing rows that the rule
    # would map to are bound silently and excluded from the result.
    original_enabled = rule.enabled
    rule.enabled = True
    try:
        created = await workspace_autodiscovery_service.autodiscover_for_paths(
            db, [rule], file_paths, baseline_sha=head_sha
        )
    finally:
        rule.enabled = original_enabled
    await db.commit()
    logger.info(
        "Autodiscovery on-demand scan complete",
        rule_id=rule_id,
        rule_name=rule.name,
        ref=target_branch,
        files_walked=len(file_paths),
        workspaces_created=len(created),
        actor=user.email,
    )
    return JSONResponse(
        content={
            "data": {
                "type": "autodiscovery-rule-scans",
                "attributes": {
                    "ref": target_branch,
                    "files-walked": len(file_paths),
                    "workspaces-created": len(created),
                    "workspace-ids": [f"ws-{ws.id}" for ws in created],
                },
            }
        }
    )
