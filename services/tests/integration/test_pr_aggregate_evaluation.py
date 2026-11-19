"""The poller records which workspaces each PR head affects (real DB and Redis).

Driven through `handle_immediate_poll` and `poll_cycle` with the real collector,
because an optional `decisions` parameter makes the bypass the default and a
test that omits it proves nothing about the path production takes. Only the
provider is faked: `github_service` for listings, branch heads and the compare
call, and `git_fetch` (the boundary below the archive cache) for the archive.
"""

import contextlib
import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import git_fetch, github_service, vcs_pr_decisions, vcs_rate_limit
from terrapod.services.vcs_aggregate_status import aggregate
from terrapod.services.vcs_poller import handle_immediate_poll, poll_cycle
from terrapod.services.vcs_provider import OpenPullRequests, PullRequest
from terrapod.storage import get_storage

REPO_URL = "https://github.com/acme/mono"
REPO = "acme/mono"
BRANCH_SHA = "1" * 40
HEAD_SHA = "2" * 40
OTHER_HEAD = "3" * 40


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b'resource "null_resource" "x" {}\n'
        info = tarfile.TarInfo("main.tf")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SCOPE = vcs_pr_decisions.repo_scope(SimpleNamespace(provider="github", server_url=""), REPO)


async def _conn(db, tag, suffix="", status="active"):
    conn = VCSConnection(
        provider="github",
        name=f"agg-{tag}{suffix}",
        server_url="",
        token="x",
        status=status,
        github_installation_id=int(uuid.uuid4().hex[:7], 16),
    )
    db.add(conn)
    await db.flush()
    return conn


async def _seed(
    *,
    b_branch: str = "main",
    b_url: str = REPO_URL,
    b_own_connection: bool = False,
    b_status: str = "active",
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        conn = await _conn(db, tag)
        conn_b = await _conn(db, tag, "-b", b_status) if b_own_connection else conn
        ids = {}
        for label, prefix, branch, url, c in (
            ("a", "svc/a", "main", REPO_URL, conn),
            ("b", "svc/b", b_branch, b_url, conn_b),
        ):
            ws = Workspace(
                name=f"agg-{label}-{tag}",
                vcs_connection_id=c.id,
                vcs_repo_url=url,
                vcs_branch=branch,
                trigger_prefixes=[prefix],
                vcs_last_commit_sha=BRANCH_SHA,
            )
            db.add(ws)
            await db.flush()
            ids[label] = ws.id
        await db.commit()
        return conn.id, ids


def _pr(sha=HEAD_SHA, number=5, base="main"):
    return PullRequest(number, sha, "feature/x", "a change", from_fork=False, base_ref=base)


class _Provider:
    """Patches the provider boundary; `archive` and `default_branch` are the switches."""

    def __init__(
        self,
        *,
        pr=None,
        prs=None,
        changed=("svc/a/main.tf",),
        archive_fails_for=None,
        listing_complete=True,
        branch_sha_fails=(),
    ):
        self.branch_sha_fails = branch_sha_fails
        self.pr = pr or _pr()
        self.prs = list(prs) if prs is not None else [self.pr]
        self.listing_complete = listing_complete
        self.changed = list(changed)
        self.archive_fails = archive_fails_for
        self.compares = 0

    async def _sparse(self, conn, owner, repo, sha, paths, storage_key, *, clone_dir):
        if self.archive_fails:
            raise RuntimeError("archive fetch failed")
        data = _tarball()
        await get_storage().put(storage_key, data, content_type="application/x-tar")
        return len(data)

    async def _branch_sha(self, conn, owner, repo, branch, *a, **kw):
        if branch in self.branch_sha_fails:
            raise RuntimeError("provider 502")
        return BRANCH_SHA

    async def _changed(self, conn, owner, repo, base, head):
        self.compares += 1
        return list(self.changed)

    def patches(self, *, default_branch_error=False):
        default = (
            AsyncMock(side_effect=RuntimeError("provider 500"))
            if default_branch_error
            else AsyncMock(return_value="main")
        )
        return [
            patch.object(
                github_service,
                "get_repo_branch_sha",
                self._branch_sha,
            ),
            patch.object(github_service, "get_repo_default_branch", default),
            patch.object(
                github_service,
                "list_open_pull_requests_for_repo",
                AsyncMock(return_value=OpenPullRequests(self.prs, complete=self.listing_complete)),
            ),
            patch.object(github_service, "list_open_pull_requests", AsyncMock(return_value=[])),
            patch.object(github_service, "get_changed_files", self._changed),
            patch.object(git_fetch, "sparse_archive_to_storage", self._sparse),
            patch("terrapod.services.vcs_poller._poll_autodiscovery", new=AsyncMock()),
        ]


async def _poll(provider, *, default_branch_error=False):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in provider.patches(default_branch_error=default_branch_error):
            stack.enter_context(p)
        await handle_immediate_poll({"repo": REPO, "provider": "github"})


async def _runs(ws_id):
    async with get_db_session() as db:
        return (await db.execute(select(Run).where(Run.workspace_id == ws_id))).scalars().all()


async def _eval(sha=HEAD_SHA, number=5):
    return await vcs_pr_decisions.load(SCOPE, number, sha)


class TestThePollerRecordsWhatThePRAffects:
    async def test_a_pr_touching_only_one_workspace(self, app):
        _, ids = await _seed()
        await _poll(_Provider())

        ev = await _eval()
        assert ev is not None and ev.complete
        assert len(ev.affected) == 1 and ev.affected[0].startswith("agg-a-")
        assert not ev.undecided and not ev.not_evaluated and not ev.fork_skipped
        assert len(await _runs(ids["a"])) == 1
        assert await _runs(ids["b"]) == []

    async def test_a_pr_touching_nothing_is_complete_with_no_affected_workspace(self, app):
        _, ids = await _seed()
        await _poll(_Provider(changed=["docs/readme.md"]))

        ev = await _eval()
        assert ev is not None and ev.complete and ev.affected == ()
        assert aggregate([], ev).plan.state == "success"
        assert await _runs(ids["a"]) == [] and await _runs(ids["b"]) == []

    async def test_a_workspace_whose_poll_raises_is_not_evaluated(self, app):
        await _seed(b_branch="")
        await _poll(_Provider(), default_branch_error=True)

        ev = await _eval()
        assert ev is not None and not ev.complete
        assert len(ev.not_evaluated) == 1 and ev.not_evaluated[0].startswith("agg-b-")
        assert len(ev.affected) == 1
        assert aggregate([], ev).plan.state == "pending"

    async def test_an_archive_fetch_that_raises_leaves_the_workspace_undecided(self, app):
        _, ids = await _seed()
        await _poll(_Provider(archive_fails_for="a"))

        ev = await _eval()
        assert ev is not None and not ev.complete
        assert [n.split("-")[1] for n, _ in ev.undecided] == ["a"]
        assert ev.affected == ()
        assert await _runs(ids["a"]) == []
        assert aggregate([], ev).plan.state == "pending"

    async def test_a_transient_failure_does_not_flip_a_decided_pr(self, app):
        await _seed(b_branch="")
        await _poll(_Provider())
        first = await _eval()
        assert first is not None and first.complete

        await _poll(_Provider(), default_branch_error=True)
        second = await _eval()
        assert second == first

    async def test_a_new_head_sha_starts_fresh(self, app):
        await _seed(b_branch="")
        await _poll(_Provider())
        assert (await _eval()).complete

        newer = _Provider(pr=_pr(OTHER_HEAD))
        await _poll(newer, default_branch_error=True)
        ev = await _eval(sha=OTHER_HEAD)
        assert ev is not None and not ev.complete
        assert len(ev.not_evaluated) == 1
        assert (await _eval()).complete

    async def test_a_redis_outage_while_loading_is_not_a_record(self, app):
        _, _ = await _seed()
        await _poll(_Provider())
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            assert await _eval() is None

    async def test_the_head_and_the_ttl_are_stored(self, app):
        from terrapod.redis.client import get_redis_client

        _, _ = await _seed()
        await _poll(_Provider())
        redis = get_redis_client()
        assert await vcs_pr_decisions.latest_head(SCOPE, 5) == HEAD_SHA
        ttl = await redis.ttl(vcs_pr_decisions.evaluation_key(SCOPE, 5, HEAD_SHA))
        assert 0 < ttl <= 7 * 24 * 3600
        assert 0 < await redis.ttl(vcs_pr_decisions.head_key(SCOPE, 5)) <= 7 * 24 * 3600


class TestThePeriodicCycleRecordsToo:
    async def test_poll_cycle(self, app):
        from contextlib import ExitStack

        await _seed()
        provider = _Provider(changed=["docs/readme.md"])
        with ExitStack() as stack:
            for p in provider.patches():
                stack.enter_context(p)
            await poll_cycle()

        ev = await _eval()
        assert ev is not None and ev.complete and ev.affected == ()


_real_target = vcs_rate_limit.vcs_target


@contextlib.contextmanager
def _b_fails_before_its_repo_poll(*, repo, consumer, kind, labels):
    if consumer.startswith("agg-b-"):
        raise RuntimeError("transient failure before the repo poll")
    with _real_target(repo=repo, consumer=consumer, kind=kind, labels=labels):
        yield


async def _poll_cycle(provider):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in provider.patches():
            stack.enter_context(p)
        await poll_cycle()


def _assert_b_blocks(ev):
    assert ev is not None and not ev.complete
    assert len(ev.not_evaluated) == 1 and ev.not_evaluated[0].startswith("agg-b-")
    assert ev.affected == ()
    assert aggregate([], ev).plan.state == "pending"
    assert aggregate([], ev).policy.state == "pending"


class TestAWorkspaceThatFailsBeforeItsPollIsStillACandidate:
    """The PR changes only B's prefix and B fails before the old registration point."""

    async def test_immediate_poll_when_the_target_context_raises(self, app):
        await _seed()
        with patch.object(vcs_rate_limit, "vcs_target", _b_fails_before_its_repo_poll):
            await _poll(_Provider(changed=["svc/b/main.tf"]))
        _assert_b_blocks(await _eval())

    async def test_poll_cycle_when_the_target_context_raises(self, app):
        await _seed()
        with patch.object(vcs_rate_limit, "vcs_target", _b_fails_before_its_repo_poll):
            await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        _assert_b_blocks(await _eval())

    async def test_immediate_poll_when_the_workspace_poll_itself_raises(self, app):
        from terrapod.services import vcs_poller

        real = vcs_poller._poll_workspace

        async def flaky(db, ws, *a, **kw):
            if ws.name.startswith("agg-b-"):
                raise RuntimeError("load failed")
            return await real(db, ws, *a, **kw)

        await _seed()
        with patch.object(vcs_poller, "_poll_workspace", flaky):
            await _poll(_Provider(changed=["svc/b/main.tf"]))
        _assert_b_blocks(await _eval())

    async def test_poll_cycle_when_the_workspace_poll_itself_raises(self, app):
        from terrapod.services import vcs_poller

        real = vcs_poller._poll_workspace

        async def flaky(db, ws, *a, **kw):
            if ws.name.startswith("agg-b-"):
                raise RuntimeError("load failed")
            return await real(db, ws, *a, **kw)

        await _seed()
        with patch.object(vcs_poller, "_poll_workspace", flaky):
            await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        _assert_b_blocks(await _eval())


class TestOneRecordPerRepository:
    async def test_two_connections_and_url_spellings_share_one_record(self, app):
        await _seed(b_url="https://github.com/ACME/Mono.git", b_own_connection=True)
        await _poll_cycle(_Provider())

        ev = await _eval()
        assert ev is not None and ev.complete
        assert len(ev.affected) == 1 and ev.affected[0].startswith("agg-a-")
        from terrapod.redis.client import get_redis_client

        fields = await get_redis_client().hgetall(
            vcs_pr_decisions.evaluation_key(SCOPE, 5, HEAD_SHA)
        )
        assert (
            len(fields) == 2
            and len({__import__("json").loads(v)["c"] for v in fields.values()}) == 2
        )

    async def test_the_second_connection_failing_keeps_the_shared_record_incomplete(self, app):
        await _seed(b_url="https://github.com/ACME/Mono.git", b_own_connection=True)
        with patch.object(vcs_rate_limit, "vcs_target", _b_fails_before_its_repo_poll):
            await _poll_cycle(_Provider())
        ev = await _eval()
        assert ev is not None and not ev.complete

    async def test_a_deleted_workspace_stops_blocking_the_next_pass(self, app):
        _, ids = await _seed(b_branch="")
        await _poll(_Provider(), default_branch_error=True)
        ev = await _eval()
        assert ev is not None and len(ev.not_evaluated) == 1

        async with get_db_session() as db:
            await db.delete(await db.get(Workspace, ids["b"]))
            await db.commit()
        await _poll(_Provider())
        ev = await _eval()
        assert ev is not None and ev.complete and ev.not_evaluated == ()


class TestAPassThatSeesOnlySomeOfTheWorkspacesDeletesNothing:
    """The webhook poll and the periodic cycle must agree about one repository."""

    async def test_a_webhook_poll_does_not_erase_a_workspace_spelled_differently(self, app):
        await _seed(b_url="https://github.com/Acme/Mono")
        provider = _Provider(changed=["svc/b/main.tf"])
        await _poll_cycle(provider)
        first = await _eval()
        assert first is not None and len(first.affected) == 1
        assert first.affected[0].startswith("agg-b-")

        await _poll(_Provider(changed=["svc/b/main.tf"]))  # webhook for "acme/mono"
        second = await _eval()
        assert second is not None and second.affected == first.affected
        assert aggregate([], second).plan.state == "pending"

    async def test_the_webhook_poll_selects_the_same_way_the_scope_does(self, app):
        from terrapod.services.vcs_poller import _select_workspace_ids

        _, ids = await _seed(b_url="https://github.com/Acme/Mono.git")
        async with get_db_session() as db:
            got = await _select_workspace_ids(db, repo="acme/mono", provider="github")
        assert set(got) == {ids["a"], ids["b"]}


class TestAWorkspaceOnADisabledConnectionKeepsBlocking:
    async def test_its_affected_field_survives_the_disable(self, app):
        _, ids = await _seed(b_own_connection=True)
        await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        before = await _eval()
        assert before is not None and before.affected[0].startswith("agg-b-")

        await _set_status(ids["b"], "disabled")
        await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        after = await _eval()
        assert after is not None and after.affected == before.affected
        assert aggregate([], after).plan.state == "pending"

    async def test_a_new_head_waits_for_it_until_the_connection_is_back(self, app):
        _, ids = await _seed(b_own_connection=True, b_status="disabled")
        await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        ev = await _eval()
        assert ev is not None and not ev.complete
        assert [(n.split("-")[1], r) for n, r in ev.undecided] == [("b", "connection disabled")]
        assert aggregate([], ev).plan.state == "pending"

        await _set_status(ids["b"], "active")
        await _poll_cycle(_Provider(changed=["docs/readme.md"]))
        ev = await _eval()
        assert ev is not None and ev.complete and ev.not_evaluated == ()


async def _set_status(ws_id, status):
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        conn = await db.get(VCSConnection, ws.vcs_connection_id)
        conn.status = status
        await db.commit()


class TestAFailedRegistrationDistrustsTheOldRecord:
    async def test_the_previous_record_reads_pending_until_a_cycle_succeeds(self, app):
        from contextlib import ExitStack

        await _seed()
        await _poll_cycle(_Provider(changed=["docs/readme.md"]))
        assert (await _eval()).complete

        with ExitStack() as stack:
            for p in _Provider(changed=["docs/readme.md"]).patches():
                stack.enter_context(p)
            stack.enter_context(
                patch(
                    "terrapod.services.vcs_poller._register_candidates",
                    new=AsyncMock(return_value=False),
                )
            )
            await poll_cycle()
        assert await _eval() is None  # unverified: never green

        await _poll(
            _Provider(changed=["docs/readme.md"])
        )  # a webhook poll does not vouch for the rest
        assert await _eval() is None

        await _poll_cycle(_Provider(changed=["docs/readme.md"]))
        assert (await _eval()).complete


class TestAFailedWebhookRegistrationAlsoDistrustsTheRecord:
    async def test_a_failed_immediate_poll_marks_the_records_unverified(self, app):
        from contextlib import ExitStack

        await _seed()
        await _poll_cycle(_Provider(changed=["docs/readme.md"]))
        assert (await _eval()).complete

        with ExitStack() as stack:
            for p in _Provider(changed=["docs/readme.md"]).patches():
                stack.enter_context(p)
            stack.enter_context(
                patch(
                    "terrapod.services.vcs_poller._register_candidates",
                    new=AsyncMock(return_value=False),
                )
            )
            await handle_immediate_poll({"repo": REPO, "provider": "github"})
        assert await _eval() is None


class TestTheSelectionNormalisesLikeTheScope:
    async def test_a_url_with_a_git_suffix_and_trailing_slash_is_selected(self, app):
        from terrapod.services.vcs_poller import _select_workspace_ids

        _, ids = await _seed(b_url="https://github.com/acme/mono.git/")
        async with get_db_session() as db:
            got = await _select_workspace_ids(db, repo="acme/mono", provider="github")
        assert set(got) == {ids["a"], ids["b"]}


class TestPruningNeedsTheScopeToMatch:
    async def test_a_repointed_workspace_is_dropped_and_an_unchanged_one_kept(self, app):
        from terrapod.services.vcs_poller import _still_in_scope

        _, ids = await _seed()
        async with get_db_session() as db:
            ws = await db.get(Workspace, ids["b"])
            ws.vcs_repo_url = "https://github.com/acme/elsewhere"
            await db.commit()
        got = await _still_in_scope({ids["a"], ids["b"]}, SCOPE)
        assert got == {ids["a"]}

    async def test_a_workspace_that_only_changed_spelling_is_kept(self, app):
        from terrapod.services.vcs_poller import _still_in_scope

        _, ids = await _seed(b_url="https://github.com/ACME/Mono.git")
        assert await _still_in_scope({ids["b"]}, SCOPE) == {ids["b"]}


class TestADisabledConnectionIsLegible:
    async def test_the_blocker_names_the_connection(self, app):
        _, _ = await _seed(b_own_connection=True, b_status="disabled")
        await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
        ev = await _eval()
        assert ev is not None and not ev.complete
        assert [reason for _, reason in ev.undecided] == ["connection disabled"]
        assert "connection disabled" in aggregate([], ev).plan.description

    async def test_the_per_workspace_switch_does_not_exclude_a_workspace_from_the_aggregate(
        self, app
    ):
        from terrapod.config import settings

        _, _ = await _seed(b_own_connection=True, b_status="disabled")
        settings.vcs.commit_statuses.per_workspace = False
        try:
            await _poll_cycle(_Provider(changed=["svc/b/main.tf"]))
            ev = await _eval()
        finally:
            settings.vcs.commit_statuses.per_workspace = True
        assert ev is not None and not ev.complete

    def test_no_aggregate_module_reads_the_switch(self):
        import inspect

        from terrapod.services import vcs_aggregate_post, vcs_aggregate_status, vcs_pr_decisions

        for mod in (vcs_aggregate_post, vcs_aggregate_status, vcs_pr_decisions):
            assert "per_workspace" not in inspect.getsource(mod)


class TestAPullRequestIntoABranchNoWorkspaceTracks:
    """A ruleset requiring `terrapod/plan` on every branch must not leave such a PR at Expected."""

    UNTRACKED = 9

    def _provider(self, **kw):
        return _Provider(prs=[_pr(OTHER_HEAD, self.UNTRACKED, base="release/1")], **kw)

    async def test_immediate_poll_records_it_as_touching_no_workspace(self, app):
        await _seed()
        await _poll(self._provider())
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and ev.complete and ev.affected == ()
        assert ev.apply_then_merge_candidate is False
        agg = aggregate([], ev)
        assert agg.plan.state == "success" and agg.policy.state == "success"
        assert agg.apply is None

    async def test_poll_cycle_does_too(self, app):
        await _seed()
        await _poll_cycle(self._provider())
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and ev.complete and ev.affected == ()

    async def test_a_pr_into_a_tracked_branch_is_unchanged(self, app):
        await _seed()
        prs = [_pr(HEAD_SHA, 5, base="main"), _pr(OTHER_HEAD, self.UNTRACKED, base="release/1")]
        await _poll(_Provider(prs=prs))
        tracked = await _eval(sha=HEAD_SHA, number=5)
        assert tracked is not None and len(tracked.affected) == 1

    async def test_an_incomplete_listing_records_nothing_green(self, app):
        await _seed()
        await _poll(self._provider(listing_complete=False))
        assert await _eval(sha=OTHER_HEAD, number=self.UNTRACKED) is None

    async def test_a_workspace_whose_branch_is_unknown_keeps_it_pending(self, app):
        await _seed(b_branch="")
        await _poll(self._provider(), default_branch_error=True)
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and not ev.complete
        assert aggregate([], ev).plan.state == "pending"

    async def test_a_workspace_that_does_track_the_base_decides_it_itself(self, app):
        await _seed(b_branch="release/1")
        await _poll(self._provider(changed=["svc/b/main.tf"]))
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and len(ev.affected) == 1 and ev.affected[0].startswith("agg-b-")
        assert not ev.undecided


async def _repoint(ws_id, branch):
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        ws.vcs_branch = branch
        await db.commit()


class TestABaseThatStartsToBeTracked:
    """The marker left while no workspace tracked the base must not outlive that."""

    UNTRACKED = 9

    def _provider(self, **kw):
        return _Provider(prs=[_pr(OTHER_HEAD, self.UNTRACKED, base="release/1")], **kw)

    async def _green_then_repointed(self, entry):
        _, ids = await _seed()
        await entry(self._provider())
        before = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert before is not None and aggregate([], before).plan.state == "success"
        await _repoint(ids["b"], "release/1")
        return ids

    @pytest.mark.parametrize("entry", [_poll, _poll_cycle])
    async def test_a_failing_branch_poll_of_the_new_tracker_turns_it_pending(self, app, entry):
        await self._green_then_repointed(entry)
        await entry(self._provider(branch_sha_fails=("release/1",)))
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and not ev.complete
        assert aggregate([], ev).plan.state == "pending"

    @pytest.mark.parametrize("entry", [_poll, _poll_cycle])
    async def test_a_disabled_connection_of_the_new_tracker_turns_it_pending(self, app, entry):
        _, ids = await _seed(b_own_connection=True)
        await entry(self._provider())
        await _repoint(ids["b"], "release/1")
        await _set_status(ids["b"], "disabled")
        await entry(self._provider())
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and not ev.complete
        assert [(n.split("-")[1], r) for n, r in ev.undecided] == [("b", "connection disabled")]
        assert aggregate([], ev).plan.state == "pending"

    @pytest.mark.parametrize("entry", [_poll, _poll_cycle])
    async def test_a_healthy_new_tracker_decides_it_itself(self, app, entry):
        await self._green_then_repointed(entry)
        await entry(self._provider(changed=["svc/b/main.tf"]))
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and ev.complete
        assert len(ev.affected) == 1 and ev.affected[0].startswith("agg-b-")
        assert not any(n.startswith("(no workspace") for n in ev.not_evaluated)

    @pytest.mark.parametrize("entry", [_poll, _poll_cycle])
    async def test_a_tracked_base_that_becomes_untracked_stays_decided(self, app, entry):
        _, ids = await _seed(b_branch="release/1")
        await entry(self._provider(changed=["svc/b/main.tf"]))
        await _repoint(ids["b"], "main")
        await entry(self._provider())
        ev = await _eval(sha=OTHER_HEAD, number=self.UNTRACKED)
        assert ev is not None and ev.complete
        assert not ev.undecided and not ev.not_evaluated


async def _feature_keys():
    from terrapod.redis.client import get_redis_client

    prefixes = ("tp:vcs_pr_eval", "tp:vcs_pr_head", "tp:vcs_agg", "tp:vcs_disabled")
    return [
        k async for k in get_redis_client().scan_iter(match="tp:vcs_*") if k.startswith(prefixes)
    ]


class TestSwitchedOff:
    """`commit_statuses.enabled: false` leaves the poller as it was before the aggregates."""

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("entry", [_poll, _poll_cycle])
    async def test_nothing_is_recorded_but_the_runs_are_still_created(self, app, entry, enabled):
        from terrapod.config import settings

        _, ids = await _seed()
        settings.vcs.commit_statuses.enabled = enabled
        try:
            await entry(_Provider())
        finally:
            settings.vcs.commit_statuses.enabled = True
        assert len(await _runs(ids["a"])) == 1
        assert bool(await _feature_keys()) is enabled
        assert (await _eval() is not None) is enabled
