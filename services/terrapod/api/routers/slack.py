"""Slack account-linking API (#556).

Browser-driven surface consumed by the web `/slack/link` page: the user
authenticates to Terrapod normally, then POSTs the signed state, which binds
their Slack identity to their Terrapod identity. Also lists/removes a user's own
links. Any authenticated user links THEIR OWN identity — no admin needed; the
binding is attributed to the acting user.
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import SlackIdentityLink
from terrapod.db.session import get_db
from terrapod.services import slack_link_service

router = APIRouter(tags=["slack"])


def _link_json(link: SlackIdentityLink) -> dict:
    return {
        "id": f"slk-{link.id}",
        "slack-team-id": link.slack_team_id,
        "slack-user-id": link.slack_user_id,
        "email": link.terrapod_email,
        "linked-via": link.linked_via,
        "linked-at": link.linked_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.post("/slack/link/preview")
async def preview_link(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Describe (without consuming) the Slack identity a signed state would bind,
    so the browser can show a confirm screen before committing. This is the
    confused-deputy defence: binding is a deliberate act on a page that names the
    Slack user + team being linked to *your* Terrapod account, not an automatic
    bind on page load."""
    state = (body.get("state") or "").strip()
    if not state:
        raise HTTPException(status_code=422, detail="Missing link state")
    try:
        team_id, slack_user_id = await slack_link_service.peek_link_state(state)
    except slack_link_service.LinkStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        content={
            "data": {
                "slack-team-id": team_id,
                "slack-user-id": slack_user_id,
                "email": user.email,
            }
        }
    )


@router.post("/slack/link")
async def link_account(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Bind the current Terrapod user to the Slack identity in the signed state."""
    state = (body.get("state") or "").strip()
    if not state:
        raise HTTPException(status_code=422, detail="Missing link state")
    try:
        team_id, slack_user_id, response_url = await slack_link_service.verify_and_consume_state(
            state
        )
    except slack_link_service.LinkStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    link = await slack_link_service.create_link(
        db, team_id=team_id, user_id=slack_user_id, email=user.email
    )
    # Confirm back in the Slack conversation the /terrapod link came from, so the
    # user gets closure in Slack (not only in the browser). Best-effort.
    if response_url:
        await slack_link_service.post_response_url(
            response_url, f":white_check_mark: Linked to Terrapod as *{user.email}*."
        )
    return JSONResponse(content={"data": _link_json(link)})


@router.get("/slack/links")
async def list_my_links(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List the current user's Slack identity links."""
    rows = (
        (
            await db.execute(
                select(SlackIdentityLink).where(SlackIdentityLink.terrapod_email == user.email)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse(content={"data": [_link_json(r) for r in rows]})


@router.delete("/slack/links/{link_id}", status_code=204)
async def unlink_account(
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove one of the current user's own Slack links."""
    try:
        lid = uuid.UUID(link_id.removeprefix("slk-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Link not found") from exc
    link = await db.get(SlackIdentityLink, lid)
    if link is None or link.terrapod_email != user.email:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.delete(link)
    await db.commit()
