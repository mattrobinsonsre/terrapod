"""Autodiscovery rule CRUD (terrapod #283).

Connection-scoped rules that auto-create workspaces in monorepos when
a PR or default-branch push touches a path matching `pattern`. See
`docs/autodiscovery.md` for the rule schema and pattern semantics.

UX CONTRACT: consumed by the web frontend at
`web/src/app/admin/autodiscovery/page.tsx`. Changes to response shapes,
attribute names, or status codes MUST be matched there.

Endpoints:
    GET    /api/terrapod/v1/autodiscovery-rules                (list)
    POST   /api/terrapod/v1/autodiscovery-rules                (create)
    GET    /api/terrapod/v1/autodiscovery-rules/{id}           (show)
    PATCH  /api/terrapod/v1/autodiscovery-rules/{id}           (update)
    DELETE /api/terrapod/v1/autodiscovery-rules/{id}           (delete)
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
    if "ignore-patterns" in attrs:
        ip = attrs["ignore-patterns"]
        if not isinstance(ip, list) or not all(isinstance(p, str) for p in ip):
            raise HTTPException(status_code=422, detail="ignore-patterns must be a list of strings")
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
        labels = attrs["labels"]
        if not isinstance(labels, dict):
            raise HTTPException(status_code=422, detail="labels must be an object")
        out["labels"] = labels
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
