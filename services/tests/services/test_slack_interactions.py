"""Tests for the Slack interactive run-approval RBAC spine (#556).

The security-critical invariant: a button click carries no standing permission —
every click re-derives authority live (binding → live roles → workspace
capabilities → run:apply) before it can confirm/discard. Unlinked and
unauthorised clicks must NOT mutate the run.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth.capabilities import RUN_APPLY
from terrapod.services import slack_interactions as si
from terrapod.services.slack_notify_service import ACTION_APPROVE, ACTION_DISCARD


class FakeCM:
    """Async-context-manager stand-in for get_db_session()."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


def _payload(action_id: str, run_id: str = "run-1"):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "response_url": "https://hooks.slack/resp",
        "channel": {"id": "C1"},
        "message": {"ts": "123.45"},
        "actions": [{"action_id": action_id, "value": run_id}],
    }


def _db_with(run, workspace):
    db = SimpleNamespace()
    db.get = AsyncMock(side_effect=[run, workspace])
    db.commit = AsyncMock()
    return db


def _patches(
    *, db, link, roles=None, caps=frozenset(), confirm=None, discard=None, nudge=None, update=None
):
    roles = roles if roles is not None else ["everyone"]
    return [
        patch("terrapod.db.session.get_db_session", return_value=FakeCM(db)),
        patch("terrapod.services.slack_link_service.get_link", AsyncMock(return_value=link)),
        patch("terrapod.api.dependencies._resolve_user_roles", AsyncMock(return_value=roles)),
        patch(
            "terrapod.services.workspace_rbac_service.resolve_workspace_capabilities_for",
            AsyncMock(return_value=caps),
        ),
        patch("terrapod.services.run_service.confirm_run", confirm or AsyncMock()),
        patch("terrapod.services.run_service.discard_run", discard or AsyncMock()),
        patch("terrapod.services.slack_link_service.post_response_url", nudge or AsyncMock()),
        patch("terrapod.services.slack_interactions._resolve_parent", update or AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_unlinked_click_nudges_and_never_mutates():
    """No binding → ephemeral nudge to /terrapod link, run untouched."""
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    confirm, discard, nudge = AsyncMock(), AsyncMock(), AsyncMock()
    ps = _patches(db=db, link=None, confirm=confirm, discard=discard, nudge=nudge)
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(ACTION_APPROVE))
    finally:
        for p in reversed(ps):
            p.stop()
    confirm.assert_not_awaited()
    discard.assert_not_awaited()
    nudge.assert_awaited_once()
    assert "/terrapod link" in nudge.await_args.args[1]
    # ephemeral, not replace_original (never clobber the shared message)
    assert nudge.await_args.kwargs.get("replace_original") is False


@pytest.mark.asyncio
async def test_linked_but_unauthorised_is_denied_without_mutation():
    """Binding exists but the live capability set lacks run:apply → denied."""
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="dev@example.com")
    confirm, nudge = AsyncMock(), AsyncMock()
    # caps without RUN_APPLY (e.g. read only)
    ps = _patches(db=db, link=link, caps=frozenset({"run:read"}), confirm=confirm, nudge=nudge)
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(ACTION_APPROVE))
    finally:
        for p in reversed(ps):
            p.stop()
    confirm.assert_not_awaited()
    nudge.assert_awaited_once()
    assert "permission" in nudge.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_authorised_approve_confirms_commits_and_updates_message():
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com")
    confirm, update = AsyncMock(), AsyncMock()
    ps = _patches(db=db, link=link, caps=frozenset({RUN_APPLY}), confirm=confirm, update=update)
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(ACTION_APPROVE))
    finally:
        for p in reversed(ps):
            p.stop()
    confirm.assert_awaited_once()
    db.commit.assert_awaited_once()
    update.assert_awaited_once()
    # the in-place edit records who approved
    assert "lead@example.com" in update.await_args.args[4]
    assert "Approved" in update.await_args.args[4]


@pytest.mark.asyncio
async def test_authorised_discard_calls_discard_run():
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com")
    confirm, discard, update = AsyncMock(), AsyncMock(), AsyncMock()
    ps = _patches(
        db=db,
        link=link,
        caps=frozenset({RUN_APPLY}),
        confirm=confirm,
        discard=discard,
        update=update,
    )
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(ACTION_DISCARD))
    finally:
        for p in reversed(ps):
            p.stop()
    discard.assert_awaited_once()
    confirm.assert_not_awaited()
    assert "Discarded" in update.await_args.args[4]


@pytest.mark.asyncio
async def test_stale_run_valueerror_is_surfaced_not_crashed():
    """A stale button (run already resolved) → ValueError → ephemeral, no 500."""
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com")
    confirm = AsyncMock(side_effect=ValueError("Can only confirm runs in 'planned' status"))
    nudge, update = AsyncMock(), AsyncMock()
    ps = _patches(
        db=db,
        link=link,
        caps=frozenset({RUN_APPLY}),
        confirm=confirm,
        nudge=nudge,
        update=update,
    )
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(ACTION_APPROVE))
    finally:
        for p in reversed(ps):
            p.stop()
    nudge.assert_awaited_once()
    update.assert_not_awaited()  # no message edit when the action didn't happen


@pytest.mark.asyncio
async def test_unknown_action_id_is_ignored():
    confirm = AsyncMock()
    with patch("terrapod.services.run_service.confirm_run", confirm):
        await si.handle_block_actions(_payload("some_other_button"))
    confirm.assert_not_awaited()


def test_authenticated_user_shape_is_constructible():
    # Guards the interaction handler's AuthenticatedUser(...) call against drift.
    u = AuthenticatedUser(
        email="a@b.c",
        display_name=None,
        roles=["everyone"],
        provider_name="slack",
        auth_method="session",
        kind="interactive",
    )
    assert u.email == "a@b.c"
