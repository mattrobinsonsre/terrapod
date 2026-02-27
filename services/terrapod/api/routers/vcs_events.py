"""VCS webhook event receiver (optional).

Only active when a webhook secret is configured. Validates HMAC signature
and triggers an immediate poll for the affected repo — no complex event
parsing. The poller does all the real work.

Endpoints:
    POST /api/v2/vcs-events/github   (GitHub webhook receiver)
"""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.services.github_service import validate_webhook_signature
from terrapod.services.vcs_poller import trigger_immediate_poll

router = APIRouter(prefix="/api/v2", tags=["vcs-events"])
logger = get_logger(__name__)


@router.post("/vcs-events/github")
async def github_webhook(request: Request) -> Response:
    """Receive GitHub webhook events.

    Validates HMAC-SHA256 signature when webhook_secret is configured.
    Triggers an immediate poll for the affected repository.
    """
    # Check if webhooks are configured
    if not settings.vcs.github.webhook_secret:
        raise HTTPException(
            status_code=404,
            detail="Webhooks not configured",
        )

    payload = await request.body()

    # Validate signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not validate_webhook_signature(payload, signature):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Handle ping event (GitHub sends this when webhook is first configured)
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        logger.info("GitHub webhook ping received")
        return JSONResponse(content={"message": "pong"})

    # For push and pull_request events, extract the repo and trigger a poll
    import json

    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    repo = body.get("repository", {})
    full_name = repo.get("full_name", "")

    if not full_name:
        logger.debug("Webhook event without repository", event=event_type)
        return JSONResponse(content={"message": "ignored"})

    if event_type in ("push", "pull_request"):
        logger.info(
            "Webhook event received",
            event=event_type,
            repo=full_name,
        )
        trigger_immediate_poll(full_name)

    return JSONResponse(content={"message": "accepted"})
