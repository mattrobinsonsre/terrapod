"""AI onboarding API (#824): discover existing, unmanaged cloud resources and
generate copy-pasteable ``resource`` + ``import {}`` blocks.

The whole router is gated on ``ai_onboarding.enabled`` — disabled returns 404,
so the web UI hides the "Onboard existing resources" action on any non-200.

This is P1 (foundation): the config gate + an availability probe. The session
lifecycle (create → runner discovery Job → generated result) lands in P2.

Endpoints (under /api/terrapod/v1):
    GET /onboarding   availability probe (any authenticated user)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.logging_config import get_logger

router = APIRouter(tags=["onboarding"])
logger = get_logger(__name__)


async def require_onboarding_enabled() -> None:
    """Gate the whole router — 404 when AI onboarding is off.

    Onboarding is an independent AI feature: it self-gates on its OWN switch
    (``ai_onboarding.enabled``), with no dependency on ``ai_summary``.
    """
    if not settings.ai_onboarding.enabled:
        raise HTTPException(status_code=404, detail="AI onboarding is not enabled")


@router.get("/onboarding")
async def onboarding_availability(
    _: None = Depends(require_onboarding_enabled),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Report onboarding availability to the UI.

    404 when the feature is off (the UI hides the action). When on, reports
    whether a model is actually configured so the UI can distinguish
    "enabled but not yet configured" from "ready".
    """
    cfg = settings.ai_onboarding
    return {
        "data": {
            "type": "onboarding-availability",
            "attributes": {
                "enabled": True,
                "model-configured": bool(cfg.model),
            },
        }
    }
