"""The aggregate statuses reach the provider, including for a PR that touches no workspace.

Real Postgres and Redis, driven through `handle_immediate_poll` and `poll_cycle`
with the real collector. The provider is faked at the HTTP request boundary
(`_github_request` / `_gitlab_request`), so the status bodies asserted on are
what would go on the wire; listings, compare and the archive are faked one level
above, as in `test_pr_aggregate_evaluation.py`.
"""

import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from terrapod.db.models import VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import git_fetch, github_service, gitlab_service
from terrapod.services.vcs_poller import handle_immediate_poll, poll_cycle
from terrapod.services.vcs_provider import OpenPullRequests, PullRequest
from tests.integration.test_pr_aggregate_evaluation import (
    BRANCH_SHA,
    HEAD_SHA,
    REPO,
    _feature_keys,
    _Provider,
    _seed,
)

PLAN, POLICY, APPLY = "terrapod/plan", "terrapod/policy", "terrapod/apply"


class _Wire:
    """Records commit-status requests; `status` is what the provider answers."""

    def __init__(self, status: int = 201):
        self.status = status
        self.posts: list[dict] = []
        self.landed: list[dict] = []
        self.conns: list[uuid.UUID] = []

    def _response(self):
        resp = MagicMock()
        resp.status_code = self.status
        if self.status >= 400:
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    str(self.status), request=MagicMock(), response=resp
                )
            )
        else:
            resp.raise_for_status = MagicMock()
        return resp

    async def github(self, method, url, token, *, conn, **kw):
        assert method == "POST" and "/statuses/" in url, f"unexpected request {method} {url}"
        self.posts.append({"url": url, **kw["json"]})
        if self.status < 400:
            self.landed.append({"url": url, **kw["json"]})
        self.conns.append(conn.id)
        return self._response()

    async def gitlab(self, method, url, conn, **kw):
        assert method == "POST" and "/statuses/" in url, f"unexpected request {method} {url}"
        self.posts.append({"url": url, **kw["json"]})
        return self._response()

    def patches(self):
        return [
            patch.object(github_service, "_api_call_token", AsyncMock(return_value="tok")),
            patch.object(github_service, "_github_request", self.github),
            patch.object(gitlab_service, "_gitlab_request", self.gitlab),
            patch("terrapod.services.vcs_poller._poll_pr_comments", new=AsyncMock()),
        ]

    def by_context(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for p in self.posts:
            out.setdefault(p.get("context") or p.get("name"), []).append(p)
        return out


async def _seed_one(*, workflow="merge_then_apply", provider="github", prefix="svc/a"):
    tag = uuid.uuid4().hex[:8]
    url = f"https://{'gitlab' if provider == 'gitlab' else 'github'}.com/acme/mono"
    async with get_db_session() as db:
        conn = VCSConnection(
            provider=provider,
            name=f"post-{tag}",
            server_url="",
            token="x",
            status="active",
            github_installation_id=int(tag[:7], 16),
        )
        db.add(conn)
        await db.flush()
        ws = Workspace(
            name=f"post-a-{tag}",
            vcs_connection_id=conn.id,
            vcs_repo_url=url,
            vcs_branch="main",
            trigger_prefixes=[prefix],
            vcs_last_commit_sha=BRANCH_SHA,
            vcs_workflow=workflow,
        )
        db.add(ws)
        await db.flush()
        await db.commit()
        return conn.id, ws.id


async def _run(entry, provider: _Provider, wire: _Wire, *, gitlab=False):
    with ExitStack() as stack:
        for p in provider.patches():
            stack.enter_context(p)
        for p in wire.patches():
            stack.enter_context(p)
        if gitlab:
            pr = provider.pr
            stack.enter_context(
                patch.object(gitlab_service, "get_branch_sha", AsyncMock(return_value=BRANCH_SHA))
            )
            stack.enter_context(
                patch.object(gitlab_service, "get_default_branch", AsyncMock(return_value="main"))
            )
            stack.enter_context(
                patch.object(
                    gitlab_service,
                    "list_open_merge_requests_for_repo",
                    AsyncMock(return_value=OpenPullRequests([pr], complete=True)),
                )
            )
            stack.enter_context(
                patch.object(
                    gitlab_service,
                    "get_changed_files",
                    AsyncMock(return_value=provider.changed),
                )
            )
        await entry()


def _immediate(provider="github"):
    async def go():
        await handle_immediate_poll({"repo": REPO, "provider": provider})

    return go


class TestAPRThatTouchesNoWorkspaceGetsGreenPlanAndPolicy:
    """The case the whole feature exists for: nobody else would ever post these."""

    async def test_immediate_poll(self, app):
        await _seed_one()
        wire = _Wire()
        await _run(_immediate(), _Provider(changed=["docs/readme.md"]), wire)

        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["success"]
        assert [p["state"] for p in got[POLICY]] == ["success"]
        assert got[PLAN][0]["description"] == "No Terrapod workspaces affected"
        assert APPLY not in got
        assert all(HEAD_SHA in p["url"] for p in wire.posts)
        assert not any(c.startswith("terrapod/post-") for c in got)

    async def test_poll_cycle(self, app):
        await _seed_one()
        wire = _Wire()
        await _run(poll_cycle, _Provider(changed=["docs/readme.md"]), wire)

        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["success"]
        assert [p["state"] for p in got[POLICY]] == ["success"]
        assert APPLY not in got

    async def test_an_apply_then_merge_repository_also_gets_nothing_to_apply(self, app):
        await _seed_one(workflow="apply_then_merge")
        wire = _Wire()
        await _run(_immediate(), _Provider(changed=["docs/readme.md"]), wire)

        got = wire.by_context()
        assert [p["state"] for p in got[APPLY]] == ["success"]
        assert got[APPLY][0]["description"] == "Nothing to apply"

    async def test_the_same_with_the_archive_fetch_failing_never_posts_success(self, app):
        await _seed_one()
        wire = _Wire()
        provider = _Provider(archive_fails_for="a")
        await _run(_immediate(), provider, wire)

        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["pending"]
        assert [p["state"] for p in got[POLICY]] == ["pending"]
        assert not any(p["state"] == "success" for p in wire.posts)

    async def test_a_second_poll_posts_nothing_new(self, app):
        await _seed_one()
        wire = _Wire()
        provider = _Provider(changed=["docs/readme.md"])
        await _run(_immediate(), provider, wire)
        first = len(wire.posts)
        assert first == 2
        await _run(_immediate(), provider, wire)
        assert len(wire.posts) == first

    async def test_a_4xx_is_final_not_raised_and_not_hammered(self, app):
        await _seed_one()
        wire = _Wire(status=422)
        provider = _Provider(changed=["docs/readme.md"])
        await _run(_immediate(), provider, wire)
        first = len(wire.posts)
        assert first == 2
        await _run(_immediate(), provider, wire)
        assert len(wire.posts) == first

    async def test_gitlab_posts_the_name_and_failed_not_failure(self, app):
        await _seed_one(provider="gitlab")
        wire = _Wire()
        pr = PullRequest(5, HEAD_SHA, "feature/x", "a change", from_fork=False, base_ref="main")
        provider = _Provider(pr=pr, changed=["docs/readme.md"])
        await _run(_immediate("gitlab"), provider, wire, gitlab=True)

        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["success"]
        assert "name" in got[PLAN][0] and "context" not in got[PLAN][0]
        assert len(got[PLAN][0]["description"]) <= 140


@pytest.fixture(autouse=True)
def _no_real_archive():
    # `_Provider` patches the archive; nothing here should reach git.
    assert git_fetch.sparse_archive_to_storage
    yield


class TestTheDispatcherHook:
    """The per-run hook, through the real handler, collector and Redis record."""

    async def _planned_atm_run(self, wire, workflow="apply_then_merge"):
        from sqlalchemy import select

        from terrapod.db.models import Run

        _, ws_id = await _seed_one(workflow=workflow)
        await _run(_immediate(), _Provider(), wire)
        async with get_db_session() as db:
            run = (await db.execute(select(Run).where(Run.workspace_id == ws_id))).scalars().one()
            run.status = "planned"
            run.plan_finished_at = datetime.now(UTC)
            run.has_changes = True
            await db.commit()
            return run.id, ws_id

    async def _dispatch(self, wire, run_id, ws_id, status="planned"):
        from terrapod.services.vcs_status_dispatcher import handle_vcs_commit_status

        with ExitStack() as stack:
            for p in wire.patches():
                stack.enter_context(p)
            await handle_vcs_commit_status(
                {
                    "run_id": str(run_id),
                    "workspace_id": str(ws_id),
                    "target_status": status,
                    "has_changes": True,
                }
            )

    async def test_a_planned_apply_then_merge_run_turns_plan_green_while_its_own_status_stays_pending(
        self, app
    ):
        wire = _Wire()
        run_id, ws_id = await self._planned_atm_run(wire)
        before = len(wire.posts)
        await self._dispatch(wire, run_id, ws_id)

        new = wire.posts[before:]
        got = {(p.get("context")): p["state"] for p in new}
        assert got["terrapod/plan"] == "success"
        assert got["terrapod/policy"] == "success"
        assert got["terrapod/apply"] == "pending"
        per_workspace = [c for c in got if c.startswith("terrapod/post-a-")]
        assert [got[c] for c in per_workspace] == ["pending"]

    async def test_the_overlay_is_used_when_the_row_has_not_caught_up(self, app):
        wire = _Wire()
        run_id, ws_id = await self._planned_atm_run(wire)
        async with get_db_session() as db:
            from terrapod.db.models import Run

            run = await db.get(Run, run_id)
            run.status = "planning"
            run.plan_finished_at = None
            await db.commit()
        before = len(wire.posts)
        await self._dispatch(wire, run_id, ws_id, status="planned")
        got = {p["context"]: p["state"] for p in wire.posts[before:]}
        assert got["terrapod/plan"] == "success"

    async def test_a_second_event_for_the_same_state_posts_no_aggregate_again(self, app):
        wire = _Wire()
        run_id, ws_id = await self._planned_atm_run(wire)
        await self._dispatch(wire, run_id, ws_id)
        before = len(wire.posts)
        await self._dispatch(wire, run_id, ws_id)
        again = [p["context"] for p in wire.posts[before:]]
        assert not [c for c in again if c in (PLAN, POLICY, APPLY)]


class TestWhichConnectionPosts:
    @pytest.mark.parametrize("changed", [["svc/a/main.tf"], ["svc/b/main.tf"], ["docs/readme.md"]])
    async def test_one_connection_posts_every_pr_of_the_repository(self, app, changed):
        await _seed(b_url="https://github.com/ACME/Mono.git", b_own_connection=True)
        async with get_db_session() as db:
            lowest = min((await db.execute(select(VCSConnection.id))).scalars().all())
        wire = _Wire()
        await _run(poll_cycle, _Provider(changed=changed), wire)

        got = wire.by_context()
        assert len(got[PLAN]) == 1 and len(got[POLICY]) == 1
        assert set(wire.conns) == {lowest}  # whichever workspace is affected

    @pytest.mark.parametrize("entry", ["cycle", "immediate"])
    async def test_prs_into_different_bases_post_through_the_same_connection(self, app, entry):
        from tests.integration.test_pr_aggregate_evaluation import OTHER_HEAD, _pr

        await _seed(
            b_branch="release/1", b_url="https://github.com/ACME/Mono.git", b_own_connection=True
        )
        async with get_db_session() as db:
            lowest = min((await db.execute(select(VCSConnection.id))).scalars().all())
        prs = [
            _pr(HEAD_SHA, 5, base="main"),
            _pr(OTHER_HEAD, 9, base="release/1"),
            _pr("4" * 40, 11, base="hotfix"),
        ]
        wire = _Wire()
        run = poll_cycle if entry == "cycle" else _immediate()
        await _run(run, _Provider(prs=prs, changed=["docs/readme.md"]), wire)
        assert len(wire.posts) >= 6  # plan and policy for each of the three PRs
        assert set(wire.conns) == {lowest}


class TestEveryConnectionDisabled:
    async def test_nothing_is_posted(self, app):
        conn_id, _ = await _seed_one()
        async with get_db_session() as db:
            (await db.get(VCSConnection, conn_id)).status = "disabled"
            await db.commit()
        wire = _Wire()
        await _run(poll_cycle, _Provider(), wire)
        assert wire.posts == []


class TestSwitchedOffNothingIsPosted:
    @pytest.mark.parametrize("entry", ["cycle", "immediate"])
    async def test_a_pr_touching_nothing_gets_no_status_and_no_record(self, app, entry):
        from terrapod.config import settings

        await _seed_one()
        wire = _Wire()
        settings.vcs.commit_statuses.enabled = False
        try:
            run = poll_cycle if entry == "cycle" else _immediate()
            await _run(run, _Provider(changed=["docs/readme.md"]), wire)
        finally:
            settings.vcs.commit_statuses.enabled = True
        assert wire.posts == []
        assert await _feature_keys() == []


class TestAFailedDowngradeHealsOnTheNextPoll:
    async def test_the_poll_pass_retries_until_the_failure_lands(self, app):
        from terrapod.db.models import Run

        wire = _Wire()
        helper = TestTheDispatcherHook()
        # merge_then_apply: nothing else pending keeps the pass republishing
        run_id, ws_id = await helper._planned_atm_run(wire, "merge_then_apply")
        await helper._dispatch(wire, run_id, ws_id)
        plan = [p["state"] for p in wire.landed if p.get("context") == PLAN]
        assert plan[-1] == "success"
        assert APPLY not in wire.by_context()

        async with get_db_session() as db:
            run = await db.get(Run, run_id)
            run.status = "errored"
            await db.commit()
        wire.status = 502  # the provider is down for the downgrade
        await helper._dispatch(wire, run_id, ws_id, status="errored")
        plan = [p["state"] for p in wire.landed if p.get("context") == PLAN]
        assert plan[-1] == "success"  # still green on the provider: the downgrade did not land

        wire.status = 201
        from terrapod.services import vcs_aggregate_post

        later = vcs_aggregate_post._now() + 3600  # past the failed marker's backoff
        with patch.object(vcs_aggregate_post, "_now", return_value=later):
            await _run(_immediate(), _Provider(), wire)  # nothing about the record changed
        plan = [p["state"] for p in wire.landed if p.get("context") == PLAN]
        assert plan[-1] == "failure"


class TestAPullRequestIntoAnUntrackedBranchGetsGreenChecks:
    async def test_immediate_poll_posts_plan_and_policy_success_and_no_apply(self, app):
        from tests.integration.test_pr_aggregate_evaluation import _pr

        await _seed_one()
        wire = _Wire()
        provider = _Provider(prs=[_pr(HEAD_SHA, 9, base="release/1")])
        await _run(_immediate(), provider, wire)
        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["success"]
        assert [p["state"] for p in got[POLICY]] == ["success"]
        assert APPLY not in got

    async def test_poll_cycle_posts_them_too(self, app):
        from tests.integration.test_pr_aggregate_evaluation import _pr

        await _seed_one(workflow="apply_then_merge")
        wire = _Wire()
        await _run(poll_cycle, _Provider(prs=[_pr(HEAD_SHA, 9, base="release/1")]), wire)
        got = wire.by_context()
        assert [p["state"] for p in got[PLAN]] == ["success"]
        assert APPLY not in got  # nobody evaluated an apply for it

    async def test_an_incomplete_listing_posts_nothing(self, app):
        from tests.integration.test_pr_aggregate_evaluation import _pr

        await _seed_one()
        wire = _Wire()
        provider = _Provider(prs=[_pr(HEAD_SHA, 9, base="release/1")], listing_complete=False)
        await _run(_immediate(), provider, wire)
        assert wire.posts == []
