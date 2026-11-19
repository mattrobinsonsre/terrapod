"""Which runs count towards a PR head's verdicts, and what the PR comment's own
collector does with the same rows (real Postgres)."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services.run_service import MODULE_IMPACT_POLLER_CREATED_BY
from terrapod.services.vcs_aggregate_post import collect_head_rows
from terrapod.services.vcs_pr_decisions import repo_scope
from terrapod.services.vcs_status_comment import _collect_rows

HEAD = "a" * 40
OLD = "b" * 40
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = repo_scope(SimpleNamespace(provider="github", server_url=""), "acme/mono")


async def _world():
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        conn = VCSConnection(
            provider="github",
            name=f"rows-{tag}",
            server_url="",
            token="x",
            status="active",
            github_installation_id=int(tag[:7], 16),
        )
        db.add(conn)
        await db.flush()
        out = {"conn": conn.id}
        for label, url in (
            ("a", "https://github.com/acme/mono"),
            ("b", "https://github.com/Acme/Mono.git"),
            ("other", "https://github.com/acme/other"),
        ):
            ws = Workspace(
                name=f"rows-{label}-{tag}",
                vcs_connection_id=conn.id,
                vcs_repo_url=url,
                vcs_branch="main",
            )
            db.add(ws)
            await db.flush()
            out[label] = ws.id
        await db.commit()
        return out


async def _run(ws_id, *, sha=HEAD, pr=5, status="planned", minutes=0, source="vcs", **kw):
    async with get_db_session() as db:
        run = Run(
            workspace_id=ws_id,
            created_by=kw.pop("created_by", "tests"),
            source=source,
            vcs_pull_request_number=pr,
            vcs_commit_sha=sha,
            status=status,
            created_at=T0 + timedelta(minutes=minutes),
            **kw,
        )
        db.add(run)
        await db.commit()
        return run.id


async def _rows(**kw):
    async with get_db_session() as db:
        return await collect_head_rows(db, SCOPE, 5, HEAD, **kw)


class TestCollectHeadRows:
    async def test_the_latest_run_per_workspace_wins(self, app):
        w = await _world()
        await _run(w["a"], status="errored", minutes=1)
        await _run(w["a"], status="planned", minutes=2)
        rows = await _rows()
        assert [(r.name.split("-")[1], r.status) for r in rows] == [("a", "planned")]

    async def test_a_superseded_run_does_not_hide_the_replacement(self, app):
        w = await _world()
        await _run(w["a"], status="canceled", minutes=1)
        await _run(w["a"], status="planning", minutes=2)
        assert [r.status for r in await _rows()] == ["planning"]

    async def test_runs_at_another_commit_are_excluded(self, app):
        w = await _world()
        await _run(w["a"], sha=OLD, status="planned", minutes=9)
        assert await _rows() == []

    async def test_module_impact_runs_are_excluded(self, app):
        w = await _world()
        await _run(
            w["a"],
            source="module-test",
            created_by=MODULE_IMPACT_POLLER_CREATED_BY,
            minutes=5,
        )
        assert await _rows() == []

    async def test_a_module_impact_run_does_not_shadow_the_workspaces_own(self, app):
        w = await _world()
        await _run(w["a"], status="errored", minutes=1)
        await _run(w["a"], source="module-test", status="planned", minutes=5)
        assert [r.status for r in await _rows()] == ["errored"]

    async def test_another_repositorys_run_with_the_same_pr_number_is_excluded(self, app):
        w = await _world()
        await _run(w["other"], status="planned")
        assert await _rows() == []

    async def test_url_spellings_of_one_repository_both_count(self, app):
        w = await _world()
        await _run(w["a"])
        await _run(w["b"])
        assert len(await _rows()) == 2

    async def test_the_overlay_replaces_the_triggering_runs_status(self, app):
        w = await _world()
        run_id = await _run(w["a"], status="planning")
        rows = await _rows(overlay=(run_id, "planned", True))
        assert [(r.status, r.has_changes) for r in rows] == [("planned", True)]

    async def test_the_overlay_never_overrides_a_newer_terminal_status(self, app):
        w = await _world()
        run_id = await _run(w["a"], status="errored")
        rows = await _rows(overlay=(run_id, "planning", None))
        assert [r.status for r in rows] == ["errored"]

    async def test_a_discard_racing_the_commit_is_seen_through_the_overlays(self, app):
        w = await _world()
        run_id = await _run(w["a"], status="planned")
        rows = await _rows(overlay={run_id: [("planned", True), ("discarded", None)]})
        assert [r.status for r in rows] == ["discarded"]

    async def test_the_overlay_of_another_run_changes_nothing(self, app):
        w = await _world()
        await _run(w["a"], status="planning")
        rows = await _rows(overlay=(uuid.uuid4(), "applied", True))
        assert [r.status for r in rows] == ["planning"]

    async def test_plan_and_apply_stamps_are_carried(self, app):
        w = await _world()
        await _run(w["a"], status="errored", plan_finished_at=T0, apply_started_at=T0)
        (row,) = await _rows()
        assert row.plan_finished and row.apply_started


class TestThePRCommentsOwnCollector:
    """The plan suspected `_collect_rows` leaks another repository's module-impact
    run into a PR comment. This establishes whether it does."""

    async def _session(self, conn_id):
        async with get_db_session() as db:
            sess = PRSession(vcs_connection_id=conn_id, repo="acme/mono", pr_number=5, state="open")
            db.add(sess)
            await db.commit()
            return sess.id

    @pytest.mark.xfail(
        strict=True,
        reason="reproduced: a module-impact run carrying another repository's PR number "
        "appears in this repository's PR comment",
    )
    async def test_a_module_impact_run_from_another_repository_is_not_a_row(self, app):
        w = await _world()
        await _run(w["a"], source="module-test", created_by=MODULE_IMPACT_POLLER_CREATED_BY)
        sess_id = await self._session(w["conn"])
        async with get_db_session() as db:
            sess = await db.get(PRSession, sess_id)
            assert await _collect_rows(db, sess) == []

    async def test_the_workspaces_own_run_at_an_older_commit_is_still_shown(self, app):
        # Not a defect: the comment reports the latest run, whatever its commit.
        w = await _world()
        await _run(w["a"], sha=OLD, status="planned")
        sess_id = await self._session(w["conn"])
        async with get_db_session() as db:
            sess = await db.get(PRSession, sess_id)
            assert len(await _collect_rows(db, sess)) == 1
