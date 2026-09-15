"""Module autodiscovery rules (#1584, #1620).

The module registry's counterpart to `autodiscovery_rules.py`: rules that find
modules in repositories — the root and any submodules — and register them. A
rule's `repo-url` names one repository, an org or group, or a pattern over one
namespace's repositories; the server decides which when the rule is saved and
reports it as `target-kind`. See `services/module_autodiscovery_service.py` for
the matching and registration, `services/module_autodiscovery_targets.py` for
the classification, and `docs/registry.md` for the operator's view.

UX CONTRACT: consumed by `web/src/app/admin/module-autodiscovery/page.tsx` and
the Discover panel on `web/src/app/registry/modules/page.tsx`. Changes to
response shapes, attribute names or status codes MUST be matched there.

Endpoints (all platform admin — a rule reads repositories with the platform's
VCS credentials, and registers modules on its own authority):
    GET    /api/terrapod/v1/module-autodiscovery-rules                   (list)
    POST   /api/terrapod/v1/module-autodiscovery-rules                   (create)
    POST   /api/terrapod/v1/module-autodiscovery-rules/preview           (dry-run unsaved rule)
    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}              (show)
    PATCH  /api/terrapod/v1/module-autodiscovery-rules/{id}              (update)
    DELETE /api/terrapod/v1/module-autodiscovery-rules/{id}              (delete)
    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}/preview      (dry-run saved rule)
    POST   /api/terrapod/v1/module-autodiscovery-rules/{id}/scan         (register all or a subset)
    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}/repositories (per-repository state)
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.labels import validate_labels
from terrapod.api.pagination import MAX_PAGE_SIZE, build_meta, paginate, parse_page_params
from terrapod.api.routers.autodiscovery_rules import _reject_directory_pattern
from terrapod.api.serialization import rfc3339
from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    VCSConnection,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import module_autodiscovery_service as svc
from terrapod.services import module_autodiscovery_targets as targets
from terrapod.services import vcs_rate_limit

router = APIRouter(tags=["module-autodiscovery-rules"])
logger = get_logger(__name__)

_ID_PREFIX = "modrule-"
_TYPE = "module-autodiscovery-rules"
_REPO_ID_PREFIX = "modrepo-"
_REPO_TYPE = "module-autodiscovery-rule-repositories"
_NOT_FOUND = "module autodiscovery rule not found"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
#: Most repositories one scan request may select.
_MAX_SELECTIONS = 200
#: Repositories an unsaved org-wide preview reads live, per page.
_LIVE_PAGE_DEFAULT = 10
_LIVE_PAGE_MAX = 25


def _rule_json(rule: ModuleAutodiscoveryRule) -> dict:
    return {
        "id": f"{_ID_PREFIX}{rule.id}",
        "type": _TYPE,
        "attributes": {
            "name": rule.name,
            "vcs-connection-id": f"vcs-{rule.vcs_connection_id}",
            "repo-url": rule.repo_url,
            "target-kind": svc.target_kind(rule),
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
            # A single-repository rule's head; empty for an org-wide rule, whose
            # heads are per repository (`/repositories`).
            "last-scanned-sha": rule.last_scanned_sha or "",
            "last-enumerated-at": (
                rfc3339(rule.last_enumerated_at) if rule.last_enumerated_at else None
            ),
            "last-error": rule.last_error or "",
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


def _repository_json(row: ModuleAutodiscoveryRepository) -> dict:
    return {
        "id": f"{_REPO_ID_PREFIX}{row.id}",
        "type": _REPO_TYPE,
        "attributes": {
            "repository": row.repo_path,
            "repo-url": row.repo_url,
            "vcs-repo-id": row.vcs_repo_id,
            "default-branch": row.default_branch,
            "origin": row.origin,
            "status": row.status,
            "last-scanned-sha": row.last_scanned_sha,
            "seen-subdirectories": list(row.seen_subdirectories or []),
            "candidates": list(row.candidates or []),
            "last-skips": list(row.last_skips or []),
            "previous-paths": list(row.previous_paths or []),
            "repo-created-at": rfc3339(row.repo_created_at) if row.repo_created_at else None,
            "first-seen-at": rfc3339(row.first_seen_at) if row.first_seen_at else None,
            "last-checked-at": rfc3339(row.last_checked_at) if row.last_checked_at else None,
            "next-check-at": rfc3339(row.next_check_at) if row.next_check_at else None,
            "failure-count": row.failure_count or 0,
            "last-error": row.last_error or "",
        },
        "relationships": {
            "rule": {"data": {"id": f"{_ID_PREFIX}{row.rule_id}", "type": _TYPE}},
        },
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
        # A real boolean only: `bool("false")` is True.
        if not isinstance(attrs["enabled"], bool):
            raise HTTPException(status_code=422, detail="enabled must be a boolean")
        out["enabled"] = attrs["enabled"]
    if "name-template" in attrs:
        template = attrs["name-template"] or ""
        # Literal text and the placeholders, nothing else: no format specs,
        # no attribute access, no other braces.
        if not isinstance(template, str) or not svc.TEMPLATE_RE.match(template):
            raise HTTPException(
                status_code=422,
                detail=(
                    "name-template may use only {repo}, {path}, {leaf}, {root} and {owner} "
                    f"with literal text: {template!r} is not allowed"
                ),
            )
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
        email = str(attrs["owner-email"] or "").strip()
        if email and not _EMAIL_RE.match(email):
            raise HTTPException(status_code=422, detail="owner-email must be a valid email address")
        out["owner_email"] = email or None
    return out


async def _classify(conn: VCSConnection, repo_url: str) -> targets.Target:
    """What `repo-url` names on `conn`: 422 when it names nothing, 502 when
    the provider could not be asked (so an apply can be retried)."""
    try:
        with vcs_rate_limit.vcs_source("module-autodiscovery"):
            return await targets.classify(conn, repo_url)
    except targets.TargetError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _repository_http_error(exc: svc.RepositoryError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


async def _read_repository(rule: ModuleAutodiscoveryRule, repo_url: str):  # type: ignore[no-untyped-def]
    """A repository's head and file paths, or the HTTP error to report."""
    try:
        with vcs_rate_limit.vcs_source("module-autodiscovery"):
            head = await svc.resolve_head(rule.vcs_connection, repo_url, rule.branch)
            file_paths = await svc.list_files(rule.vcs_connection, head)
    except svc.RepositoryError as exc:
        raise _repository_http_error(exc) from exc
    return head, file_paths


def _repository_summary(row: ModuleAutodiscoveryRepository, ref: str | None = None) -> dict:
    return {
        "repository": row.repo_path,
        "repo-url": row.repo_url,
        "ref": ref if ref is not None else row.default_branch,
        "status": row.status,
        "origin": row.origin,
        "error": row.last_error or "",
    }


def _preview_json(
    ref: str,
    files_walked: int,
    entries: list[dict],
    *,
    kind: str,
    repositories: list[dict],
    meta: dict | None = None,
    complete: bool = True,
) -> dict:
    body: dict = {
        "data": {
            "type": "module-autodiscovery-rule-previews",
            "attributes": {
                "ref": ref,
                "files-walked": files_walked,
                "entries": entries,
                "target-kind": kind,
                # Grouping for the entries, with each repository's status.
                "repositories": repositories,
                # False when an org-wide listing stopped at the repository cap.
                "listing-complete": complete,
            },
        }
    }
    if meta is not None:
        body["meta"] = meta
    return body


def _names_repository(value: str, ctx: svc.RepoContext) -> bool:
    """Whether `value` — a path or a URL — is the repository `ctx` describes."""
    wanted = value.strip().strip("/").lower()
    return wanted == ctx.path.lower() or targets.url_key(value) in {
        targets.url_key(u) for u in ctx.urls
    }


def _find_row(
    rows: list[ModuleAutodiscoveryRepository], repository: str
) -> ModuleAutodiscoveryRepository | None:
    for row in rows:
        if _names_repository(repository, svc.row_context(row)):
            return row
    return None


def _scannable(rows: list[ModuleAutodiscoveryRepository]) -> list[ModuleAutodiscoveryRepository]:
    """Repositories with candidates this rule may register, by path."""
    return sorted(
        (r for r in rows if r.status in svc.SCANNABLE_STATUSES and r.candidates),
        key=lambda r: r.repo_path.lower(),
    )


def _parse_selections(attrs: dict) -> list[tuple[str, list[str] | None]] | None:
    """`selections: [{repository, subdirectories?}]`, validated; None if absent."""
    if "selections" not in attrs:
        return None
    raw = attrs["selections"]
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="selections must be a list")
    if len(raw) > _MAX_SELECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {_MAX_SELECTIONS} repositories may be selected in one scan",
        )
    out: list[tuple[str, list[str] | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="each selection must be an object")
        repository = item.get("repository")
        if not isinstance(repository, str) or not repository.strip():
            raise HTTPException(status_code=422, detail="each selection needs a repository")
        subdirectories = item.get("subdirectories")
        if subdirectories is not None and (
            not isinstance(subdirectories, list)
            or not all(isinstance(d, str) for d in subdirectories)
        ):
            raise HTTPException(
                status_code=422, detail="a selection's subdirectories must be a list of strings"
            )
        out.append((repository.strip(), subdirectories))
    return out


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

    `repo-url` is classified with the provider: 422 when it names nothing, 502
    when the provider cannot be asked. A pattern that matches nothing yet is
    accepted — it exists to catch repositories created later. Saving registers
    nothing: the first poll only records what is already there. Register
    existing modules with `/scan`.
    """
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=True)
    conn = await _get_connection(db, fields["vcs_connection_id"])
    target = await _classify(conn, fields["repo_url"])
    rule = ModuleAutodiscoveryRule(**fields, target_kind=target.kind, target_id=target.id)
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
        target_kind=target.kind,
        actor=user.email,
    )
    return JSONResponse(status_code=201, content={"data": _rule_json(rule)})


@router.post("/module-autodiscovery-rules/preview")
async def preview_unsaved_rule(
    request: Request = None,
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What a prospective rule would find, without saving it. Admin only.

    Takes the same attributes as create, validated and classified the same
    way, so a mistake shows up here rather than after saving. An org-wide rule
    reads one bounded page of repositories live (`page[size]`, at most 25,
    default 10); a repository that cannot be read is reported in
    `repositories`, not as a failed preview.
    """
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=True)
    conn = await _get_connection(db, fields["vcs_connection_id"])
    target = await _classify(conn, fields["repo_url"])
    rule = ModuleAutodiscoveryRule(
        id=uuid.uuid4(), **fields, target_kind=target.kind, target_id=target.id
    )
    # Never added to the session; the repository read needs the connection.
    rule.vcs_connection = conn

    if target.kind == targets.KIND_REPOSITORY:
        ctx = svc.rule_context(rule)
        head, file_paths = await _read_repository(rule, ctx.url)
        entries = await svc.preview(db, rule, file_paths, repo=ctx)
        summary = {
            "repository": ctx.path,
            "repo-url": ctx.url,
            "ref": head.branch,
            "status": "active",
            "origin": "baseline",
            "error": "",
        }
        return JSONResponse(
            content=_preview_json(
                head.branch, len(file_paths), entries, kind=target.kind, repositories=[summary]
            )
        )

    from terrapod.config import settings

    number, size = parse_page_params(request)
    size = min(size or _LIVE_PAGE_DEFAULT, _LIVE_PAGE_MAX)
    try:
        with vcs_rate_limit.vcs_source("module-autodiscovery"):
            listing = await targets.list_repositories(
                conn,
                target.kind,
                target.id,
                target.glob,
                max_repositories=settings.registry.module_autodiscovery.max_repositories,
            )
    except targets.TargetGone as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"could not list the repositories: {exc}; try again"
        ) from exc
    refs = [r for r in listing.repositories if not r.fork and not r.disabled]
    page = refs[(number - 1) * size : number * size]
    covered = await svc.covered_for(db, conn.id)
    with vcs_rate_limit.vcs_source("module-autodiscovery"):
        entries, repositories, walked = await svc.read_repositories(db, rule, conn, page, covered)
    for info in repositories:
        info["origin"] = "baseline"
    return JSONResponse(
        content=_preview_json(
            rule.branch,
            walked,
            entries,
            kind=target.kind,
            repositories=repositories,
            meta=build_meta(len(refs), number, size),
            complete=listing.complete,
        )
    )


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

    Another connection or `repo-url` is classified again, as on create; so is
    re-saving the same `repo-url` of a rule in error, or of one saved before
    classification existed. The classification never changes otherwise.

    A change to what the rule looks at starts it afresh: another connection,
    repository or target, branch, a different `pattern` or `ignore-patterns`,
    or a re-enable after being disabled. What it has seen no longer describes
    what it would claim, so its per-repository state is cleared and the next
    poll records a new baseline rather than registering every directory the old
    rule never claimed — none of them previewed or ticked. Register those with
    a scan.
    """
    rule = await _get_rule(db, rule_id)
    fields = _coerce_attrs(body.get("data", {}).get("attributes", {}), on_create=False)
    conn = rule.vcs_connection
    if "vcs_connection_id" in fields:
        conn = await _get_connection(db, fields["vcs_connection_id"])

    target_changed = False
    changed = any(
        k in fields and fields[k] != getattr(rule, k) for k in ("vcs_connection_id", "repo_url")
    )
    resaved = ("repo_url" in fields or "vcs_connection_id" in fields) and (
        not rule.target_id or bool(rule.last_error)
    )
    if changed or resaved:
        target = await _classify(conn, fields.get("repo_url", rule.repo_url))
        old_kind, old_id = svc.target_kind(rule), rule.target_id or ""
        target_changed = target.kind != old_kind or bool(old_id and target.id != old_id)
        fields["target_kind"], fields["target_id"] = target.kind, target.id
        rule.last_error = ""

    rebaseline = (
        target_changed
        or any(
            k in fields and fields[k] != getattr(rule, k)
            for k in ("vcs_connection_id", "repo_url", "branch", "pattern")
        )
        or (
            "ignore_patterns" in fields
            and fields["ignore_patterns"] != list(rule.ignore_patterns or [])
        )
        or (fields.get("enabled") is True and not rule.enabled)
    )
    for key, value in fields.items():
        setattr(rule, key, value)
    if rebaseline:
        rule.seen_subdirectories = []
        rule.last_scanned_sha = ""
        rule.first_scan_at = None
        rule.last_enumerated_at = None
        # The per-repository state goes with it (#1620): the next poll takes a
        # new baseline of every repository the rule now looks at.
        #
        # Row by row through the ORM, not one Core `delete()`: these rows
        # replicate (#1666), and a Core statement never reaches the outbox, so
        # a follower would keep the old baseline and a promoted node would
        # skip the re-baseline the operator asked for.
        for row in await svc.load_repositories(db, rule):
            await db.delete(row)
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
        rebaselined=rebaseline,
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


# ── Preview / scan / repositories ──────────────────────────────────────────


@router.get("/module-autodiscovery-rules/{rule_id}/preview")
async def preview_rule(
    request: Request = None,
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What the rule finds. Registers nothing. Admin only.

    A single-repository rule reads its repository live, as always. An
    org-wide rule is served from what the poller last found, with no VCS
    calls, a page of repositories at a time (`page[size]`, `page[number]`),
    its entries grouped by `repository`; `repository=<path>` reads that one
    repository live instead.
    """
    rule = await _get_rule(db, rule_id)
    kind = svc.target_kind(rule)
    repository = (request.query_params.get("repository") or "").strip() if request else ""
    rows = await svc.load_repositories(db, rule)

    if kind == targets.KIND_REPOSITORY:
        ctx = svc.rule_context(rule, rows[0] if rows else None)
        if repository and not _names_repository(repository, ctx):
            raise HTTPException(
                status_code=422, detail=f"{repository!r} is not this rule's repository"
            )
        head, file_paths = await _read_repository(rule, ctx.url)
        entries = await svc.preview(db, rule, file_paths, repo=ctx)
        summary = (
            _repository_summary(rows[0], head.branch)
            if rows
            else {
                "repository": ctx.path,
                "repo-url": ctx.url,
                "ref": head.branch,
                "status": "active",
                "origin": "baseline",
                "error": "",
            }
        )
        return JSONResponse(
            content=_preview_json(
                head.branch, len(file_paths), entries, kind=kind, repositories=[summary]
            )
        )

    if repository:
        row = _find_row(rows, repository)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"{repository!r} is not one of this rule's repositories"
            )
        head, file_paths = await _read_repository(rule, row.repo_url)
        entries = await svc.preview(db, rule, file_paths, repo=svc.row_context(row))
        return JSONResponse(
            content=_preview_json(
                head.branch,
                len(file_paths),
                entries,
                kind=kind,
                repositories=[_repository_summary(row, head.branch)],
            )
        )

    page_rows, meta = paginate(_scannable(rows), request)
    entries = await svc.stored_preview(db, rule, page_rows)
    return JSONResponse(
        content=_preview_json(
            "",
            0,
            entries,
            kind=kind,
            repositories=[_repository_summary(r) for r in page_rows],
            meta=meta,
        )
    )


@router.post("/module-autodiscovery-rules/{rule_id}/scan")
async def scan_rule(
    rule_id: str = Path(...),
    body: dict | None = Body(default=None),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Register the rule's candidates — all of them, or a chosen subset. Admin only.

    Works whether or not the rule is enabled: this is an explicit operator
    action. Candidates already registered, or whose name is taken, are skipped
    and reported.

    A single-repository rule reads its repository live and takes
    `subdirectories` (or `selections` naming its one repository); everything
    the scan saw counts as seen, so automatic registration will not later pick
    up a candidate left out here. An org-wide rule registers from what the
    poller last found, with no VCS calls: `selections: [{repository,
    subdirectories?}]` picks repositories and, optionally, directories in them;
    an empty body registers every current candidate.
    """
    rule = await _get_rule(db, rule_id)
    attrs = ((body or {}).get("data") or {}).get("attributes") or {}
    only = None
    if "subdirectories" in attrs:
        only = attrs["subdirectories"]
        if not isinstance(only, list) or not all(isinstance(d, str) for d in only):
            raise HTTPException(status_code=422, detail="subdirectories must be a list of strings")
    selections = _parse_selections(attrs)
    kind = svc.target_kind(rule)

    if kind != targets.KIND_REPOSITORY:
        if only is not None:
            raise HTTPException(
                status_code=422,
                detail="subdirectories applies to a rule that names one repository; "
                "use selections: [{repository, subdirectories}]",
            )
        return await _scan_namespace_rule(db, rule, selections, user)

    rows = await svc.load_repositories(db, rule)
    row = svc.repository_state(rule)
    ctx = svc.rule_context(rule, row if rows else None)
    if selections is not None:
        for repository, subs in selections:
            if not _names_repository(repository, ctx):
                raise HTTPException(
                    status_code=422, detail=f"{repository!r} is not this rule's repository"
                )
            if subs is None:
                only = None
                break
            only = [*(only or []), *subs]

    head, file_paths = await _read_repository(rule, ctx.url)
    try:
        result = await svc.register_candidates(db, rule, file_paths, only=only, repo=ctx)
    except svc.UnknownSubdirectoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Its repository row is kept current alongside the rule (#1620).
    row.default_branch = head.branch
    row.last_skips = [{"subdirectory": d, "reason": r} for d, r in result.skipped]
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
                            "repository": ctx.path,
                            "repo-url": m.vcs_repo_url,
                        }
                        for m in result.created
                    ],
                    "skipped": [{"subdirectory": d, "reason": r} for d, r in result.skipped],
                    "repositories-scanned": 1,
                },
            }
        }
    )


async def _scan_namespace_rule(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    selections: list[tuple[str, list[str] | None]] | None,
    user: AuthenticatedUser,
) -> JSONResponse:
    rows = await svc.load_repositories(db, rule)
    scannable = _scannable(rows)
    if selections:
        chosen = []
        for repository, subs in selections:
            row = _find_row(scannable, repository)
            if row is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"{repository!r} is not one of this rule's repositories with "
                    "candidates to register",
                )
            chosen.append((row, subs))
    else:
        chosen = [(row, None) for row in scannable]
    try:
        result = await svc.register_stored(db, rule, chosen)
    except svc.UnknownSubdirectoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Module autodiscovery scan complete",
        rule_id=str(rule.id),
        repositories=len(chosen),
        modules_registered=len(result.created),
        skipped=len(result.skipped),
        actor=user.email,
    )
    return JSONResponse(
        content={
            "data": {
                "type": "module-autodiscovery-rule-scans",
                "attributes": {
                    "ref": "",
                    "files-walked": 0,
                    "modules-registered": len(result.created),
                    "modules": [
                        {
                            "id": str(m.id),
                            "name": m.name,
                            "provider": m.provider,
                            "subdirectory": m.subdirectory,
                            "repository": repository,
                            "repo-url": m.vcs_repo_url,
                        }
                        for m, repository in zip(result.created, result.created_in, strict=True)
                    ],
                    "skipped": list(result.skipped_in),
                    "repositories-scanned": len(chosen),
                },
            }
        }
    )


@router.get("/module-autodiscovery-rules/{rule_id}/repositories")
async def list_rule_repositories(
    request: Request = None,
    rule_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The rule's per-repository scan state, by path. Admin only.

    One entry for a single-repository rule (once it has been polled), one per
    repository an org-wide rule has listed. `filter[status]` narrows by
    status. Paginated in the database, since a namespace can be large.
    """
    rule = await _get_rule(db, rule_id)
    query = select(ModuleAutodiscoveryRepository).where(
        ModuleAutodiscoveryRepository.rule_id == rule.id
    )
    status = (request.query_params.get("filter[status]") or "").strip() if request else ""
    if status:
        query = query.where(ModuleAutodiscoveryRepository.status == status)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    number, size = parse_page_params(request)
    query = query.order_by(ModuleAutodiscoveryRepository.repo_path)
    if size is not None:
        size = min(size, MAX_PAGE_SIZE)
        query = query.limit(size).offset((number - 1) * size)
    rows = (await db.execute(query)).scalars().all()
    return JSONResponse(
        content={
            "data": [_repository_json(r) for r in rows],
            "meta": build_meta(total, number, size),
        }
    )
