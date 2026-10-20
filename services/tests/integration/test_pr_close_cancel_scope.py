"""Which runs a closed pull request takes with it (#2173).

Integration tier, and necessarily so -- the same reason as
`test_run_artifact_reap_eligibility.py`. The whole of this change is a **SQL
WHERE clause**, and every services-tier test of these sweeps hands the function
its rows through a mocked `db.execute`, so those tests assert what the loop does
with rows it is given and can say nothing about which rows it asks for.

What the predicate has to get right, in both directions:

  * a FINISHED SPECULATIVE plan must survive. `planned` is not terminal, so the
    old `notin_(TERMINAL_STATES)` cancelled it the moment the PR merged -- the
    plan a reviewer was reading, and in the module-impact case the blast-radius
    analysis somebody merged the PR in order to act on. It is also where a
    plan-only run stays for ever: nothing supersedes it and plan expiry is off
    by default.
  * a FINISHED APPLY-CAPABLE plan must still be cancelled, and this is the
    direction that would do damage if the fix were written as "leave finished
    plans alone". `claim_next_run` blocks a workspace on any other
    `plan_only=False` run in `planning/planned/confirmed/applying/canceling`,
    and this sweep is the only thing that clears one abandoned by a closed PR --
    so leaving it holds the workspace against every other pull request for ever.
  * a run still EXECUTING is cancelled whatever its `plan_only`: that really is
    work nobody is waiting for once the PR is gone.
  * a TERMINAL run is never touched.
"""

import uuid

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service
from terrapod.services.module_impact_service import _cancel_stale_module_runs
from terrapod.services.vcs_poller import _reconcile_closed_pr_sessions

PR = 7


async def _seed(*runs: dict) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """One workspace plus N runs, built through the ORM.

    Not a hand-written INSERT: `runs` carries ~70 columns, so a literal one
    breaks on the next NOT NULL anyone adds, with a driver error naming a column
    that has nothing to do with the test.
    """
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        ws = Workspace(name=f"prclose-{tag}")
        db.add(ws)
        await db.flush()
        ids: dict[str, uuid.UUID] = {}
        for fields in runs:
            label = fields.pop("label")
            run = Run(
                workspace_id=ws.id,
                created_by="tests",
                vcs_pull_request_number=PR,
                **fields,
            )
            db.add(run)
            await db.flush()
            ids[label] = run.id
        return ws.id, ids


THE_FOUR_CASES = (
    {"label": "finished-speculative", "status": "planned", "plan_only": True},
    {"label": "finished-apply-capable", "status": "planned", "plan_only": False},
    {"label": "still-planning", "status": "planning", "plan_only": True},
    {"label": "already-applied", "status": "applied", "plan_only": False},
)


async def _selected(ws_id) -> set[str]:
    """The labels the shared predicate selects, read back from the database."""
    from sqlalchemy import select

    async with get_db_session() as db:
        rows = await db.execute(
            select(Run.id).where(
                Run.workspace_id == ws_id,
                Run.vcs_pull_request_number == PR,
                run_service.pr_closure_cancels_sql(Run),
            )
        )
        return {str(r) for r in rows.scalars().all()}


class TestThePredicateSelectsTheRightRows:
    async def test_the_sql_twin_agrees_with_the_python_one_on_every_case(self):
        """One rule, two implementations -- so prove they answer alike.

        This is the assertion a mocked session cannot make, and the reason this
        file is in the integration tier rather than beside the truth table.
        """
        ws_id, ids = await _seed(*[dict(c) for c in THE_FOUR_CASES])
        chosen = await _selected(ws_id)

        async with get_db_session() as db:
            for case in THE_FOUR_CASES:
                run = await db.get(Run, ids[case["label"]])
                assert run is not None
                python_says = run_service.pr_closure_cancels(run)
                sql_says = str(run.id) in chosen
                assert python_says == sql_says, (
                    f"{case['label']}: python={python_says} sql={sql_says}"
                )

    async def test_a_finished_speculative_plan_is_not_selected(self):
        ws_id, ids = await _seed(*[dict(c) for c in THE_FOUR_CASES])
        chosen = await _selected(ws_id)
        assert str(ids["finished-speculative"]) not in chosen

    async def test_a_finished_apply_capable_plan_IS_selected(self):
        """Or it holds the workspace against every other PR for ever."""
        ws_id, ids = await _seed(*[dict(c) for c in THE_FOUR_CASES])
        chosen = await _selected(ws_id)
        assert str(ids["finished-apply-capable"]) in chosen

    async def test_a_running_plan_is_selected_and_a_terminal_one_is_not(self):
        ws_id, ids = await _seed(*[dict(c) for c in THE_FOUR_CASES])
        chosen = await _selected(ws_id)
        assert str(ids["still-planning"]) in chosen
        assert str(ids["already-applied"]) not in chosen


class TestTheSweepsThemselvesLeaveTheFinishedPlanAlone:
    """End to end through the real sweeps, against the real database."""

    async def test_the_module_sweep_leaves_a_finished_impact_plan(self):
        """Every run this sweep touches is plan-only, so before the fix it
        cancelled the finished analysis on every consumer of a merged module."""
        from unittest.mock import MagicMock

        ws_id, ids = await _seed(
            {
                "label": "finished",
                "status": "planned",
                "plan_only": True,
                "source": "module-test",
            },
            {
                "label": "running",
                "status": "planning",
                "plan_only": True,
                "source": "module-test",
            },
        )
        module = MagicMock(name="m", workspace_links=[MagicMock(workspace_id=ws_id)])

        async with get_db_session() as db:
            await _cancel_stale_module_runs(db, module, PR)
            await db.commit()

        async with get_db_session() as db:
            finished = await db.get(Run, ids["finished"])
            running = await db.get(Run, ids["running"])
            assert finished is not None and running is not None
            assert finished.status == "planned", "the finished analysis was cancelled"
            assert running.status == "canceled", "the running one should have gone"
            assert running.discard_reason == run_service.pr_closed_cancel_reason(PR)

    async def test_the_workspace_sweep_closes_the_session_either_way(self):
        """The reconciliation must still do its other job."""
        from terrapod.db.models import PRSession, VCSConnection

        async with get_db_session() as db:
            conn = VCSConnection(name=f"c-{uuid.uuid4().hex[:8]}", provider="github", server_url="")
            db.add(conn)
            await db.flush()
            sess = PRSession(vcs_connection_id=conn.id, repo="org/repo", pr_number=PR, state="open")
            db.add(sess)
            await db.commit()
            conn_id, sess_id = conn.id, sess.id

        async with get_db_session() as db:
            conn = await db.get(VCSConnection, conn_id)
            await _reconcile_closed_pr_sessions(db, conn, "org/repo", set())
            await db.commit()

        async with get_db_session() as db:
            sess = await db.get(PRSession, sess_id)
            assert sess is not None and sess.state == "closed"
