"""AI onboarding API (#824): discover existing, unmanaged cloud resources and
generate copy-pasteable ``resource`` + ``import {}`` blocks (optionally opening
an MR).

Onboarding itself has **no feature flag** — it's gated per workspace by the
``workspace:onboard`` RBAC capability. The **AI mode** (natural-language,
conversational, config-cleanup) is the only optional part, keyed on its own
independent switch ``ai_onboarding.enabled`` — never on ``ai_summary``.

Endpoints (under /api/terrapod/v1):
    GET  /onboarding                                availability probe (any authed user)
    POST /workspaces/{id}/onboarding-sessions       start a discovery session (workspace:onboard)
    GET  /workspaces/{id}/onboarding-sessions       list a workspace's sessions (workspace:onboard)
    GET  /onboarding-sessions/{id}                  one session incl. discovery surface (workspace:onboard)

Creating a session kicks off the credential-less **D1 schema** discovery on a
scheduler trigger (off the request thread); the client polls the session until
``status == "schema_ready"``, then reads the discovery surface. The surface is
served from a time-limited Redis cache (never persisted per-session); a session
whose cache entry has expired simply re-runs discovery. D2/D3 (query + generate)
run on the runner in a later phase.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.pagination import paginate
from terrapod.api.serialization import rfc3339
from terrapod.auth.capabilities import WORKSPACE_ONBOARD, has_capability
from terrapod.config import settings
from terrapod.db.models import OnboardingSession, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import onboarding_polish, onboarding_service
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["onboarding"])
logger = get_logger(__name__)


@router.get("/onboarding")
async def onboarding_availability(
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Report onboarding availability to the UI.

    The onboarding feature is always present (no feature flag) — whether a given
    user can actually run it is decided per-workspace by the ``workspace:onboard``
    capability on the real endpoints. This probe reports whether the optional
    **AI mode** is available (its own switch + a configured model), so the UI can
    offer the conversational path when it's set up.
    """
    cfg = settings.ai_onboarding
    return {
        "data": {
            "type": "onboarding-availability",
            "attributes": {
                "ai-available": cfg.enabled,
                "ai-model-configured": bool(cfg.enabled and cfg.model),
            },
        }
    }


# --- helpers ---------------------------------------------------------------
async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_onboard(ws: Workspace, user: AuthenticatedUser, db: AsyncSession) -> None:
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, WORKSPACE_ONBOARD):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{WORKSPACE_ONBOARD}' capability on workspace",
        )


def _session_json(
    s: OnboardingSession, surface: dict | None = None, *, detail: bool = False
) -> dict:
    """Serialize a session. ``surface`` (from the Redis cache) and the derived
    ``paired-*`` views are included only on the **detail** read — the surface is
    never stored on the row, and the paired views are a regex reflow over the full
    config, so computing them per-row in the list handler (an ``async def``) would
    be sync work on the event loop (Rule 13). The list omits both."""
    paired_config = (
        onboarding_polish.pair_config_and_imports(s.generated_config, s.import_blocks or "")
        if detail and s.generated_config
        else None
    )
    paired_polished = (
        onboarding_polish.pair_config_and_imports(s.polished_config, s.polished_import_blocks or "")
        if detail and s.polished_config
        else None
    )
    return {
        "type": "onboarding-sessions",
        "id": str(s.id),
        "attributes": {
            "workspace-id": str(s.workspace_id),
            "status": s.status,
            "provider": s.provider,
            "provider-version": s.provider_version,
            "engine": s.engine,
            "engine-version": s.engine_version,
            "selected-types": s.selected_types or [],
            "ai-assisted": s.ai_assisted,
            "error": s.error,
            "data-source-count": (surface or {}).get("count") if surface else None,
            "discovery-surface": surface,
            # D3 output — the reviewable payload. Present once status == config_ready.
            "generated-config": s.generated_config,
            "import-blocks": s.import_blocks,
            # Optional AI-polished view (#824 Phase A) — resources renamed from
            # tags, grouped, commented; import ids/values untouched. Null until
            # the polish lands (or if rejected / AI disabled). `ai-assisted`
            # (above) is true iff these are populated.
            "polished-config": s.polished_config,
            "polished-import-blocks": s.polished_import_blocks,
            # Derived, presentation-only "paired" view: each `import {}` interleaved
            # directly above the resource it targets (import ids/values untouched;
            # the split fields above stay canonical). Computed at serialize time —
            # never stored. Null until config exists.
            "paired-config": paired_config,
            "paired-polished-config": paired_polished,
            "discovery-run-id": str(s.discovery_run_id) if s.discovery_run_id else None,
            "result-run-id": str(s.result_run_id) if s.result_run_id else None,
            "created-by": s.created_by,
            "created-at": rfc3339(s.created_at),
            "updated-at": rfc3339(s.updated_at),
        },
    }


# --- endpoints -------------------------------------------------------------
@router.post("/workspaces/{workspace_id}/onboarding-sessions", status_code=201)
async def create_onboarding_session(
    workspace_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Start a discovery session and kick off credential-less D1 schema discovery."""
    ws = await _get_workspace(workspace_id, db)
    await _require_onboard(ws, user, db)

    attrs = (body.get("data") or {}).get("attributes") or {}
    provider = str(attrs.get("provider") or "").strip()
    if not onboarding_service.is_valid_provider(provider):
        raise HTTPException(
            status_code=422,
            detail="provider must be a lowercase terraform provider name (e.g. 'aws')",
        )
    provider_version = str(
        attrs.get("provider-version") or attrs.get("provider_version") or ""
    ).strip()
    if not onboarding_service.is_valid_version_constraint(provider_version):
        raise HTTPException(
            status_code=422,
            detail="provider-version must be a terraform version constraint (e.g. '< 6.0', '~> 5.0')",
        )

    session = await onboarding_service.create_session(
        db,
        workspace_id=ws.id,
        provider=provider,
        created_by=user.email,
        provider_version=provider_version,
    )
    await db.commit()

    # Run the (potentially slow, first-time) schema discovery off the request
    # thread. Best-effort enqueue — the session is already persisted as pending.
    try:
        from terrapod.services.scheduler import enqueue_trigger

        await enqueue_trigger(
            "onboarding_schema_discover",
            {"session_id": str(session.id)},
            dedup_key=f"onb_schema:{session.id}",
        )
    except Exception as exc:  # noqa: BLE001 — surface stays pending, retriable
        logger.warning(
            "onboarding_schema_enqueue_failed", session_id=str(session.id), error=str(exc)
        )

    return {"data": _session_json(session)}


@router.get("/workspaces/{workspace_id}/onboarding-sessions")
async def list_onboarding_sessions(
    workspace_id: str,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    ws = await _get_workspace(workspace_id, db)
    await _require_onboard(ws, user, db)
    sessions = await onboarding_service.list_sessions(db, ws.id)
    # Surfaces are omitted from the list (each is a large per-session Redis read);
    # the detail endpoint carries the surface.
    items = [_session_json(s) for s in sessions]
    page_items, meta = paginate(items, request)
    return {"data": page_items, "meta": meta}


@router.get("/onboarding-sessions/{session_id}")
async def get_onboarding_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    session = await onboarding_service.get_session(db, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    ws = await db.get(Workspace, session.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _require_onboard(ws, user, db)
    surface = await onboarding_service.get_session_surface(session)
    return {"data": _session_json(session, surface=surface, detail=True)}


@router.post("/onboarding-sessions/{session_id}/discover")
async def start_onboarding_discovery(
    session_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Dispatch the D2/D3 runner discovery run for a schema_ready session.

    Body: ``{"data": {"attributes": {"selected-types": ["aws_vpcs", ...]}}}`` —
    the data-source types (a subset of the session's discovery surface) to query.
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    session = await onboarding_service.get_session(db, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    ws = await db.get(Workspace, session.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _require_onboard(ws, user, db)

    attrs = (body.get("data") or {}).get("attributes") or {}
    raw_types = attrs.get("selected-types") or attrs.get("selected_types") or []
    if not isinstance(raw_types, list) or not all(isinstance(t, str) for t in raw_types):
        raise HTTPException(status_code=422, detail="selected-types must be a list of strings")

    try:
        session = await onboarding_service.start_discovery(db, session, raw_types)
    except onboarding_service.OnboardingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()

    surface = await onboarding_service.get_session_surface(session)
    return {"data": _session_json(session, surface=surface, detail=True)}
