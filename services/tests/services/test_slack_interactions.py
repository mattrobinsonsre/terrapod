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
    *,
    db,
    link,
    roles=None,
    caps=frozenset(),
    confirm=None,
    discard=None,
    nudge=None,
    update=None,
    audit=None,
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
        patch("terrapod.services.audit_service.log_audit_event", audit or AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_unlinked_click_nudges_and_never_mutates():
    """No binding → ephemeral nudge to /terrapod link, run untouched."""
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        is_destroy=False,
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
        is_destroy=False,
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="dev@example.com", identity_provider="okta")
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
        is_destroy=False,
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta")
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
        is_destroy=False,
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta")
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
        is_destroy=False,
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    ws = SimpleNamespace(id="ws-1", name="prod")
    db = _db_with(run, ws)
    link = SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta")
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


# ── the audit trail for a decision taken from Slack ───────────────────


def _run_and_ws():
    run = SimpleNamespace(
        id="run-1",
        workspace_id="ws-1",
        is_destroy=False,
        resource_additions=1,
        resource_changes=0,
        resource_destructions=0,
    )
    return run, SimpleNamespace(id="ws-1", name="prod")


async def _click(action_id: str, *, link, caps, audit, db=None, **extra):
    """Drive one button click and return the patched audit mock."""
    run, ws = _run_and_ws()
    db = db if db is not None else _db_with(run, ws)
    ps = _patches(db=db, link=link, caps=caps, audit=audit, **extra)
    for p in ps:
        p.start()
    try:
        await si.handle_block_actions(_payload(action_id))
    finally:
        for p in reversed(ps):
            p.stop()
    return db


class TestASlackDrivenDecisionIsAudited:
    """Socket Mode carries no HTTP request, so the audit middleware structurally
    cannot see any of this.

    Without an explicit write, a production apply approved from a Slack button
    leaves exactly the same trail as one nobody ever touched — and the Slack
    path is a full `run:apply`, re-derived live but no less privileged than the
    API's. Everything the row needs is already resolved where the decision is
    taken.
    """

    @pytest.mark.asyncio
    async def test_an_approval_writes_one_row_naming_the_run_and_the_actor(self):
        audit = AsyncMock()
        await _click(
            ACTION_APPROVE,
            link=SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta"),
            caps=frozenset({RUN_APPLY}),
            audit=audit,
        )
        audit.assert_awaited_once()
        kw = audit.await_args.kwargs
        assert kw["action"] == "run.confirm"
        assert kw["resource_type"] == "run"
        assert kw["resource_id"] == "run-1"
        assert kw["status_code"] == 200
        assert kw["actor_email"] == "lead@example.com"
        # The surface is what distinguishes this from an API confirm.
        assert kw["origin"] == "slack"
        # The Slack account that clicked, so the trail survives an unlinking.
        assert kw["actor_id"] == "U1"

    @pytest.mark.asyncio
    async def test_a_discard_is_audited_as_a_discard(self):
        audit = AsyncMock()
        await _click(
            ACTION_DISCARD,
            link=SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta"),
            caps=frozenset({RUN_APPLY}),
            audit=audit,
        )
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["action"] == "run.discard"
        assert audit.await_args.kwargs["status_code"] == 200

    @pytest.mark.asyncio
    async def test_a_refused_click_is_audited_as_a_403(self):
        """The negative path. A refusal that leaves no trace is the half of the
        trail an auditor most needs: it is the record of someone reaching for a
        production apply they did not have.
        """
        audit = AsyncMock()
        await _click(
            ACTION_APPROVE,
            link=SimpleNamespace(terrapod_email="dev@example.com", identity_provider="okta"),
            caps=frozenset({"run:read"}),
            audit=audit,
        )
        audit.assert_awaited_once()
        kw = audit.await_args.kwargs
        assert kw["status_code"] == 403
        assert kw["action"] == "run.confirm"
        assert kw["actor_email"] == "dev@example.com"
        assert kw["origin"] == "slack"

    @pytest.mark.asyncio
    async def test_an_unlinked_click_is_audited_as_a_401(self):
        audit = AsyncMock()
        await _click(
            ACTION_APPROVE,
            link=None,
            caps=frozenset({RUN_APPLY}),
            audit=audit,
        )
        audit.assert_awaited_once()
        kw = audit.await_args.kwargs
        assert kw["status_code"] == 401
        assert kw["resource_id"] == "run-1"
        # No Terrapod identity to attribute it to — but the Slack one is recorded,
        # and calling them a `terrapod_user` would misattribute the refusal.
        assert kw["actor_email"] == ""
        assert kw["actor_type"] == "slack_user"
        assert kw["actor_id"] == "U1"

    @pytest.mark.asyncio
    async def test_the_row_is_written_after_the_mutation_commits(self):
        """`log_audit_event` commits internally, so the order matters: a row
        written first would outlive a decision that then failed to land.
        """
        order: list[str] = []
        run, ws = _run_and_ws()
        db = _db_with(run, ws)
        db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
        audit = AsyncMock(side_effect=lambda *a, **k: order.append("audit"))
        await _click(
            ACTION_APPROVE,
            link=SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta"),
            caps=frozenset({RUN_APPLY}),
            audit=audit,
            db=db,
        )
        assert order == ["commit", "audit"], order

    @pytest.mark.asyncio
    async def test_a_stale_button_is_not_audited_as_a_decision(self):
        """Nothing happened, so nothing is recorded as having happened."""
        audit = AsyncMock()
        await _click(
            ACTION_APPROVE,
            link=SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta"),
            caps=frozenset({RUN_APPLY}),
            audit=audit,
            confirm=AsyncMock(side_effect=ValueError("Can only confirm runs in 'planned' status")),
        )
        audit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_audit_write_does_not_strand_the_slack_message(self):
        """The apply is already committed and cannot be unwound, so the write is
        best-effort: it is logged, not raised.
        """
        update = AsyncMock()
        await _click(
            ACTION_APPROVE,
            link=SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta"),
            caps=frozenset({RUN_APPLY}),
            audit=AsyncMock(side_effect=RuntimeError("audit table is gone")),
            update=update,
        )
        update.assert_awaited_once()


class TestADestroyNeedsTheDestroyCapability:
    """Slack must apply the same capability rule as the API route.

    The route requires `run:apply-destroy` to confirm a destroy run; Slack checked
    `run:apply` for everything, so a role holding apply but not apply-destroy could
    confirm from Slack a destroy the API would have refused. Both now call
    `confirm_capability`.
    """

    @pytest.mark.asyncio
    async def test_apply_without_apply_destroy_cannot_confirm_a_destroy(self):
        run = SimpleNamespace(
            id="run-1",
            workspace_id="ws-1",
            is_destroy=True,
            resource_additions=0,
            resource_changes=0,
            resource_destructions=7,
        )
        ws = SimpleNamespace(id="ws-1", name="prod")
        db = _db_with(run, ws)
        link = SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta")
        confirm, nudge = AsyncMock(), AsyncMock()
        ps = _patches(db=db, link=link, caps=frozenset({RUN_APPLY}), confirm=confirm, nudge=nudge)
        for p in ps:
            p.start()
        try:
            await si.handle_block_actions(_payload(ACTION_APPROVE))
        finally:
            for p in reversed(ps):
                p.stop()
        confirm.assert_not_awaited()
        db.commit.assert_not_awaited()
        nudge.assert_awaited()

    @pytest.mark.asyncio
    async def test_apply_destroy_can_confirm_a_destroy(self):
        from terrapod.auth.capabilities import RUN_APPLY_DESTROY

        run = SimpleNamespace(
            id="run-1",
            workspace_id="ws-1",
            is_destroy=True,
            resource_additions=0,
            resource_changes=0,
            resource_destructions=7,
        )
        ws = SimpleNamespace(id="ws-1", name="prod")
        db = _db_with(run, ws)
        link = SimpleNamespace(terrapod_email="lead@example.com", identity_provider="okta")
        confirm, update = AsyncMock(), AsyncMock()
        ps = _patches(
            db=db,
            link=link,
            caps=frozenset({RUN_APPLY, RUN_APPLY_DESTROY}),
            confirm=confirm,
            update=update,
        )
        for p in ps:
            p.start()
        try:
            await si.handle_block_actions(_payload(ACTION_APPROVE))
        finally:
            for p in reversed(ps):
                p.stop()
        confirm.assert_awaited_once()


class TestTheSlackPathIsProviderScoped:
    """A Slack action resolves roles against the binding's IdP, not across all of them.

    GHSA-3m8x-ff8g-7x8c. The binding is a long-lived email -> Slack-user mapping, and
    resolving its roles by email alone gave every Slack action the union of what that
    address was assigned under any configured provider. Every other test here patches
    the resolver, so without this one the provider argument could be dropped from the
    call and nothing would fail.
    """

    async def test_the_links_provider_is_passed_to_role_resolution(self):
        resolver = AsyncMock(return_value=["everyone"])
        run = SimpleNamespace(
            id="run-1",
            workspace_id="ws-1",
            status="planned",
            is_destroy=False,
            configuration_version_id=None,
        )
        ws = SimpleNamespace(id="ws-1", name="prod")
        db = _db_with(run, ws)
        link = SimpleNamespace(terrapod_email="dev@example.com", identity_provider="okta")
        ps = _patches(db=db, link=link, caps=frozenset())
        ps = [p for p in ps if "_resolve_user_roles" not in str(p)]
        ps.append(patch("terrapod.api.dependencies._resolve_user_roles", resolver))
        for p in ps:
            p.start()
        try:
            await si.handle_block_actions(_payload(ACTION_APPROVE))
        finally:
            for p in reversed(ps):
                p.stop()

        resolver.assert_awaited_once()
        passed = resolver.await_args.args + tuple(resolver.await_args.kwargs.values())
        assert "okta" in passed, (
            "the binding's identity_provider is not reaching _resolve_user_roles, so "
            f"Slack actions resolve roles across every provider again: {passed!r}"
        )
