"""AI onboarding API (#824): discover existing, unmanaged cloud resources and
generate copy-pasteable ``resource`` + ``import {}`` blocks (optionally opening
an MR).

Onboarding itself has **no feature flag** — it's gated by the per-workspace
``workspace:onboard`` RBAC capability on the real endpoints (added with the
discovery run type in a later phase). The **AI mode** (natural-language,
conversational, config-cleanup) is the only optional part, keyed on its own
independent switch ``ai_onboarding.enabled`` — never on ``ai_summary``.

This is P1 (foundation): the config + capability + an availability probe. The
session lifecycle (discovery run → generated result → copy-paste/MR) lands next.

Endpoints (under /api/terrapod/v1):
    GET /onboarding   availability probe (any authenticated user)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.logging_config import get_logger

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
