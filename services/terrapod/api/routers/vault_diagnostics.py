"""Vault diagnostics endpoints (#1663).

UX CONTRACT: consumed by the web frontend:
  - web/src/app/admin/vault/page.tsx (instance status)
  - web/src/components/vault-reference-check.tsx (the "Check" action on the
    Vault reference form, in the workspace and variable-set variable forms)
Changes to response shapes, attribute names or status codes here MUST be
matched there, in go-terrapod (vault.go) and in the MCP tools.

Endpoints:
    GET  /api/terrapod/v1/admin/vault                              (admin, audit)
    POST /api/terrapod/v1/workspaces/{id}/vault-reference-checks   (var:write)
    POST /api/terrapod/v1/varsets/{id}/vault-reference-checks      (admin)

Mounted whether or not Vault is enabled, so the route surface does not depend
on configuration. With Vault off the status is an empty list and a check
reports the value source as disabled; the sampling task is never registered.
"""

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_admin_or_audit,
)
from terrapod.api.pagination import paginate
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import VariableSet, VariableSetVariable, Workspace
from terrapod.db.session import get_db
from terrapod.services import audit_service, variable_service, vault_diagnostics
from terrapod.services.vault_source_service import vault_check_audit_entries
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["vault"])


def _instance_json(i: dict) -> dict:
    """One instance's sampled status. Names, states and messages — never a value."""
    return {
        "type": "vault-instance-statuses",
        "id": i["name"],
        "attributes": {
            "name": i["name"],
            "default": i["default"],
            "address": i["address"],
            "namespace": i["namespace"],
            "auth-method": i["auth-method"],
            "auth-mount": i["auth-mount"],
            "auth-role": i["auth-role"],
            # instance-ca | global-bundle | default | skip-verify
            "tls-trust": i["tls-trust"],
            # Null throughout means "not sampled yet", never "false".
            "reachable": i["reachable"],
            "initialized": i["initialized"],
            "sealed": i["sealed"],
            "standby": i["standby"],
            "version": i["version"],
            "health-error": i["health-error"],
            "login-ok": i["login-ok"],
            "login-error": i["login-error"],
            "ttl-seconds": i["ttl-seconds"],
            "checked-at": i["checked-at"],
            # {class, message, at} of the last failed resolution, or null.
            "last-error": i["last-error"],
        },
    }


@router.get("/admin/vault")
async def vault_status(
    request: Request,
    _user: AuthenticatedUser = Depends(require_admin_or_audit),
) -> JSONResponse:
    """Per-instance Vault status, from the periodic sample in Redis.

    Never contacts Vault: the sample is taken by the ``vault_status`` scheduler
    task on one replica, so opening this page cannot log in to every Vault from
    every browser tab. Admin or audit, because addresses and auth settings are
    infrastructure detail.
    """
    st = await vault_diagnostics.read_status()
    items = [_instance_json(i) for i in st["instances"]]
    page, meta = paginate(items, request)
    meta["vault"] = {
        "enabled": st["enabled"],
        "sampled-at": st["sampled-at"],
        "unavailable-reason": st["unavailable-reason"],
    }
    return JSONResponse(content={"data": page, "meta": meta})


def _check_json(a: dict) -> dict:
    """A reference check's result. Key names for kv-v2; never a value."""
    return {
        "type": "vault-reference-checks",
        # Nothing is stored: the id only lets a client tell two answers apart.
        "id": f"vrc-{uuid.uuid4()}",
        "attributes": {
            "ok": a["ok"],
            "vault-enabled": a["vault-enabled"],
            "parses": a["parses"],
            "parse-error": a["parse-error"],
            "instance": a["instance"],
            "instance-known": a["instance-known"],
            "engine": a["engine"],
            "read-path": a["read-path"],
            "path-allowed": a["path-allowed"],
            "readable": a["readable"],
            "capabilities": a["capabilities"],
            "required-capabilities": a["required-capabilities"],
            "keys": a["keys"],
            "fields-present": a["fields-present"],
            "missing-fields": a["missing-fields"],
            "notes": a["notes"],
            "checks": a["checks"],
        },
    }


def _attrs(body: dict) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attrs, dict):
        raise HTTPException(status_code=422, detail="expected data.attributes")
    return attrs


def _var_uuid(raw: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw).removeprefix("var-"))
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Variable not found") from e


def _reference_attr(attrs: dict) -> object:
    if "reference" not in attrs or attrs["reference"] in (None, ""):
        raise HTTPException(
            status_code=422,
            detail="supply `reference` (an OpenBao/Vault reference object) or `variable-id`",
        )
    ref = attrs["reference"]
    if not isinstance(ref, (dict, str)):
        raise HTTPException(status_code=422, detail="`reference` must be an object")
    return ref


def _stored_reference(var) -> object:
    if var.value_source != "vault":
        raise HTTPException(
            status_code=422,
            detail="that variable's value source is not 'vault'; there is no reference to check",
        )
    return var.value or ""


async def _rate_limit(user: AuthenticatedUser) -> None:
    allowed, retry_after = await vault_diagnostics.check_rate_allowed(user.email or "anonymous")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"at most {vault_diagnostics.CHECKS_PER_MINUTE} OpenBao/Vault reference "
            "checks a minute; try again shortly",
            headers={"Retry-After": str(retry_after)},
        )


async def _audit_reads(
    db: AsyncSession,
    user: AuthenticatedUser,
    resource_type: str,
    resource_id: str,
    reads: list,
) -> None:
    """Record the Vault reads a check made, if it made any (#1688).

    The middleware's own row names the endpoint, not the instance, mount and
    path that were read, so without this an operator reconciling Terrapod's
    audit log against the server's own could not attribute those reads.
    """
    if not reads:
        return
    audit_service.add_audit_events(
        db,
        vault_check_audit_entries(
            actor_email=user.email,
            resource_type=resource_type,
            resource_id=resource_id,
            reads=reads,
        ),
    )
    await db.commit()


@router.post("/workspaces/{workspace_id}/vault-reference-checks")
async def check_workspace_reference(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Check a Vault reference, or a workspace variable's stored one.

    Needs ``var:write`` on the workspace: the check answers for whoever could
    create the variable. Listing a kv-v2 secret's key names additionally needs
    ``run:plan`` — someone who could create the variable *and* run a plan with
    it could deliver the secret itself, so the names give them nothing more.
    Anyone else gets every other check, and a note saying why keys are absent.
    """
    ws_uuid = workspace_id.removeprefix("ws-")
    try:
        uuid.UUID(ws_uuid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    ws = (await db.execute(select(Workspace).where(Workspace.id == ws_uuid))).scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.VAR_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires write permission on workspace",
        )

    attrs = _attrs(body)
    key = str(attrs.get("key") or "")
    if attrs.get("variable-id"):
        var = await variable_service.get_variable(db, ws.id, _var_uuid(attrs["variable-id"]))
        if var is None:
            raise HTTPException(status_code=404, detail="Variable not found")
        reference = _stored_reference(var)
        key = key or var.key
    else:
        reference = _reference_attr(attrs)

    await _rate_limit(user)
    reads: list = []
    result = await vault_diagnostics.check_reference(
        reference,
        key=key or "check",
        may_list_keys=has_capability(caps, cap.RUN_PLAN),
        local_execution=getattr(ws, "execution_mode", "agent") == "local",
        reads=reads,
    )
    await _audit_reads(db, user, "workspaces", f"ws-{ws.id}", reads)
    return JSONResponse(content={"data": _check_json(result)})


@router.post("/varsets/{varset_id}/vault-reference-checks")
async def check_varset_reference(
    varset_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Check a Vault reference, or a variable-set variable's stored one.

    Admin only, because writing a variable-set variable is admin only.
    """
    try:
        vs_uuid = uuid.UUID(varset_id.removeprefix("varset-"))
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Variable set not found") from e
    vs = (
        await db.execute(select(VariableSet).where(VariableSet.id == vs_uuid))
    ).scalar_one_or_none()
    if vs is None:
        raise HTTPException(status_code=404, detail="Variable set not found")

    attrs = _attrs(body)
    key = str(attrs.get("key") or "")
    if attrs.get("variable-id"):
        vsv = (
            await db.execute(
                select(VariableSetVariable).where(
                    VariableSetVariable.id == _var_uuid(attrs["variable-id"]),
                    VariableSetVariable.variable_set_id == vs.id,
                )
            )
        ).scalar_one_or_none()
        if vsv is None:
            raise HTTPException(status_code=404, detail="Variable not found")
        reference = _stored_reference(vsv)
        key = key or vsv.key
    else:
        reference = _reference_attr(attrs)

    await _rate_limit(user)
    reads: list = []
    result = await vault_diagnostics.check_reference(reference, key=key or "check", reads=reads)
    await _audit_reads(db, user, "variable-sets", f"varset-{vs.id}", reads)
    return JSONResponse(content={"data": _check_json(result)})
