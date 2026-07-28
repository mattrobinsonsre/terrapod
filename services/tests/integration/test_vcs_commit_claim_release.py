"""Integration test (real Postgres) for releasing a VCS commit claim (#1099).

`_poll_workspace_branch` advances `vcs_last_commit_sha` before fetching the
archive and creating the run, so a concurrent poll cannot duplicate the run
(#217). If the run creation then fails, the claim has to be released or the
commit is skipped permanently.

The guards on that release — a compare-and-set on the claimed sha, and a check
for an existing run — are SQL predicates, so a mocked-DB test would only assert
that we built the statement we built. These run against a real engine.
"""

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service
from terrapod.services.vcs_poller import _release_commit_claim
from tests.integration.conftest import AUTH, admin_user, set_auth

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"

pytestmark = pytest.mark.asyncio

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
NEWER_SHA = "c" * 40


async def _seed_workspace(client, name: str, *, claimed: str = NEW_SHA) -> uuid.UUID:
    """Create a workspace already holding a claim on `claimed`."""
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = uuid.UUID(resp.json()["data"]["id"].removeprefix("ws-"))

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        ws.vcs_last_commit_sha = claimed
        await db.commit()
    return ws_id


async def _cursor(ws_id: uuid.UUID) -> str | None:
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        return ws.vcs_last_commit_sha


async def _seed_run(ws_id: uuid.UUID, *, sha: str, pr_number: int | None) -> None:
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        run = await run_service.create_run(db, ws, message="seeded", source="vcs")
        run.vcs_commit_sha = sha
        run.vcs_pull_request_number = pr_number
        await db.commit()


class TestReleasesTheClaim:
    """The point of the fix: a failed run creation must not consume the commit."""

    async def test_cursor_rolls_back_when_no_run_was_created(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-basic")

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        assert await _cursor(ws_id) == OLD_SHA, (
            "the next poll must see the commit again, or it is skipped forever"
        )

    async def test_first_ever_poll_rolls_back_to_empty(self, app, client):
        """The column is non-nullable: "never polled" is "", not NULL."""
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-first-poll")

        await _release_commit_claim(ws_id, NEW_SHA, "")

        assert await _cursor(ws_id) == ""

    async def test_a_speculative_pr_run_does_not_count_as_handled(self, app, client):
        """A PR run can share the head sha; only a branch run means it was handled."""
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-pr-run")
        await _seed_run(ws_id, sha=NEW_SHA, pr_number=7)

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        assert await _cursor(ws_id) == OLD_SHA


class TestGuards:
    """Releasing must never be able to do harm — these are the two guards."""

    async def test_does_not_release_when_the_run_already_exists(self, app, client):
        """A failure *after* the run was created must not re-run the commit."""
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-run-exists")
        await _seed_run(ws_id, sha=NEW_SHA, pr_number=None)

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        assert await _cursor(ws_id) == NEW_SHA, "would have produced a duplicate run"

    async def test_never_rewinds_past_a_newer_claim(self, app, client):
        """A concurrent poll may have claimed a later commit while we were failing."""
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-cas", claimed=NEWER_SHA)

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        assert await _cursor(ws_id) == NEWER_SHA, "rewound over a newer poll's claim"

    async def test_another_workspaces_run_does_not_suppress_the_release(self, app, client):
        """The existing-run check must be scoped to this workspace."""
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-scoped")
        other_id = await _seed_workspace(client, "claim-release-scoped-other")
        await _seed_run(other_id, sha=NEW_SHA, pr_number=None)

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        assert await _cursor(ws_id) == OLD_SHA


class TestBestEffort:
    """It runs on the failure path, so it must never raise on top of the failure."""

    async def test_unknown_workspace_is_swallowed(self, app):
        set_auth(app, admin_user())

        await _release_commit_claim(uuid.uuid4(), NEW_SHA, OLD_SHA)

    async def test_does_not_touch_other_rows(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _seed_workspace(client, "claim-release-isolated")
        bystander = await _seed_workspace(client, "claim-release-bystander")

        await _release_commit_claim(ws_id, NEW_SHA, OLD_SHA)

        async with get_db_session() as db:
            rows = (
                (await db.execute(select(Run).where(Run.workspace_id == bystander))).scalars().all()
            )
        assert rows == []
        assert await _cursor(bystander) == NEW_SHA
