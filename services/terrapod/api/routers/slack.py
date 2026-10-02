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
from terrapod.services.audit_service import log_audit_event

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
    so the browser can show a confirm screen before committing.

    The confirm screen is the confused-deputy defence, and it only works if the
    reader can tell whose Slack account they are about to attach to their own. This
    endpoint used to return `slack-user-id` and `slack-team-id` and nothing else —
    `U04F2AB3C` in `T01XYZ` — which a victim cannot recognise as not theirs, so the
    screen asked a question nobody could answer and the "defence" was a click-through
    (`GHSA-5899-fm2p-88x3`). It now resolves the handle, real name and team name.

    `resolved` is returned explicitly so the page can say the lookup failed rather
    than silently falling back to ids that look like a deliberate choice; see
    `describe_slack_identity` for why that is best-effort."""
    state = (body.get("state") or "").strip()
    if not state:
        raise HTTPException(status_code=422, detail="Missing link state")
    try:
        team_id, slack_user_id = await slack_link_service.peek_link_state(state)
    except slack_link_service.LinkStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    named = await slack_link_service.describe_slack_identity(team_id, slack_user_id)
    return JSONResponse(
        content={
            "data": {
                "slack-team-id": team_id,
                "slack-user-id": slack_user_id,
                "email": user.email,
                **named,
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
    # A binding is a standing ability to act as this account from Slack, so it gets
    # a row naming who was bound to what (GHSA-5899-fm2p-88x3). There is no
    # per-user notification channel in Terrapod and inventing one for this would be
    # a bigger change than the finding warrants — the audit log is the trail, and it
    # is where an operator looks when asked "who attached that Slack account".
    named = await slack_link_service.describe_slack_identity(team_id, slack_user_id)
    await log_audit_event(
        db,
        actor_email=user.email,
        action="slack.link.create",
        resource_type="slack_identity_link",
        resource_id=f"slk-{link.id}",
        status_code=200,
        origin="terrapod_ui",
        detail=(
            f"bound Slack user {slack_user_id} "
            f"({named.get('user-name') or 'name unavailable'}) "
            f"in team {team_id} ({named.get('team-name') or 'name unavailable'})"
        ),
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
    slack_user_id, team_id = link.slack_user_id, link.slack_team_id
    await db.delete(link)
    await db.commit()
    # Revocation is audited too: "the link is gone" and "the link was never there"
    # are different answers to the same question, and only a row distinguishes them.
    await log_audit_event(
        db,
        actor_email=user.email,
        action="slack.link.revoke",
        resource_type="slack_identity_link",
        resource_id=link_id if link_id.startswith("slk-") else f"slk-{link_id}",
        status_code=204,
        origin="terrapod_ui",
        detail=f"revoked Slack user {slack_user_id} in team {team_id}",
    )
