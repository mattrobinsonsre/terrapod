"""Polling one workspace must not close another's PR sessions (#2349).

Integration tier, and necessarily so. The defect is about which ROWS
`_reconcile_closed_pr_sessions` closes and which runs it cancels, and every
services-tier test of that sweep hands it sessions through a mocked
`db.execute` -- so those tests assert what the loop does with rows it is given
and can say nothing about the interaction between two workspaces' rows, which
is the whole of this bug.

The reported shape, reproduced here exactly: two workspaces on one VCS
connection and one repository, tracking different base branches. A PR into the
branch workspace B tracks has an open session and an apply-capable planned run.
Workspace A polls. Before the fix, A's branch-scoped open-PR list did not
contain B's PR, so the set difference closed B's session and force-cancelled
its run -- and the next `terrapod ...` comment on a PR that was still open got
"not tracking this pull request".

The pair of tests is the point. A single fixture cannot distinguish "absent
because it targets another branch" from "absent because it closed", and those
must resolve differently -- so both states are instantiated.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import github_service, run_service
from terrapod.services.vcs_poller import _poll_workspace_prs
from terrapod.services.vcs_provider import OpenPullRequests, PullRequest

PR_ON_MAIN = 1
PR_ON_DEVELOP = 2


async def _seed():
    """One connection, two workspaces on one repo, one session+run on each PR."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        conn = VCSConnection(name=f"brscope-{tag}", provider="github", server_url="")
        db.add(conn)
        await db.flush()

        made = {}
        for label, branch, pr in (
            ("a", "main", PR_ON_MAIN),
            ("b", "develop", PR_ON_DEVELOP),
        ):
            ws = Workspace(
                name=f"brscope-{label}-{tag}",
                vcs_connection_id=conn.id,
                vcs_repo_url="https://github.com/org/repo",
                vcs_branch=branch,
            )
            db.add(ws)
            await db.flush()
            # Apply-capable and finished: the run the sweep is right to cancel
            # when the PR really closes, and must not touch otherwise. It also
            # holds the workspace, which is why cancelling it wrongly strands
            # every later PR on that workspace.
            run = Run(
                workspace_id=ws.id,
                created_by="tests",
                vcs_pull_request_number=pr,
                status="planned",
                plan_only=False,
            )
            db.add(run)
            sess = PRSession(vcs_connection_id=conn.id, repo="org/repo", pr_number=pr, state="open")
            db.add(sess)
            await db.flush()
            made[label] = {"ws": ws.id, "run": run.id, "sess": sess.id, "branch": branch}
        await db.commit()
        return conn.id, made


def _pr(number, base_ref):
    return PullRequest(
        number=number,
        head_sha=f"{number:040d}",
        head_ref=f"feature/{number}",
        title=f"pr {number}",
        from_fork=False,
        base_ref=base_ref,
    )


async def _poll(conn_id, ws_id, branch, *, open_on_repo):
    """Poll one workspace, with only the provider mocked."""
    async with get_db_session() as db:
        conn = await db.get(VCSConnection, conn_id)
        ws = await db.get(Workspace, ws_id)
        with (
            patch.object(
                github_service,
                "list_open_pull_requests_for_repo",
                new=AsyncMock(return_value=OpenPullRequests(list(open_on_repo), complete=True)),
            ),
            # Reached only for a PR that targets the polled branch; this test is
            # about reconciliation, so run creation is stubbed out.
            patch(
                "terrapod.services.vcs_poller._create_vcs_run",
                new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ),
            patch("terrapod.services.vcs_poller._upsert_pr_session", new=AsyncMock()),
            patch("terrapod.services.vcs_poller.enqueue_trigger", new=AsyncMock()),
            patch("terrapod.services.vcs_poller._poll_pr_comments", new=AsyncMock()),
        ):
            await _poll_workspace_prs(db, ws, conn, "org", "repo", branch, meta=None)
        await db.commit()


async def _state(ids):
    async with get_db_session() as db:
        sess = await db.get(PRSession, ids["sess"])
        run = await db.get(Run, ids["run"])
        assert sess is not None and run is not None
        return sess.state, run.status, run.discard_reason


class TestASiblingWorkspacesSessionSurvives:
    async def test_polling_main_leaves_the_develop_prs_session_and_run_alone(self):
        """The defect, end to end against the database."""
        conn_id, made = await _seed()
        await _poll(
            conn_id,
            made["a"]["ws"],
            "main",
            open_on_repo=[_pr(PR_ON_MAIN, "main"), _pr(PR_ON_DEVELOP, "develop")],
        )

        state, status, reason = await _state(made["b"])
        assert state == "open", "the sibling workspace's session was closed"
        assert status == "planned", "the sibling workspace's planned run was cancelled"
        assert reason is None

    async def test_the_polled_workspaces_own_session_is_also_untouched(self):
        conn_id, made = await _seed()
        await _poll(
            conn_id,
            made["a"]["ws"],
            "main",
            open_on_repo=[_pr(PR_ON_MAIN, "main"), _pr(PR_ON_DEVELOP, "develop")],
        )
        assert (await _state(made["a"]))[0] == "open"

    async def test_a_tracked_branch_with_no_prs_of_its_own_closes_nothing(self):
        """The amplified case from the report: workspace A tracks a branch that
        has no open PRs -- or does not exist, which produces the same empty
        branch-scoped list -- and used to close every session on the repo on
        every cycle."""
        conn_id, made = await _seed()
        await _poll(
            conn_id,
            made["a"]["ws"],
            "no-such-branch",
            open_on_repo=[_pr(PR_ON_MAIN, "main"), _pr(PR_ON_DEVELOP, "develop")],
        )
        assert (await _state(made["a"]))[0] == "open"
        assert (await _state(made["b"]))[0] == "open"


class TestAGenuinelyClosedPRIsStillReconciled:
    """The control. The fix must not amount to switching reconciliation off."""

    async def test_a_pr_the_provider_no_longer_lists_is_closed_and_its_run_cancelled(self):
        conn_id, made = await _seed()
        # PR 2 has closed; PR 1 is still open.
        await _poll(conn_id, made["a"]["ws"], "main", open_on_repo=[_pr(PR_ON_MAIN, "main")])

        state, status, reason = await _state(made["b"])
        assert state == "closed"
        assert status == "canceled", "an apply-capable planned run must not keep holding the lock"
        assert reason == run_service.pr_closed_cancel_reason(PR_ON_DEVELOP)
