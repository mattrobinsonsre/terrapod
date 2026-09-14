"""Module autodiscovery rules (#1584).

The module registry's counterpart to `autodiscovery_rules.py`: rules that find
modules in a repository — the root and any submodules — and register them. See
`services/module_autodiscovery_service.py` for the matching and registration,
and `docs/registry.md` for the operator's view.

UX CONTRACT: consumed by `web/src/app/admin/module-autodiscovery/page.tsx` and
the Discover panel on `web/src/app/registry/modules/page.tsx`. Changes to
response shapes, attribute names or status codes MUST be matched there.

Endpoints (all platform admin — a rule reads repositories with the platform's
VCS credentials, and registers modules on its own authority):
    GET    /api/terrapod/v1/module-autodiscovery-rules               (list)
    POST   /api/terrapod/v1/module-autodiscovery-rules               (create)
    POST   /api/terrapod/v1/module-autodiscovery-rules/preview       (dry-run unsaved rule)
    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}          (show)
    PATCH  /api/terrapod/v1/module-autodiscovery-rules/{id}          (update)
    DELETE /api/terrapod/v1/module-autodiscovery-rules/{id}          (delete)
    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}/preview  (dry-run saved rule)
    POST   /api/terrapod/v1/module-autodiscovery-rules/{id}/scan     (register all or a subset)
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.labels import validate_labels
from terrapod.api.pagination import paginate
from terrapod.api.routers.autodiscovery_rules import _reject_directory_pattern
from terrapod.api.serialization import rfc3339
from terrapod.db.models import ModuleAutodiscoveryRule, VCSConnection
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import module_autodiscovery_service as svc
from terrapod.services import vcs_rate_limit

router = APIRouter(tags=["module-autodiscovery-rules"])
logger = get_logger(__name__)

_ID_PREFIX = "modrule-"
_TYPE = "module-autodiscovery-rules"
_NOT_FOUND = "module autodiscovery rule not found"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TEMPLATE_PLACEHOLDERS = {"repo": "r", "path": "p", "leaf": "l", "root": "o"}


def _rule_json(rule: ModuleAutodiscoveryRule) -> dict:
    return {
        "id": f"{_ID_PREFIX}{rule.id}",
        "type": _TYPE,
        "attributes": {
            "name": rule.name,
            "vcs-connection-id": f"vcs-{rule.vcs_connection_id}",
            "repo-url": rule.repo_url,
            "branch": rule.branch,
            "pattern": rule.pattern,
            "ignore-patterns": list(rule.ignore_patterns or []),
            "enabled": rule.enabled,
            "name-template": rule.name_template,
            "provider": rule.provider,
            "vcs-tag-pattern": rule.vcs_tag_pattern,
            "labels": dict(rule.labels or {}),
            "owner-email": rule.owner_email or "",
            "first-scan-at": rfc3339(rule.first_scan_at) if rule.first_scan_at else None,
            "last-scanned-sha": rule.last_scanned_sha or "",
            "created-at": rfc3339(rule.created_at),
            "updated-at": rfc3339(rule.updated_at),
        },
        "relationships": {
            "vcs-connection": {
                "data": {"id": f"vcs-{rule.vcs_connection_id}", "type": "vcs-connections"},
            },
        },
        "links": {"self": f"/api/terrapod/v1/module-autodiscovery-rules/{_ID_PREFIX}{rule.id}"},
    }


def _parse_rule_id(rule_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(rule_id.removeprefix(_ID_PREFIX))
    except ValueError:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from None


async def _get_rule(db: AsyncSession, rule_id: str) -> ModuleAutodiscoveryRule:
    rule = await db.get(ModuleAutodiscoveryRule, _parse_rule_id(rule_id))
    if rule is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return rule


async def _get_connection(db: AsyncSession, connection_id: uuid.UUID) -> VCSConnection:
    conn = await db.get(VCSConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=422, detail="vcs-connection-id not found")
    return conn


def _required_str(attrs: dict, key: str) -> str:
    value = attrs.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{key} is required")
    return value.strip()


def _coerce_attrs(attrs: dict, *, on_create: bool) -> dict[str, Any]:
    """Validate request attributes into model fields; 422 on anything wrong."""
    out: dict[str, Any] = {}
    if on_create:
        for key in ("name", "vcs-connection-id", "repo-url", "pattern"):
            _required_str(attrs, key)

    if "name" in attrs:
        out["name"] = _required_str(attrs, "name")
    if "vcs-connection-id" in attrs:
        try:
            out["vcs_connection_id"] = uuid.UUID(
                str(attrs["vcs-connection-id"]).removeprefix("vcs-")
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="vcs-connection-id is not a UUID") from exc
    if "repo-url" in attrs:
        out["repo_url"] = _required_str(attrs, "repo-url")
    if "branch" in attrs:
        out["branch"] = str(attrs["branch"] or "").strip()
    if "pattern" in attrs:
        out["pattern"] = _required_str(attrs, "pattern")
        _reject_directory_pattern(out["pattern"], field="pattern")
    if "ignore-patterns" in attrs:
        ip = attrs["ignore-patterns"]
        if not isinstance(ip, list) or not all(isinstance(p, str) for p in ip):
            raise HTTPException(status_code=422, detail="ignore-patterns must be a list of strings")
        for p in ip:
            _reject_directory_pattern(p, field="ignore-patterns")
        out["ignore_patterns"] = [p.strip() for p in ip if p.strip()]
    if "enabled" in attrs:
        out["enabled"] = bool(attrs["enabled"])
    if "name-template" in attrs:
        template = str(attrs["name-template"] or "")
        try:
            template.format(**_TEMPLATE_PLACEHOLDERS)
        except (KeyError, IndexError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "name-template may use only {repo}, {path}, {leaf} and {root}: "
                    f"{template!r} does not render"
                ),
            ) from exc
        out["name_template"] = template
    if "provider" in attrs:
        provider = str(attrs["provider"] or "").strip()
        if provider and not _PROVIDER_RE.match(provider):
            raise HTTPException(
                status_code=422,
                detail="provider must be lowercase letters, digits and hyphens (e.g. aws)",
            )
        out["provider"] = provider
    if "vcs-tag-pattern" in attrs:
        out["vcs_tag_pattern"] = str(attrs["vcs-tag-pattern"] or "").strip() or "v*"
    if "labels" in attrs:
        # Copied onto every module the rule registers, so the reserved-key
        # guard runs here, where the operator can fix it (#316).
        out["labels"] = validate_labels(attrs["labels"])
    if "owner-email" in attrs:
        out["owner_email"] = str(attrs["owner-email"] or "").strip() or None
    return out


def _repository_http_error(exc: svc.RepositoryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


async def _read_repository(rule: ModuleAutodiscoveryRule):  # type: ignore[no-untyped-def]
    """The rule's repository head and file paths, or the HTTP error to report."""
    try:
        with vcs_rate_limit.vcs_source("module-autodiscovery"):
            head = await svc.resolve_head(rule.vcs_connection, rule.repo_url, rule.branch)
            file_paths = await svc.list_files(rule.vcs_connection, head)
    except svc.RepositoryError as exc:
        raise _repository_http_error(exc) from exc
    return head, file_paths


def _preview_json(head, file_paths: list[str], entries: list[dict]) -> dict:  # type: ignore[no-untyped-def]
    return {
        "data": {
            "type": "module-autodiscovery-rule-previews",
            "attributes": {
                "ref": head.branch,
                "files-walked": len(file_paths),
                "entries": entries,
            },
        }
    }


# ── List / create / show / update / delete ─────────────────────────────────


@router.get("/module-autodiscovery-rules")
async def list_rules(
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List module autodiscovery rules, newest first. Admin only."""
    rules = (
        (
            await db.execute(
                select(ModuleAutodiscoveryRule).order_by(ModuleAutodiscoveryRule.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    page_items, meta = paginate([_rule_json(r) for r in rules], request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/module-autodiscovery-rules", status_code=201)
async def create_rule(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a module autodiscovery rule. Admin only.

    Saving registers nothing: the first poll only records what is already in the
    repository. Register existing modules with `/scan`.
    """
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=True)
    await _get_connection(db, fields["vcs_connection_id"])
    rule = ModuleAutodiscoveryRule(**fields)
    db.add(rule)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A module autodiscovery rule with that name already exists for this connection",
        ) from exc
    await db.refresh(rule)
    logger.info(
        "Module autodiscovery rule created",
        rule_id=str(rule.id),
        rule_name=rule.name,
        repo_url=rule.repo_url,
        actor=user.email,
    )
    return JSONResponse(status_code=201, content={"data": _rule_json(rule)})


@router.post("/module-autodiscovery-rules/preview")
async def preview_unsaved_rule(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What a prospective rule would find, without saving it. Admin only.

    Takes the same attributes as create, validated the same way, so a mistake
    shows up here rather than after saving.
    """
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=True)
    conn = await _get_connection(db, fields["vcs_connection_id"])
    rule = ModuleAutodiscoveryRule(id=uuid.uuid4(), **fields)
    # Never added to the session; the repository read needs the connection.
    rule.vcs_connection = conn
    head, file_paths = await _read_repository(rule)
    entries = await svc.preview(db, rule, file_paths)
    return JSONResponse(content=_preview_json(head, file_paths, entries))


@router.get("/module-autodiscovery-rules/{rule_id}")
async def show_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show one module autodiscovery rule. Admin only."""
    return JSONResponse(content={"data": _rule_json(await _get_rule(db, rule_id))})


@router.patch("/module-autodiscovery-rules/{rule_id}")
async def update_rule(
    rule_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a module autodiscovery rule. Admin only.

    Pointing it at another repository or connection starts it afresh: the
    directories it has seen belong to the old one, so the next poll records a
    new baseline rather than registering everything in the new repository.
    """
    rule = await _get_rule(db, rule_id)
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=False)
    if "vcs_connection_id" in fields:
        await _get_connection(db, fields["vcs_connection_id"])
    moved = any(
        k in fields and fields[k] != getattr(rule, k)
        for k in ("vcs_connection_id", "repo_url", "branch")
    )
    for key, value in fields.items():
        setattr(rule, key, value)
    if moved:
        rule.seen_subdirectories = []
        rule.last_scanned_sha = ""
        rule.first_scan_at = None
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A module autodiscovery rule with that name already exists for this connection",
        ) from exc
    await db.refresh(rule)
    logger.info(
        "Module autodiscovery rule updated",
        rule_id=str(rule.id),
        actor=user.email,
        changed=sorted(fields),
    )
    return JSONResponse(content={"data": _rule_json(rule)})


@router.delete("/module-autodiscovery-rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a rule. Admin only. The modules it registered stay registered."""
    rule = await _get_rule(db, rule_id)
    await db.delete(rule)
    await db.commit()
    logger.info("Module autodiscovery rule deleted", rule_id=str(rule.id), actor=user.email)
    return Response(status_code=204)


# ── Preview / scan ─────────────────────────────────────────────────────────


@router.get("/module-autodiscovery-rules/{rule_id}/preview")
async def preview_rule(
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What the rule finds in its repository now. Registers nothing. Admin only."""
    rule = await _get_rule(db, rule_id)
    head, file_paths = await _read_repository(rule)
    entries = await svc.preview(db, rule, file_paths)
    return JSONResponse(content=_preview_json(head, file_paths, entries))


@router.post("/module-autodiscovery-rules/{rule_id}/scan")
async def scan_rule(
    rule_id: str = Path(...),
    body: dict | None = Body(default=None),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Register the rule's candidates — all of them, or `subdirectories`. Admin only.

    Works whether or not the rule is enabled: this is an explicit operator
    action. Candidates already registered, or whose name is taken, are skipped
    and reported. Everything the scan saw counts as seen, so automatic
    registration will not later pick up a candidate that was left out here.
    """
    rule = await _get_rule(db, rule_id)
    only = None
    attrs = ((body or {}).get("data") or {}).get("attributes") or {}
    if "subdirectories" in attrs:
        only = attrs["subdirectories"]
        if not isinstance(only, list) or not all(isinstance(d, str) for d in only):
            raise HTTPException(status_code=422, detail="subdirectories must be a list of strings")

    head, file_paths = await _read_repository(rule)
    try:
        result = await svc.register_candidates(db, rule, file_paths, only=only)
    except svc.UnknownSubdirectoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    svc.record_scan(rule, file_paths, head.sha)
    await db.commit()
    logger.info(
        "Module autodiscovery scan complete",
        rule_id=str(rule.id),
        ref=head.branch,
        modules_registered=len(result.created),
        skipped=len(result.skipped),
        actor=user.email,
    )
    return JSONResponse(
        content={
            "data": {
                "type": "module-autodiscovery-rule-scans",
                "attributes": {
                    "ref": head.branch,
                    "files-walked": len(file_paths),
                    "modules-registered": len(result.created),
                    "modules": [
                        {
                            "id": str(m.id),
                            "name": m.name,
                            "provider": m.provider,
                            "subdirectory": m.subdirectory,
                        }
                        for m in result.created
                    ],
                    "skipped": [{"subdirectory": d, "reason": r} for d, r in result.skipped],
                },
            }
        }
    )
