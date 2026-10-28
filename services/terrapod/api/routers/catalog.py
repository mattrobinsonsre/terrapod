"""Service catalog API (#535): provider-template + catalog-item management and
the provision flow.

UX CONTRACT: consumed by the web frontend catalog pages (selection-first
provisioning). Changes to response shapes, attribute names, or status codes
MUST be matched there and in go-terrapod.

Endpoints (all under /api/terrapod/v1):
    GET    /provider-templates                 list        (admin/audit)
    POST   /provider-templates                 create      (admin)
    GET    /provider-templates/{id}            show        (admin/audit)
    PATCH  /provider-templates/{id}            update      (admin)
    DELETE /provider-templates/{id}            delete      (admin)

    GET    /catalog-items                       list        (catalog read+)
    POST   /catalog-items                       create      (admin)
    GET    /catalog-items/{id}                  show        (catalog read+)
    PATCH  /catalog-items/{id}                  update      (admin)
    DELETE /catalog-items/{id}                  delete      (admin)
    GET    /catalog-items/{id}/form             provision form (catalog read+)
    GET    /catalog-items/{id}/instances        list instances (catalog read+)
    POST   /catalog-items/{id}/provision        provision   (catalog use + pool write)

The whole router is gated on `catalog.enabled` — disabled returns 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from redis.exceptions import LockError, RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    AuthenticatedUser,
    effective_platform_roles,
    get_current_user,
    require_admin,
    require_admin_or_audit,
)
from terrapod.api.ids import parse_id
from terrapod.api.labels import validate_labels
from terrapod.api.pagination import paginate
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.config import settings
from terrapod.db.models import (
    AgentPool,
    CatalogItem,
    ConfigurationVersion,
    ProviderTemplate,
    RegistryModule,
    Run,
    Variable,
    Workspace,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client
from terrapod.services import catalog_service, pool_set, run_service
from terrapod.services.catalog_rbac_service import (
    resolve_catalog_capabilities_for,
)
from terrapod.services.catalog_service import CatalogError
from terrapod.services.pool_rbac_service import (
    resolve_pool_capabilities_for,
)
from terrapod.storage.keys import module_tarball_key

router = APIRouter(tags=["catalog"])
logger = get_logger(__name__)


async def require_catalog_enabled() -> None:
    """Gate the whole router — 404 when the catalog feature is off."""
    if not settings.catalog.enabled:
        raise HTTPException(status_code=404, detail="Service catalog is not enabled")


def _rfc3339(dt) -> str:  # noqa: ANN001
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(UTC)


# ── Serializers ────────────────────────────────────────────────────────


def _template_json(t: ProviderTemplate, *, include_sensitive_values: bool = False) -> dict:
    parameters = (
        list(t.parameters or [])
        if include_sensitive_values
        else [
            (
                {k: p[k] for k in ("name", "type", "required", "sensitive") if k in p}
                if p.get("sensitive")
                else {
                    k: p[k]
                    for k in (
                        "name",
                        "type",
                        "description",
                        "required",
                        "sensitive",
                        "default",
                        "options",
                    )
                    if k in p
                }
            )
            for p in (t.parameters or [])
        ]
    )
    return {
        "id": str(t.id),
        "type": "provider-templates",
        "attributes": {
            "name": t.name,
            "provider-type": t.provider_type,
            # Arbitrary provider HCL can inline credentials independently of
            # parameter flags; an ordinary read cannot prove its body safe.
            "body": t.body if include_sensitive_values else None,
            "parameters": parameters,
            "labels": dict(t.labels or {}),
            "owner-email": t.owner_email or "",
            "created-at": _rfc3339(t.created_at),
            "updated-at": _rfc3339(t.updated_at),
        },
        "links": {"self": f"/api/v1/provider-templates/{t.id}"},
    }


def _item_json(
    item: CatalogItem,
    *,
    include_sensitive_values: bool = False,
) -> dict:
    module = item.module
    options = (
        list(item.variable_options or [])
        if include_sensitive_values
        else [
            # Unknown metadata may contain nested defaults or choices.
            {
                k: opt[k]
                for k in ("name", "hidden", "sensitive", "description", "type", "required")
                if k in opt
            }
            for opt in (item.variable_options or [])
        ]
    )
    return {
        "id": str(item.id),
        "type": "catalog-items",
        "attributes": {
            "name": item.name,
            "display-name": item.display_name or "",
            "description": item.description or "",
            "enabled": item.enabled,
            "self-contained": item.self_contained,
            "execution-backend-constraints": item.execution_backend_constraints,
            "module-id": str(item.module_id),
            "module-name": module.name if module else "",
            "module-provider": module.provider if module else "",
            "default-version-pin": item.default_version_pin,
            "provider-template-ids": [str(x) for x in (item.provider_template_ids or [])],
            "allowed-agent-pool-ids": (
                [f"apool-{x}" for x in item.allowed_agent_pool_ids]
                if item.allowed_agent_pool_ids is not None
                else None
            ),
            "variable-options": options,
            # The Ansible half this item provisions (#2019). `configure-enabled`
            # says WHETHER and `ansible-directory` says WHERE; the path does not
            # carry "off" in its emptiness, because #1966 corrected exactly that
            # on the workspace's own `config-directory`.
            "configure-enabled": item.configure_enabled,
            "ansible-directory": item.ansible_directory or "",
            "default-playbook": item.default_playbook or "",
            "default-limit": item.default_limit or "",
            "labels": dict(item.labels or {}),
            "owner-email": item.owner_email or "",
            "created-at": _rfc3339(item.created_at),
            "updated-at": _rfc3339(item.updated_at),
        },
        "links": {"self": f"/api/v1/catalog-items/{item.id}"},
    }


def _authoring_read_allowed(user: AuthenticatedUser, requested: str | None) -> bool:
    if requested not in (None, "true", "false"):
        raise HTTPException(
            status_code=422, detail="include-sensitive-values must be true or false"
        )
    include_sensitive_values = requested == "true"
    if include_sensitive_values and "admin" not in effective_platform_roles(user):
        raise HTTPException(
            status_code=403, detail="Requires platform admin to include sensitive values"
        )
    return include_sensitive_values


def _instance_json(ws: Workspace) -> dict:
    return {
        "id": str(ws.id),
        "type": "catalog-instances",
        "attributes": {
            "name": ws.name,
            "catalog-item-id": str(ws.catalog_item_id) if ws.catalog_item_id else None,
            "catalog-version-pin": ws.catalog_version_pin,
            "catalog-update-block": ws.catalog_update_block,
            "agent-pool-id": (
                f"apool-{_ws_pools[0]}" if (_ws_pools := pool_set.workspace_pool_ids(ws)) else None
            ),
            "owner-email": ws.owner_email or "",
            "labels": dict(ws.labels or {}),
        },
        # JSON:API house style (#1063): links belong in `relationships`. The
        # `*-id` attributes above stay indefinitely for back-compat — this is
        # purely additive.
        "relationships": {
            **(
                {
                    "catalog-item": {
                        "data": {"id": str(ws.catalog_item_id), "type": "catalog-items"},
                    },
                }
                if ws.catalog_item_id
                else {}
            ),
            **(
                {
                    "agent-pool": {
                        "data": {
                            "id": f"apool-{pool_set.workspace_pool_ids(ws)[0]}",
                            "type": "agent-pools",
                        },
                    },
                }
                if pool_set.workspace_pool_ids(ws)
                else {}
            ),
            "workspace": {"data": {"id": f"ws-{ws.id}", "type": "workspaces"}},
        },
        "links": {"self": f"/api/v1/workspaces/ws-{ws.id}"},
    }


# ── Provider templates ─────────────────────────────────────────────────


def _coerce_template(attrs: dict, *, on_create: bool) -> dict:
    out: dict = {}
    if on_create or "name" in attrs:
        name = str(attrs.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        out["name"] = name
    if on_create or "provider-type" in attrs:
        pt = str(attrs.get("provider-type", "")).strip()
        if not pt:
            raise HTTPException(status_code=422, detail="provider-type is required")
        out["provider_type"] = pt
    if on_create or "body" in attrs:
        body = str(attrs.get("body", "")).strip()
        if not body:
            raise HTTPException(status_code=422, detail="body is required")
        out["body"] = body
    if "parameters" in attrs:
        params = attrs["parameters"]
        if not isinstance(params, list):
            raise HTTPException(status_code=422, detail="parameters must be a list")
        for p in params:
            if not isinstance(p, dict) or not str(p.get("name", "")).strip():
                raise HTTPException(
                    status_code=422, detail="each parameter must be an object with a name"
                )
            if "sensitive" in p and not isinstance(p["sensitive"], bool):
                raise HTTPException(status_code=422, detail="parameter sensitive must be a boolean")
        out["parameters"] = params
    if "labels" in attrs:
        out["labels"] = validate_labels(attrs["labels"] or {})
    return out


@router.get("/provider-templates")
async def list_provider_templates(
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    templates = await catalog_service.list_provider_templates(db)
    items = [_template_json(t) for t in templates]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/provider-templates", status_code=201)
async def create_provider_template(
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    attrs = body.get("data", {}).get("attributes", {})
    fields = _coerce_template(attrs, on_create=True)
    t = ProviderTemplate(
        id=uuid.uuid4(),
        name=fields["name"],
        provider_type=fields["provider_type"],
        body=fields["body"],
        parameters=fields.get("parameters", []),
        labels=fields.get("labels", {}),
        owner_email=user.email,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(t)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="provider template name already exists") from e
    await db.refresh(t)
    return JSONResponse(content={"data": _template_json(t)}, status_code=201)


@router.get("/provider-templates/{template_id}")
async def show_provider_template(
    template_id: str = Path(...),
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    reveal = _authoring_read_allowed(user, request.query_params.get("include-sensitive-values"))
    t = await catalog_service.get_provider_template(
        db, parse_id(template_id, detail="provider template not found")
    )
    if t is None:
        raise HTTPException(status_code=404, detail="provider template not found")
    return JSONResponse(content={"data": _template_json(t, include_sensitive_values=reveal)})


@router.patch("/provider-templates/{template_id}")
async def update_provider_template(
    template_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    t = await catalog_service.get_provider_template(
        db, parse_id(template_id, detail="provider template not found")
    )
    if t is None:
        raise HTTPException(status_code=404, detail="provider template not found")
    attrs = body.get("data", {}).get("attributes", {})
    for k, v in _coerce_template(attrs, on_create=False).items():
        setattr(t, k, v)
    t.updated_at = _now()
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="provider template name already exists") from e
    await db.refresh(t)
    return JSONResponse(content={"data": _template_json(t)})


@router.delete("/provider-templates/{template_id}", status_code=204)
async def delete_provider_template(
    template_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    t = await catalog_service.get_provider_template(
        db, parse_id(template_id, detail="provider template not found")
    )
    if t is None:
        raise HTTPException(status_code=404, detail="provider template not found")
    # Refuse to delete a template still referenced by a catalog item.
    result = await db.execute(select(CatalogItem))
    for item in result.scalars().all():
        if str(t.id) in [str(x) for x in (item.provider_template_ids or [])]:
            raise HTTPException(
                status_code=409,
                detail=f"provider template is referenced by catalog item '{item.name}'",
            )
    await db.delete(t)
    await db.commit()
    return Response(status_code=204)


# ── Catalog items ──────────────────────────────────────────────────────


async def _coerce_item(
    db: AsyncSession,
    attrs: dict,
    *,
    on_create: bool,
    current: CatalogItem | None = None,
) -> dict:
    out: dict = {}
    if on_create or "name" in attrs:
        name = str(attrs.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        out["name"] = name
    if on_create or "module-id" in attrs:
        raw = str(attrs.get("module-id", "")).strip()
        if not raw:
            raise HTTPException(status_code=422, detail="module-id is required")
        try:
            module_id = uuid.UUID(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail="module-id is not a UUID") from e
        module = await db.get(RegistryModule, module_id)
        if module is None:
            raise HTTPException(status_code=422, detail="module-id not found")
        out["module_id"] = module_id
    if "display-name" in attrs:
        out["display_name"] = str(attrs["display-name"] or "")
    if "description" in attrs:
        out["description"] = str(attrs["description"] or "")
    if "enabled" in attrs:
        out["enabled"] = bool(attrs["enabled"])
    if "self-contained" in attrs:
        if not isinstance(attrs["self-contained"], bool):
            raise HTTPException(status_code=422, detail="self-contained must be a boolean")
        out["self_contained"] = attrs["self-contained"]
    if "execution-backend-constraints" in attrs:
        constraints = attrs["execution-backend-constraints"]
        if constraints is not None:
            if not isinstance(constraints, dict) or not constraints:
                raise HTTPException(
                    status_code=422,
                    detail="execution-backend-constraints must be a non-empty object or null",
                )
            for backend, spec in constraints.items():
                if backend not in ("tofu", "terraform") or not isinstance(spec, str):
                    raise HTTPException(
                        status_code=422,
                        detail="execution-backend-constraints must map tofu/terraform to version ranges",
                    )
                try:
                    SpecifierSet(spec)
                except InvalidSpecifier as e:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid version constraint for {backend}: {spec}",
                    ) from e
        out["execution_backend_constraints"] = constraints
    if "default-version-pin" in attrs:
        pin = attrs["default-version-pin"]
        out["default_version_pin"] = str(pin) if pin else None
    if "provider-template-ids" in attrs:
        ids = attrs["provider-template-ids"]
        if not isinstance(ids, list):
            raise HTTPException(status_code=422, detail="provider-template-ids must be a list")
        for tid in ids:
            try:
                t = await db.get(ProviderTemplate, uuid.UUID(str(tid)))
            except ValueError as e:
                raise HTTPException(
                    status_code=422, detail=f"provider-template-id '{tid}' is not a UUID"
                ) from e
            if t is None:
                raise HTTPException(status_code=422, detail=f"provider template '{tid}' not found")
        out["provider_template_ids"] = [str(x) for x in ids]
    if "allowed-agent-pool-ids" in attrs:
        ids = attrs["allowed-agent-pool-ids"]
        if ids is None:
            out["allowed_agent_pool_ids"] = None
        elif isinstance(ids, list):
            # The UI/API emit pool ids as "apool-{uuid}" — normalise to bare
            # UUID strings so the provision-time allow-list compare matches.
            normalised: list[str] = []
            for pid in ids:
                try:
                    pool_uuid = uuid.UUID(str(pid).removeprefix("apool-"))
                except ValueError as e:
                    raise HTTPException(
                        status_code=422, detail=f"agent-pool-id '{pid}' is not a UUID"
                    ) from e
                pool = await db.get(AgentPool, pool_uuid)
                if pool is None:
                    raise HTTPException(status_code=422, detail=f"agent pool '{pid}' not found")
                normalised.append(str(pool_uuid))
            out["allowed_agent_pool_ids"] = normalised
        else:
            raise HTTPException(
                status_code=422, detail="allowed-agent-pool-ids must be a list or null"
            )
    if "variable-options" in attrs:
        vo = attrs["variable-options"]
        if not isinstance(vo, list):
            raise HTTPException(status_code=422, detail="variable-options must be a list")
        for i, opt in enumerate(vo):
            if not isinstance(opt, dict) or not str(opt.get("name", "")).strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"variable-options[{i}] must be an object with a non-empty name",
                )
            if "options" in opt and not isinstance(opt["options"], list):
                raise HTTPException(
                    status_code=422,
                    detail=f"variable-options[{i}].options must be a list",
                )
            if "hidden" in opt and not isinstance(opt["hidden"], bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"variable-options[{i}].hidden must be a boolean",
                )
            if "sensitive" in opt and not isinstance(opt["sensitive"], bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"variable-options[{i}].sensitive must be a boolean",
                )
            # A hidden input is removed from the form, so it MUST carry a fixed
            # default — otherwise it can neither be supplied nor wired and a
            # required module input fails opaquely at plan time.
            if opt.get("hidden") and opt.get("default") is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"variable-options[{i}] ('{opt['name']}') is hidden but has no "
                        "default — a hidden input must supply a fixed default value"
                    ),
                )
        out["variable_options"] = vo
    if "labels" in attrs:
        out["labels"] = validate_labels(attrs["labels"] or {})

    # --- the Ansible half (#2019) ---------------------------------------
    #
    # Validate what the CALLER SENT and store it byte for byte -- the v1.9.0
    # rule, because a provider writes the server's response back into state and
    # a trimmed or lower-cased value fails the apply with "Provider produced
    # inconsistent result after apply". Re-validating a stored value is also how
    # a row written by a migration becomes uneditable (#316), which is why each
    # of these is gated on the key being present.
    if "configure-enabled" in attrs:
        raw = attrs["configure-enabled"]
        if not isinstance(raw, bool):
            raise HTTPException(status_code=422, detail="configure-enabled must be a boolean")
        out["configure_enabled"] = raw
    if "ansible-directory" in attrs:
        out["ansible_directory"] = _item_relative_path(
            attrs["ansible-directory"], "ansible-directory"
        )
    if "default-playbook" in attrs:
        out["default_playbook"] = _item_relative_path(attrs["default-playbook"], "default-playbook")
    if "default-limit" in attrs:
        raw = attrs["default-limit"]
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail="default-limit must be a string")
        if raw != raw.strip():
            raise HTTPException(
                status_code=422,
                detail="default-limit: leading or trailing whitespace is not allowed",
            )
        out["default_limit"] = raw

    # A cross-field invariant, so it takes the RESULTING values rather than the
    # submitted ones -- the #1966 shape, and the exception that proves the
    # validate-only-what-was-sent rule either side of it. `configure-enabled`
    # with no directory is incoherent rather than a no-op: it would provision a
    # workspace whose Ansible root is the configuration version's root, where
    # only the generated wrapper lives, and fail at run time for want of a
    # playbook.
    enabled = out.get("configure_enabled", current.configure_enabled if current else False)
    directory = out.get("ansible_directory", current.ansible_directory if current else "")
    if enabled and not directory:
        raise HTTPException(
            status_code=422,
            detail=(
                "configure-enabled needs an ansible-directory naming the subdirectory "
                "of the module that holds the playbook"
            ),
        )
    return out


def _item_relative_path(raw: object, field: str) -> str:
    """A module-relative path, refused rather than normalised.

    The same refusals as the workspace's own Ansible paths (#1929): no traversal,
    no leading or trailing slash, no stray whitespace. Refused rather than
    stripped because this value round-trips through a Terraform provider, and
    silently storing a different one than the caller sent is what fails an apply
    with "Provider produced inconsistent result after apply".
    """
    if not isinstance(raw, str):
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    if len(raw) > 255:
        raise HTTPException(status_code=422, detail=f"{field}: maximum 255 characters")
    if ".." in raw:
        raise HTTPException(status_code=422, detail=f"{field}: path traversal not allowed")
    if raw.startswith("/") or raw.endswith("/"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field}: write the path without a leading or trailing slash "
                f"(it is relative to the module root)"
            ),
        )
    if raw != raw.strip():
        raise HTTPException(
            status_code=422, detail=f"{field}: leading or trailing whitespace is not allowed"
        )
    return raw


@router.get("/catalog-items")
async def list_catalog_items(
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    items = await catalog_service.list_catalog_items(db)
    visible = []
    for item in items:
        caps = await resolve_catalog_capabilities_for(
            db, user, item.name, item.labels or {}, item.owner_email or ""
        )
        if has_capability(caps, cap.CATALOG_READ):
            visible.append(_item_json(item))
    page_items, meta = paginate(visible, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/catalog-items", status_code=201)
async def create_catalog_item(
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    attrs = body.get("data", {}).get("attributes", {})
    fields = await _coerce_item(db, attrs, on_create=True)
    if fields.get("self_contained", False) and fields.get("provider_template_ids"):
        raise HTTPException(
            status_code=422, detail="self-contained catalog items cannot use provider templates"
        )
    if fields.get("self_contained", False) and not fields.get("execution_backend_constraints"):
        raise HTTPException(
            status_code=422, detail="self-contained items require execution-backend-constraints"
        )
    item = CatalogItem(
        id=uuid.uuid4(),
        module_id=fields["module_id"],
        name=fields["name"],
        display_name=fields.get("display_name", ""),
        description=fields.get("description", ""),
        enabled=fields.get("enabled", True),
        self_contained=fields.get("self_contained", False),
        execution_backend_constraints=fields.get("execution_backend_constraints"),
        default_version_pin=fields.get("default_version_pin"),
        provider_template_ids=fields.get("provider_template_ids", []),
        allowed_agent_pool_ids=fields.get("allowed_agent_pool_ids"),
        variable_options=fields.get("variable_options", []),
        configure_enabled=fields.get("configure_enabled", False),
        ansible_directory=fields.get("ansible_directory", ""),
        default_playbook=fields.get("default_playbook", ""),
        default_limit=fields.get("default_limit", ""),
        labels=fields.get("labels", {}),
        owner_email=user.email,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="catalog item name already exists") from e
    await db.refresh(item)
    return JSONResponse(content={"data": _item_json(item)}, status_code=201)


async def _load_item_for_read(
    db: AsyncSession, user: AuthenticatedUser, item_id: str
) -> CatalogItem:
    try:
        item = await catalog_service.get_catalog_item(
            db, parse_id(item_id, detail="catalog item not found")
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail="catalog item not found") from e
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    caps = await resolve_catalog_capabilities_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_capability(caps, cap.CATALOG_READ):
        raise HTTPException(status_code=403, detail="Requires catalog read on this item")
    return item


def _form_version_pin(request: Request, item: CatalogItem) -> tuple[str | None, bool]:
    """Absent selects the item default; explicit latest bypasses that pin."""
    selected = request.query_params.get("version-pin")
    if selected is None:
        return item.default_version_pin, False
    if selected == "latest":
        return None, True
    if not selected.strip():
        raise HTTPException(status_code=422, detail="version-pin must not be empty")
    return selected, True


async def _catalog_version_interface(item: CatalogItem, mv) -> dict:
    """Read root inputs from the archive that provision would run, not cached metadata."""
    if not item.self_contained:
        return {
            "inputs": mv.inputs if mv else None,
            "outputs": mv.outputs if mv else None,
            "error": mv.interface_error if mv else None,
        }
    if mv is None or item.module is None:
        raise HTTPException(
            status_code=422, detail="self-contained item has no uploaded module archive"
        )

    from terrapod.storage import get_storage

    try:
        lock = get_redis_client().lock(
            "tp:catalog:root_interface_read", timeout=120, blocking_timeout=0.25
        )
        if not await lock.acquire():
            raise HTTPException(
                status_code=503,
                detail="Catalog archive reads are busy; retry shortly",
                headers={"Retry-After": "1"},
            )
    except (RedisError, RuntimeError) as e:
        raise HTTPException(status_code=503, detail="Catalog archive reads are unavailable") from e

    async def renew_lock() -> None:
        while True:
            await anyio.sleep(30)
            await lock.extend(120, replace_ttl=True)

    try:
        # Shield the scoped worker: cancellation must not release the permit
        # while a background thread is still parsing an archive.
        with anyio.CancelScope(shield=True):
            error = None
            interface = None
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(renew_lock)
                try:
                    try:
                        path, interface = await catalog_service._stage_root_archive(
                            get_storage(),
                            module_tarball_key(
                                item.module.namespace,
                                item.module.name,
                                item.module.provider,
                                mv.version,
                            ),
                        )
                    except CatalogError as e:
                        error = e
                    else:
                        await catalog_service._remove_root_archive(path)
                finally:
                    tasks.cancel_scope.cancel()
            if error is not None:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from error
            return interface
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await lock.release()
            except (LockError, RedisError):
                logger.error("Could not release catalog archive read permit", exc_info=True)


@router.get("/catalog-items/{item_id}")
async def show_catalog_item(
    item_id: str = Path(...),
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    reveal = _authoring_read_allowed(user, request.query_params.get("include-sensitive-values"))
    item = await _load_item_for_read(db, user, item_id)
    return JSONResponse(
        content={
            "data": _item_json(
                item,
                include_sensitive_values=reveal,
            )
        }
    )


@router.patch("/catalog-items/{item_id}")
async def update_catalog_item(
    item_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await catalog_service.get_catalog_item(
        db, parse_id(item_id, detail="catalog item not found")
    )
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    attrs = body.get("data", {}).get("attributes", {})
    fields = await _coerce_item(db, attrs, on_create=False, current=item)
    if fields.get("self_contained", item.self_contained) and fields.get(
        "provider_template_ids", item.provider_template_ids
    ):
        raise HTTPException(
            status_code=422, detail="self-contained catalog items cannot use provider templates"
        )
    if fields.get("self_contained", item.self_contained) and not fields.get(
        "execution_backend_constraints", item.execution_backend_constraints
    ):
        raise HTTPException(
            status_code=422, detail="self-contained items require execution-backend-constraints"
        )
    if "self_contained" in fields and fields["self_contained"] != item.self_contained:
        if await catalog_service.list_instances(db, item.id, active_only=True):
            raise HTTPException(
                status_code=409, detail="cannot change catalog item mode while instances are active"
            )
    for k, v in fields.items():
        setattr(item, k, v)
    item.updated_at = _now()
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="catalog item name already exists") from e
    await db.refresh(item)
    return JSONResponse(content={"data": _item_json(item)})


@router.delete("/catalog-items/{item_id}", status_code=204)
async def delete_catalog_item(
    item_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    item = await catalog_service.get_catalog_item(
        db, parse_id(item_id, detail="catalog item not found")
    )
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    instances = await catalog_service.list_instances(db, item.id, active_only=True)
    if instances:
        raise HTTPException(
            status_code=409,
            detail=(
                f"catalog item has {len(instances)} active instance(s); "
                "destroy them before deleting the item"
            ),
        )
    await db.delete(item)
    await db.commit()
    return Response(status_code=204)


@router.get("/catalog-items/{item_id}/form")
async def get_catalog_item_form(
    item_id: str = Path(...),
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Derive the provision form (fields the user fills) for a catalog item."""
    item = await _load_item_for_read(db, user, item_id)
    pin, selected = _form_version_pin(request, item)
    mv = await catalog_service._resolve_module_version(db, item.module_id, pin)
    if selected and mv is None:
        raise HTTPException(status_code=422, detail="selected module version is not uploaded")
    interface = await _catalog_version_interface(item, mv)
    module_inputs = interface["inputs"] or []
    templates = []
    for tid in item.provider_template_ids or []:
        t = await db.get(ProviderTemplate, uuid.UUID(str(tid)))
        if t is not None:
            templates.append(t)
    fields = catalog_service.derive_form(item, module_inputs, templates)
    sensitive_options = {
        opt.get("name")
        for opt in item.variable_options or []
        if opt.get("sensitive") or opt.get("hidden")
    }
    fields = [
        {**field, "sensitive": True, "description": "", "default": None, "options": None}
        if field["sensitive"] or field["name"] in sensitive_options
        else field
        for field in fields
    ]
    return JSONResponse(
        content={
            "data": {
                "type": "catalog-item-forms",
                "id": str(item.id),
                "attributes": {
                    "resolved-version": mv.version if mv else None,
                    "fields": fields,
                    # Why the module's interface could not be read (#1707). A
                    # form with no fields is otherwise indistinguishable from a
                    # module that genuinely declares no variables.
                    "interface-error": interface["error"],
                },
            }
        }
    )


@router.get("/catalog-items/{item_id}/interface")
async def get_catalog_item_interface(
    item_id: str = Path(...),
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The selected version's inputs and outputs, from the root archive when self-contained."""
    item = await _load_item_for_read(db, user, item_id)
    pin, selected = _form_version_pin(request, item)
    mv = await catalog_service._resolve_module_version(db, item.module_id, pin)
    if selected and mv is None:
        raise HTTPException(status_code=422, detail="selected module version is not uploaded")
    interface = await _catalog_version_interface(item, mv)
    sensitive_options = {
        opt.get("name")
        for opt in item.variable_options or []
        if opt.get("sensitive") or opt.get("hidden")
    }
    public_inputs = (
        [
            {**inp, "sensitive": True, "description": "", "default": None, "options": None}
            if inp.get("sensitive") or inp.get("name") in sensitive_options
            else dict(inp)
            for inp in interface["inputs"] or []
        ]
        if interface["inputs"] is not None
        else None
    )
    return JSONResponse(
        content={
            "data": {
                "type": "catalog-item-interfaces",
                "id": str(item.id),
                "attributes": {
                    "resolved-version": mv.version if mv else None,
                    "inputs": public_inputs,
                    "outputs": interface["outputs"],
                    "interface-error": interface["error"],
                },
            }
        }
    )


@router.get("/catalog-items/{item_id}/instances")
async def list_catalog_item_instances(
    item_id: str = Path(...),
    request: Request = None,
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await _load_item_for_read(db, user, item_id)
    instances = await catalog_service.list_instances(db, item.id)
    items = [_instance_json(w) for w in instances]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/catalog-items/{item_id}/provision", status_code=201)
async def provision_catalog_item(
    item_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Provision a workspace from a catalog item. Requires catalog 'use' on the
    item AND 'write' on the chosen agent pool (which must be in the item's
    allowed pools, when restricted)."""
    item = await catalog_service.get_catalog_item(
        db, parse_id(item_id, detail="catalog item not found")
    )
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    if not item.enabled:
        raise HTTPException(status_code=409, detail="catalog item is disabled")

    caps = await resolve_catalog_capabilities_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_capability(caps, cap.CATALOG_USE):
        raise HTTPException(status_code=403, detail="Requires catalog 'use' on this item")

    attrs = body.get("data", {}).get("attributes", {})
    name = str(attrs.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    raw_pool = str(attrs.get("agent-pool-id", "")).strip()
    if not raw_pool:
        raise HTTPException(status_code=422, detail="agent-pool-id is required")
    try:
        # The API + UI emit pool ids as "apool-{uuid}" — strip the prefix
        # before parsing (matches tfe_v2 workspace pool assignment).
        pool_id = uuid.UUID(raw_pool.removeprefix("apool-"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail="agent-pool-id is not a UUID") from e
    pool = await db.get(AgentPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=422, detail="agent pool not found")

    # Pool must be in the item's allow-list (when the item restricts pools).
    if item.allowed_agent_pool_ids is not None:
        if str(pool_id) not in [str(x) for x in item.allowed_agent_pool_ids]:
            raise HTTPException(
                status_code=403,
                detail="agent pool is not allowed for this catalog item",
            )

    # User must have pool:assign to assign the pool (pool RBAC intersection).
    pool_caps = await resolve_pool_capabilities_for(
        db, user, pool.name, pool.labels or {}, pool.owner_email
    )
    if not has_capability(pool_caps, cap.POOL_ASSIGN):
        raise HTTPException(status_code=403, detail="Requires write permission on the agent pool")

    input_values = attrs.get("input-values", {})
    if not isinstance(input_values, dict):
        raise HTTPException(status_code=422, detail="input-values must be an object")
    version_pin = attrs.get("version-pin", item.default_version_pin)
    version_pin = str(version_pin) if version_pin else None
    backend = attrs.get("execution-backend")
    engine_version = attrs.get("engine-version")
    if engine_version is not None and (not isinstance(engine_version, str) or not engine_version):
        raise HTTPException(status_code=422, detail="engine-version must be a non-empty string")
    auto_apply = bool(attrs.get("auto-apply", False))
    labels = validate_labels(attrs.get("labels", {}) or {})

    try:
        ws = await catalog_service.provision_instance(
            db,
            user_email=user.email,
            item=item,
            name=name,
            agent_pool_id=pool_id,
            input_values=input_values,
            version_pin=version_pin,
            auto_apply=auto_apply,
            labels=labels,
            execution_backend=backend,
            engine_version=engine_version,
        )
    except CatalogError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    # GHSA-49q6-pm68-3xgw, through the catalog door. Provisioning takes
    # caller-supplied `labels` and needs only catalog `use` plus pool `write` —
    # nowhere near platform admin — so without this a non-admin could label a
    # provisioned workspace into another team's rule-assigned variable set and
    # receive its secrets in the run the provision queues. The ordinary create path
    # is gated in `tfe_v2`; this is the same check at the other entry point, which
    # constructs its Workspace directly and so does not pass through it.
    from terrapod.services.varset_self_join import refuse_varset_growth

    await db.flush()
    try:
        await refuse_varset_growth(
            db,
            workspace_id=ws.id,
            before=set(),
            is_platform_admin="admin" in effective_platform_roles(user),
            actor_email=user.email,
        )
    except Exception:
        await db.rollback()
        raise

    try:
        await db.commit()
    except IntegrityError as e:
        # A concurrent provision raced the name-uniqueness SELECT and lost the
        # workspace unique constraint — surface as 409, not an opaque 500.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"A workspace named '{name}' already exists"
        ) from e
    await db.refresh(ws)
    return JSONResponse(content={"data": _instance_json(ws)}, status_code=201)


# ── Catalog instance lifecycle (#535 P2) ───────────────────────────────


def _run_ref(run) -> dict:
    """Minimal run reference returned from lifecycle actions."""
    return {
        "id": f"run-{run.id}",
        "type": "runs",
        "attributes": {"status": run.status, "is-destroy": run.is_destroy},
        "links": {"self": f"/api/v1/runs/run-{run.id}"},
    }


async def _load_instance(
    db: AsyncSession,
    user: AuthenticatedUser,
    ws_id: str,
    *,
    required_cap: str,
    active_only: bool = False,
) -> Workspace:
    """Load a catalog instance workspace and enforce the `required_cap` catalog
    capability on its originating item. Lifecycle actions are gated by catalog
    'use' on the item — NOT by workspace permission (the clamp gives the
    provisioner read only; the catalog surface is the control plane).

    ``active_only`` rejects (409) an instance that's already been torn down
    (``lifecycle_state == "archived"``), so reconfigure/destroy can't queue a
    run against a destroyed instance."""
    try:
        ws = await db.get(Workspace, uuid.UUID(ws_id.removeprefix("ws-")))
    except ValueError as e:
        raise HTTPException(status_code=404, detail="catalog instance not found") from e
    if ws is None or ws.catalog_item_id is None:
        raise HTTPException(status_code=404, detail="catalog instance not found")
    if active_only and ws.lifecycle_state == "archived":
        raise HTTPException(
            status_code=409, detail="catalog instance is archived (already destroyed)"
        )
    item = await db.get(CatalogItem, ws.catalog_item_id)
    if item is None:
        raise HTTPException(status_code=409, detail="catalog item no longer exists")
    caps = await resolve_catalog_capabilities_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_capability(caps, required_cap):
        raise HTTPException(status_code=403, detail=f"Requires '{required_cap}' on this item")
    return ws


@router.get("/catalog-instances/{ws_id}")
async def show_catalog_instance(
    ws_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_READ)
    attrs = _instance_json(ws)["attributes"]
    item = await db.get(CatalogItem, ws.catalog_item_id)
    if item is None:
        raise HTTPException(status_code=409, detail="catalog item no longer exists")
    safe_keys: set[str] = set()
    # Each successful materialisation uploads a catalog CV after refreshing the
    # saved inputs. Older snapshots without one cannot be classified safely.
    cv_result = await db.execute(
        select(ConfigurationVersion.created_at)
        .where(
            ConfigurationVersion.workspace_id == ws.id,
            ConfigurationVersion.source == "catalog",
            ConfigurationVersion.status == "uploaded",
            ConfigurationVersion.speculative.is_(False),
        )
        .order_by(ConfigurationVersion.created_at.desc(), ConfigurationVersion.id.desc())
        .limit(1)
    )
    snapshot_at = cv_result.scalar_one_or_none()
    if (
        isinstance(snapshot_at, datetime)
        and snapshot_at.tzinfo is not None
        and isinstance(item.updated_at, datetime)
        and item.updated_at.tzinfo is not None
        and item.updated_at <= snapshot_at
    ):
        unsafe_keys = {
            opt.get("name")
            for opt in item.variable_options or []
            if opt.get("hidden") or opt.get("sensitive")
        }
        template_names: set[str] = set()
        templates_current = not (item.self_contained and item.provider_template_ids)
        for tid in item.provider_template_ids or []:
            try:
                template = await db.get(ProviderTemplate, uuid.UUID(str(tid)))
            except (ValueError, TypeError):
                template = None
            if (
                template is None
                or not isinstance(template.updated_at, datetime)
                or template.updated_at.tzinfo is None
                or template.updated_at > snapshot_at
            ):
                templates_current = False
                break
            for param in template.parameters or []:
                if (
                    not isinstance(param, dict)
                    or not isinstance(param.get("name"), str)
                    or not param["name"].strip()
                    or not isinstance(param.get("sensitive", False), bool)
                ):
                    templates_current = False
                    break
                template_names.add(param["name"])
                if param.get("sensitive"):
                    unsafe_keys.add(param["name"])
            if not templates_current:
                break

        if templates_current:
            # The variable service stores the former "terraform" category as "native".
            result = await db.execute(
                select(Variable.key, Variable.sensitive, Variable.value_source).where(
                    Variable.workspace_id == ws.id, Variable.category.in_(("native", "terraform"))
                )
            )
            for key, sensitive, source in result.all():
                if sensitive is False and source == "static":
                    safe_keys.add(key)
                else:
                    unsafe_keys.add(key)
            safe_keys.difference_update(unsafe_keys)

            # A floating root may have changed versions since the snapshot.
            # Pinned roots use the actual archive; wrappers use registry metadata.
            if safe_keys and (not item.self_contained or ws.catalog_version_pin):
                mv = await catalog_service._resolve_module_version(
                    db, item.module_id, ws.catalog_version_pin
                )
                if (
                    mv is not None
                    and isinstance(mv.updated_at, datetime)
                    and mv.updated_at.tzinfo is not None
                    and mv.updated_at <= snapshot_at
                ):
                    if item.self_contained:
                        try:
                            interface = await _catalog_version_interface(item, mv)
                        except HTTPException:
                            interface = None
                    else:
                        interface = {"inputs": mv.inputs, "error": mv.interface_error}
                    if interface and not interface["error"] and interface["inputs"] is not None:
                        classified: dict[str, bool] = {}
                        for inp in interface["inputs"]:
                            if inp.get("name"):
                                name = inp["name"]
                                classified[name] = (
                                    classified.get(name, True) and inp.get("sensitive") is False
                                )
                        safe_keys.intersection_update(classified.keys() | template_names)
                        safe_keys.difference_update(
                            name for name, is_safe in classified.items() if not is_safe
                        )
                    else:
                        safe_keys.clear()
                else:
                    safe_keys.clear()
            else:
                safe_keys.clear()
    attrs["input-values"] = {
        key: value for key, value in (ws.catalog_input_values or {}).items() if key in safe_keys
    }
    return JSONResponse(
        content={
            "data": {
                "id": str(ws.id),
                "type": "catalog-instances",
                "attributes": attrs,
                "links": {"self": f"/api/v1/catalog-instances/{ws.id}"},
            }
        }
    )


@router.patch("/catalog-instances/{ws_id}")
async def reconfigure_catalog_instance(
    ws_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a catalog instance's inputs and/or version pin, then queue a run.
    Requires catalog 'use' on the originating item. A non-auto-apply run plans
    and waits for a platform-admin confirm (the clamp blocks 'use' holders from
    confirming via the workspace API); pass auto-apply to apply directly."""
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_USE, active_only=True)
    attrs = body.get("data", {}).get("attributes", {})
    input_values = attrs.get("input-values")
    input_values_implicit = input_values is None
    if input_values is None:
        input_values = dict(ws.catalog_input_values or {})
    if not isinstance(input_values, dict):
        raise HTTPException(status_code=422, detail="input-values must be an object")
    # version-pin: absent → keep current; null → float; value → pin.
    if "version-pin" in attrs:
        vp = attrs["version-pin"]
        version_pin = str(vp) if vp else None
    else:
        version_pin = ws.catalog_version_pin
    auto_apply = bool(attrs.get("auto-apply", False))
    backend = attrs.get("execution-backend")
    engine_version = attrs.get("engine-version")
    if engine_version is not None and (not isinstance(engine_version, str) or not engine_version):
        raise HTTPException(status_code=422, detail="engine-version must be a non-empty string")

    try:
        run = await catalog_service.reconfigure_instance(
            db,
            user_email=user.email,
            ws=ws,
            input_values=input_values,
            input_values_implicit=input_values_implicit,
            version_pin=version_pin,
            auto_apply=auto_apply,
            execution_backend=backend,
            engine_version=engine_version,
        )
    except CatalogError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    await db.commit()
    return JSONResponse(content={"data": _run_ref(run)})


@router.post("/catalog-instances/{ws_id}/destroy", status_code=201)
async def destroy_catalog_instance(
    ws_id: str = Path(...),
    body: dict = Body(default={}),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Queue a destroy run for a catalog instance. Requires catalog 'use' on the
    originating item. On a successful apply the workspace is archived. As with
    reconfigure, a non-auto-apply destroy plans and waits for an admin confirm;
    pass auto-apply to tear down directly."""
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_USE, active_only=True)
    attrs = body.get("data", {}).get("attributes", {}) if body else {}
    auto_apply = bool(attrs.get("auto-apply", False))
    try:
        run = await catalog_service.destroy_instance(
            db, user_email=user.email, ws=ws, auto_apply=auto_apply
        )
    except CatalogError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    await db.commit()
    return JSONResponse(content={"data": _run_ref(run)}, status_code=201)


async def _latest_run(db: AsyncSession, workspace_id: uuid.UUID) -> Run | None:
    result = await db.execute(
        select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


# Sources of catalog-initiated apply-capable runs (provision/reconfigure →
# "catalog", destroy → "catalog-lifecycle"). The confirm/discard endpoints act
# ONLY on these, never on a speculative module-impact run. Catalog instances
# carry a ModuleWorkspaceLink, so module-impact analysis can queue a plan-only
# "module-test" (or full-apply "module-publish") run on them; without this guard
# a catalog `use` holder could promote a speculative module-test plan to a real
# apply via confirm.
_CATALOG_RUN_SOURCES = ("catalog", "catalog-lifecycle")


def _is_confirmable_catalog_run(run: Run | None) -> bool:
    return (
        run is not None
        and run.status == "planned"
        and not run.plan_only
        and run.source in _CATALOG_RUN_SOURCES
    )


@router.post("/catalog-instances/{ws_id}/confirm")
async def confirm_catalog_instance_run(
    ws_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Confirm the instance's pending **planned** run for apply. Requires catalog
    `use`. This is the catalog-surface confirm: the workspace clamp gives the
    provisioner only `read`, so a non-auto-apply provision/reconfigure/destroy
    is confirmed here rather than via the workspace run API (which would need a
    platform admin)."""
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_USE)
    run = await _latest_run(db, ws.id)
    if not _is_confirmable_catalog_run(run):
        raise HTTPException(status_code=409, detail="no planned run awaiting confirmation")
    try:
        run = await run_service.confirm_run(db, run)
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return JSONResponse(content={"data": _run_ref(run)})


@router.post("/catalog-instances/{ws_id}/discard")
async def discard_catalog_instance_run(
    ws_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Discard the instance's pending **planned** run. Requires catalog `use`.
    Catalog-surface counterpart of confirm (the clamp blocks the workspace run
    API for the provisioner)."""
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_USE)
    run = await _latest_run(db, ws.id)
    if not _is_confirmable_catalog_run(run):
        raise HTTPException(status_code=409, detail="no planned run to discard")
    try:
        run = await run_service.discard_run(db, run)
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return JSONResponse(content={"data": _run_ref(run)})


@router.delete("/catalog-instances/{ws_id}", status_code=204)
async def delete_catalog_instance(
    ws_id: str = Path(...),
    orphan: bool = Query(False),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a catalog instance.

    Catalog instances are **destroyed**, not deleted-and-orphaned like a plain
    workspace. The recommended teardown is ``POST .../destroy``, which runs
    `terraform destroy` and archives the workspace on a successful apply — the
    infrastructure is reclaimed.

    This endpoint is the explicit, **discouraged** escape hatch: with
    ``?orphan=true`` it deletes the workspace record and **abandons** the
    provisioned infrastructure (it keeps running, untracked). Requires catalog
    ``admin`` on the originating item. Without ``orphan=true`` it refuses (409)
    and points at destroy — there is no way to orphan a catalog instance by
    accident.
    """
    ws = await _load_instance(db, user, ws_id, required_cap=cap.CATALOG_ADMIN)
    if not orphan:
        raise HTTPException(
            status_code=409,
            detail=(
                "Refusing to delete a catalog instance without reclaiming its "
                "infrastructure. POST .../destroy to destroy and remove it, or pass "
                "?orphan=true to delete the workspace and abandon its infrastructure "
                "(discouraged — the infrastructure keeps running, untracked)."
            ),
        )
    ws_name = ws.name
    await db.delete(ws)
    await db.commit()
    logger.warning(
        "Catalog instance orphaned: workspace deleted, infrastructure abandoned",
        workspace=ws_name,
        user=user.email,
    )
    return Response(status_code=204)
