"""A run resolves its OWN PR session when two PRs share a number (#2199).

Integration tier, and necessarily so. The whole of this fix is a **SQL WHERE
clause**, and every services-tier test of these two handlers hands them their row
through a mocked `db.execute` -- so those tests assert what the handler does with
the session it is given and can say nothing about which session it asks for.

The property is also a RELATIONSHIP between two rows, not a fact about one, which
is the shape that a single-row fixture passes whatever the predicate is. That is
the `uq_vcs_connections_install` lesson again: nothing in the tree had ever
created two `PRSession`s -- in fact nothing in the tree constructed a `PRSession`
at all -- which is why eighteen months of tests never saw this.

`uq_pr_session` is `(vcs_connection_id, repo, pr_number)`, and a VCS connection is
one app installation covering a whole organisation. So two open pull requests
numbered alike in two repositories of one organisation are a perfectly legal pair
of rows, and a lookup on `(connection, pr_number)` alone matches both.

Both directions are asserted, deliberately. A test that only checks one would
pass on the broken code half the time, because `.first()` without an `ORDER BY`
returns whichever row the planner hands over.
"""

import uuid

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import vcs_auto_merge, vcs_status_comment

PR = 111


async def _seed() -> dict:
    """One connection, two repos, two same-numbered open PRs, a run in each."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        conn = VCSConnection(name=f"conn-{tag}", provider="github")
        db.add(conn)
        await db.flush()

        out: dict = {"conn_id": conn.id}
        for n in ("one", "two"):
            repo = f"acme-{tag}/repo-{n}"
            ws = Workspace(
                name=f"prscope-{tag}-{n}",
                vcs_connection_id=conn.id,
                vcs_repo_url=f"https://github.com/{repo}",
            )
            db.add(ws)
            await db.flush()

            sess = PRSession(
                vcs_connection_id=conn.id,
                repo=repo,
                pr_number=PR,
                head_sha="a" * 40,
                state="open",
            )
            db.add(sess)
            await db.flush()

            run = Run(
                workspace_id=ws.id,
                created_by="tests",
                status="planned",
                plan_only=True,
                vcs_pull_request_number=PR,
            )
            db.add(run)
            await db.flush()

            out[n] = {"ws_id": ws.id, "sess_id": sess.id, "run_id": run.id, "repo": repo}
        await db.commit()
        return out


class TestTwoPRsSharingANumberOnOneConnection:
    async def test_the_fixture_is_the_pair_the_bug_needs(self):
        """Guard the premise: two DISTINCT open sessions, same number, one connection.

        If `uq_pr_session` were ever narrowed to `(connection, pr_number)` this
        seed would fail instead of the tests below quietly becoming vacuous.
        """
        seeded = await _seed()
        assert seeded["one"]["sess_id"] != seeded["two"]["sess_id"]
        async with get_db_session() as db:
            a = await db.get(PRSession, seeded["one"]["sess_id"])
            b = await db.get(PRSession, seeded["two"]["sess_id"])
            assert a is not None and b is not None
            assert a.vcs_connection_id == b.vcs_connection_id
            assert a.pr_number == b.pr_number == PR
            assert a.repo != b.repo

    async def test_a_runs_status_refresh_names_its_own_session(self, monkeypatch):
        """The reported symptom: the losing PR's comment froze for ever.

        Each run must enqueue against the session for ITS repo. On the pre-fix
        predicate this resolved to whichever row the planner returned first, so
        every run on the losing PR drove the winning PR's comment and the losing
        one was never refreshed again.
        """
        seeded = await _seed()
        captured: list[str] = []

        async def _capture(_name, payload, **_kw):
            captured.append(payload["session_id"])

        monkeypatch.setattr(vcs_status_comment, "enqueue_trigger", _capture)

        for side in ("one", "two"):
            captured.clear()
            async with get_db_session() as db:
                run = await db.get(Run, seeded[side]["run_id"])
                assert run is not None
                await vcs_status_comment.refresh_for_run(db, run, "test")
            assert captured == [str(seeded[side]["sess_id"])], (
                f"the run in repo-{side} enqueued {captured} but its own session is "
                f"{seeded[side]['sess_id']} ({seeded[side]['repo']})"
            )

    async def test_auto_merge_resolves_its_own_session_instead_of_raising(self, monkeypatch):
        """The second site, whose symptom was the opposite one.

        `handle_vcs_apply_completed` used `scalar_one_or_none`, so this pair of
        rows raised `MultipleResultsFound` -- the scheduler logged it and counted
        it, and auto-merge never fired for either PR. It enqueues the comment
        refresh before it evaluates the gate, so that enqueue is what says which
        session it resolved. Both workspaces have `auto_merge` false, so the
        handler returns straight after it.
        """
        seeded = await _seed()
        captured: list[str] = []

        async def _capture(_name, payload, **_kw):
            captured.append(payload["session_id"])

        monkeypatch.setattr(vcs_auto_merge, "enqueue_trigger", _capture)

        for side in ("one", "two"):
            captured.clear()
            await vcs_auto_merge.handle_vcs_apply_completed(
                {"pr_number": PR, "workspace_id": str(seeded[side]["ws_id"])}
            )
            assert captured == [str(seeded[side]["sess_id"])], (
                f"the apply in repo-{side} resolved {captured} but its own session "
                f"is {seeded[side]['sess_id']} ({seeded[side]['repo']})"
            )
