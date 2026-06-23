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

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_admin_or_audit,
)
from terrapod.api.labels import validate_labels
from terrapod.config import settings
from terrapod.db.models import (
    AgentPool,
    CatalogItem,
    ProviderTemplate,
    RegistryModule,
    Workspace,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import catalog_service
from terrapod.services.catalog_rbac_service import (
    has_catalog_permission,
    resolve_catalog_permission_for,
)
from terrapod.services.catalog_service import CatalogError
from terrapod.services.pool_rbac_service import (
    has_pool_permission,
    resolve_pool_permission_for,
)

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


def _template_json(t: ProviderTemplate) -> dict:
    return {
        "id": str(t.id),
        "type": "provider-templates",
        "attributes": {
            "name": t.name,
            "provider-type": t.provider_type,
            "body": t.body,
            "parameters": list(t.parameters or []),
            "labels": dict(t.labels or {}),
            "owner-email": t.owner_email or "",
            "created-at": _rfc3339(t.created_at),
            "updated-at": _rfc3339(t.updated_at),
        },
        "links": {"self": f"/api/terrapod/v1/provider-templates/{t.id}"},
    }


def _item_json(item: CatalogItem) -> dict:
    module = item.module
    return {
        "id": str(item.id),
        "type": "catalog-items",
        "attributes": {
            "name": item.name,
            "display-name": item.display_name or "",
            "description": item.description or "",
            "enabled": item.enabled,
            "module-id": str(item.module_id),
            "module-name": module.name if module else "",
            "module-provider": module.provider if module else "",
            "default-version-pin": item.default_version_pin,
            "provider-template-ids": [str(x) for x in (item.provider_template_ids or [])],
            "allowed-agent-pool-ids": (
                [str(x) for x in item.allowed_agent_pool_ids]
                if item.allowed_agent_pool_ids is not None
                else None
            ),
            "variable-options": list(item.variable_options or []),
            "labels": dict(item.labels or {}),
            "owner-email": item.owner_email or "",
            "created-at": _rfc3339(item.created_at),
            "updated-at": _rfc3339(item.updated_at),
        },
        "links": {"self": f"/api/terrapod/v1/catalog-items/{item.id}"},
    }


def _instance_json(ws: Workspace) -> dict:
    return {
        "id": str(ws.id),
        "type": "catalog-instances",
        "attributes": {
            "name": ws.name,
            "catalog-item-id": str(ws.catalog_item_id) if ws.catalog_item_id else None,
            "catalog-version-pin": ws.catalog_version_pin,
            "agent-pool-id": str(ws.agent_pool_id) if ws.agent_pool_id else None,
            "owner-email": ws.owner_email or "",
            "labels": dict(ws.labels or {}),
        },
        "links": {"self": f"/api/terrapod/v1/workspaces/ws-{ws.id}"},
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
        out["parameters"] = params
    if "labels" in attrs:
        out["labels"] = validate_labels(attrs["labels"] or {})
    return out


@router.get("/provider-templates")
async def list_provider_templates(
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    templates = await catalog_service.list_provider_templates(db)
    return JSONResponse(content={"data": [_template_json(t) for t in templates]})


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
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    t = await catalog_service.get_provider_template(db, uuid.UUID(template_id))
    if t is None:
        raise HTTPException(status_code=404, detail="provider template not found")
    return JSONResponse(content={"data": _template_json(t)})


@router.patch("/provider-templates/{template_id}")
async def update_provider_template(
    template_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    t = await catalog_service.get_provider_template(db, uuid.UUID(template_id))
    if t is None:
        raise HTTPException(status_code=404, detail="provider template not found")
    attrs = body.get("data", {}).get("attributes", {})
    for k, v in _coerce_template(attrs, on_create=False).items():
        setattr(t, k, v)
    t.updated_at = _now()
    await db.commit()
    await db.refresh(t)
    return JSONResponse(content={"data": _template_json(t)})


@router.delete("/provider-templates/{template_id}", status_code=204)
async def delete_provider_template(
    template_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    t = await catalog_service.get_provider_template(db, uuid.UUID(template_id))
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


async def _coerce_item(db: AsyncSession, attrs: dict, *, on_create: bool) -> dict:
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
            for pid in ids:
                try:
                    pool = await db.get(AgentPool, uuid.UUID(str(pid)))
                except ValueError as e:
                    raise HTTPException(
                        status_code=422, detail=f"agent-pool-id '{pid}' is not a UUID"
                    ) from e
                if pool is None:
                    raise HTTPException(status_code=422, detail=f"agent pool '{pid}' not found")
            out["allowed_agent_pool_ids"] = [str(x) for x in ids]
        else:
            raise HTTPException(
                status_code=422, detail="allowed-agent-pool-ids must be a list or null"
            )
    if "variable-options" in attrs:
        vo = attrs["variable-options"]
        if not isinstance(vo, list):
            raise HTTPException(status_code=422, detail="variable-options must be a list")
        out["variable_options"] = vo
    if "labels" in attrs:
        out["labels"] = validate_labels(attrs["labels"] or {})
    return out


@router.get("/catalog-items")
async def list_catalog_items(
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    items = await catalog_service.list_catalog_items(db)
    visible = []
    for item in items:
        perm = await resolve_catalog_permission_for(
            db, user, item.name, item.labels or {}, item.owner_email or ""
        )
        if has_catalog_permission(perm, "read"):
            visible.append(item)
    return JSONResponse(content={"data": [_item_json(i) for i in visible]})


@router.post("/catalog-items", status_code=201)
async def create_catalog_item(
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    attrs = body.get("data", {}).get("attributes", {})
    fields = await _coerce_item(db, attrs, on_create=True)
    item = CatalogItem(
        id=uuid.uuid4(),
        module_id=fields["module_id"],
        name=fields["name"],
        display_name=fields.get("display_name", ""),
        description=fields.get("description", ""),
        enabled=fields.get("enabled", True),
        default_version_pin=fields.get("default_version_pin"),
        provider_template_ids=fields.get("provider_template_ids", []),
        allowed_agent_pool_ids=fields.get("allowed_agent_pool_ids"),
        variable_options=fields.get("variable_options", []),
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
        item = await catalog_service.get_catalog_item(db, uuid.UUID(item_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail="catalog item not found") from e
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    perm = await resolve_catalog_permission_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_catalog_permission(perm, "read"):
        raise HTTPException(status_code=403, detail="Requires catalog read on this item")
    return item


@router.get("/catalog-items/{item_id}")
async def show_catalog_item(
    item_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await _load_item_for_read(db, user, item_id)
    return JSONResponse(content={"data": _item_json(item)})


@router.patch("/catalog-items/{item_id}")
async def update_catalog_item(
    item_id: str = Path(...),
    body: dict = Body(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await catalog_service.get_catalog_item(db, uuid.UUID(item_id))
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    attrs = body.get("data", {}).get("attributes", {})
    for k, v in (await _coerce_item(db, attrs, on_create=False)).items():
        setattr(item, k, v)
    item.updated_at = _now()
    await db.commit()
    await db.refresh(item)
    return JSONResponse(content={"data": _item_json(item)})


@router.delete("/catalog-items/{item_id}", status_code=204)
async def delete_catalog_item(
    item_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    item = await catalog_service.get_catalog_item(db, uuid.UUID(item_id))
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    instances = await catalog_service.list_instances(db, item.id)
    if instances:
        raise HTTPException(
            status_code=409,
            detail=(
                f"catalog item has {len(instances)} provisioned instance(s); "
                "destroy them before deleting the item"
            ),
        )
    await db.delete(item)
    await db.commit()
    return Response(status_code=204)


@router.get("/catalog-items/{item_id}/form")
async def get_catalog_item_form(
    item_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Derive the provision form (fields the user fills) for a catalog item."""
    item = await _load_item_for_read(db, user, item_id)
    pin = item.default_version_pin
    mv = await catalog_service._resolve_module_version(db, item.module_id, pin)
    module_inputs = (mv.inputs if mv else None) or []
    templates = []
    for tid in item.provider_template_ids or []:
        t = await db.get(ProviderTemplate, uuid.UUID(str(tid)))
        if t is not None:
            templates.append(t)
    fields = catalog_service.derive_form(item, module_inputs, templates)
    return JSONResponse(
        content={
            "data": {
                "type": "catalog-item-forms",
                "id": str(item.id),
                "attributes": {
                    "resolved-version": mv.version if mv else None,
                    "fields": fields,
                },
            }
        }
    )


@router.get("/catalog-items/{item_id}/instances")
async def list_catalog_item_instances(
    item_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await _load_item_for_read(db, user, item_id)
    instances = await catalog_service.list_instances(db, item.id)
    return JSONResponse(content={"data": [_instance_json(w) for w in instances]})


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
    item = await catalog_service.get_catalog_item(db, uuid.UUID(item_id))
    if item is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    if not item.enabled:
        raise HTTPException(status_code=409, detail="catalog item is disabled")

    perm = await resolve_catalog_permission_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_catalog_permission(perm, "use"):
        raise HTTPException(status_code=403, detail="Requires catalog 'use' on this item")

    attrs = body.get("data", {}).get("attributes", {})
    name = str(attrs.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    raw_pool = str(attrs.get("agent-pool-id", "")).strip()
    if not raw_pool:
        raise HTTPException(status_code=422, detail="agent-pool-id is required")
    try:
        pool_id = uuid.UUID(raw_pool)
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

    # User must have write on the pool to assign it (pool RBAC intersection).
    pool_perm = await resolve_pool_permission_for(
        db, user, pool.name, pool.labels or {}, pool.owner_email
    )
    if not has_pool_permission(pool_perm, "write"):
        raise HTTPException(status_code=403, detail="Requires write permission on the agent pool")

    input_values = attrs.get("input-values", {})
    if not isinstance(input_values, dict):
        raise HTTPException(status_code=422, detail="input-values must be an object")
    version_pin = attrs.get("version-pin")
    version_pin = str(version_pin) if version_pin else None
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
        )
    except CatalogError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    await db.commit()
    await db.refresh(ws)
    return JSONResponse(content={"data": _instance_json(ws)}, status_code=201)


# ── Catalog instance lifecycle (#535 P2) ───────────────────────────────


def _run_ref(run) -> dict:
    """Minimal run reference returned from lifecycle actions."""
    return {
        "id": f"run-{run.id}",
        "type": "runs",
        "attributes": {"status": run.status, "is-destroy": run.is_destroy},
        "links": {"self": f"/api/terrapod/v1/runs/run-{run.id}"},
    }


async def _load_instance(
    db: AsyncSession, user: AuthenticatedUser, ws_id: str, *, required: str
) -> Workspace:
    """Load a catalog instance workspace and enforce `required` catalog
    permission on its originating item. Lifecycle actions are gated by catalog
    'use' on the item — NOT by workspace permission (the clamp gives the
    provisioner read only; the catalog surface is the control plane)."""
    try:
        ws = await db.get(Workspace, uuid.UUID(ws_id.removeprefix("ws-")))
    except ValueError as e:
        raise HTTPException(status_code=404, detail="catalog instance not found") from e
    if ws is None or ws.catalog_item_id is None:
        raise HTTPException(status_code=404, detail="catalog instance not found")
    item = await db.get(CatalogItem, ws.catalog_item_id)
    if item is None:
        raise HTTPException(status_code=409, detail="catalog item no longer exists")
    perm = await resolve_catalog_permission_for(
        db, user, item.name, item.labels or {}, item.owner_email or ""
    )
    if not has_catalog_permission(perm, required):
        raise HTTPException(status_code=403, detail=f"Requires catalog '{required}' on this item")
    return ws


@router.get("/catalog-instances/{ws_id}")
async def show_catalog_instance(
    ws_id: str = Path(...),
    _: None = Depends(require_catalog_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    ws = await _load_instance(db, user, ws_id, required="read")
    attrs = _instance_json(ws)["attributes"]
    attrs["input-values"] = dict(ws.catalog_input_values or {})
    return JSONResponse(
        content={
            "data": {
                "id": str(ws.id),
                "type": "catalog-instances",
                "attributes": attrs,
                "links": {"self": f"/api/terrapod/v1/catalog-instances/{ws.id}"},
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
    ws = await _load_instance(db, user, ws_id, required="use")
    attrs = body.get("data", {}).get("attributes", {})
    input_values = attrs.get("input-values")
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

    try:
        run = await catalog_service.reconfigure_instance(
            db,
            user_email=user.email,
            ws=ws,
            input_values=input_values,
            version_pin=version_pin,
            auto_apply=auto_apply,
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
    ws = await _load_instance(db, user, ws_id, required="use")
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
