"""`terrapod plan` cancels the PR's run AND replaces it (#1795).

The bug this pins was a dead end rather than a hiccup. `_route_plan` cancelled
the run and left it to the poller, but the poller's dedup matches any run for
the (workspace, sha, pr) triple including terminal ones — so the run the
command had just cancelled was itself what blocked the replacement. The PR was
left with no run, and because the dedup keys on the SHA, every later
`terrapod plan` on that commit did nothing at all. Only a new commit escaped.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import vcs_command_dispatcher


def _ws(**kw):
    base = {
        "id": uuid.uuid4(),
        "name": "prod-network",
        "vcs_connection_id": uuid.uuid4(),
        "vcs_repo_url": "https://github.com/org/repo",
        "vcs_workflow": "apply_then_merge",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _sess(**kw):
    base = {
        "id": uuid.uuid4(),
        "repo": "org/repo",
        "pr_number": 7,
        "head_sha": "deadbeef",
        "created_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _prior_run(**kw):
    base = {
        "id": uuid.uuid4(),
        "vcs_branch": "feature/thing",
        "vcs_commit_sha": "deadbeef",
        "vcs_actor_login": None,
        "vcs_actor_user_id": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _db_returning(active_runs, prior):
    """A db whose first execute() is the active-run query and second the prior-run one."""
    active_res = MagicMock()
    active_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=active_runs)))
    prior_res = MagicMock()
    prior_res.scalar_one_or_none = MagicMock(return_value=prior)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[active_res, prior_res])
    db.get = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), provider="github"))
    return db


async def test_a_replacement_run_is_created_after_the_cancel():
    ws = _ws()
    sess = _sess()
    live = _prior_run()
    db = _db_returning([live], live)
    created = SimpleNamespace(id=uuid.uuid4(), vcs_actor_login=None, vcs_actor_user_id=None)

    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock) as cancel,
        patch(
            "terrapod.services.vcs_poller._create_vcs_run",
            new_callable=AsyncMock,
            return_value=created,
        ) as create,
    ):
        await vcs_command_dispatcher._route_plan(db, sess, [ws], "octocat", "12345")

    cancel.assert_awaited_once()
    create.assert_awaited_once()
    # The whole point: without this the PR is left with no run at all.
    kwargs = create.await_args.kwargs
    assert kwargs["pr_number"] == 7
    # The branch comes off the run we replaced -- the session stores the head
    # SHA but not the head REF, so the cancelled row is the only local source.
    assert create.await_args.args[6] == "feature/thing"
    assert create.await_args.args[5] == "deadbeef"


async def test_the_replacement_bypasses_the_dedup_the_cancel_created():
    """Without `replaces_canceled` the just-cancelled run blocks its own replacement."""
    ws = _ws()
    live = _prior_run()
    db = _db_returning([live], live)

    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock),
        patch(
            "terrapod.services.vcs_poller._create_vcs_run",
            new_callable=AsyncMock,
            return_value=None,
        ) as create,
    ):
        await vcs_command_dispatcher._route_plan(db, _sess(), [ws], "octocat", "12345")

    assert create.await_args.kwargs["replaces_canceled"] is True


async def test_an_apply_then_merge_replan_is_not_speculative():
    """The replacement has to be the same KIND of run, or `terrapod apply`
    afterwards finds a plan-only run it cannot confirm."""
    live = _prior_run()
    db = _db_returning([live], live)

    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock),
        patch(
            "terrapod.services.vcs_poller._create_vcs_run",
            new_callable=AsyncMock,
            return_value=None,
        ) as create,
    ):
        await vcs_command_dispatcher._route_plan(
            db, _sess(), [_ws(vcs_workflow="apply_then_merge")], "octocat", "1"
        )
    assert create.await_args.kwargs["speculative"] is False

    live2 = _prior_run()
    db2 = _db_returning([live2], live2)
    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock),
        patch(
            "terrapod.services.vcs_poller._create_vcs_run",
            new_callable=AsyncMock,
            return_value=None,
        ) as create2,
    ):
        await vcs_command_dispatcher._route_plan(
            db2, _sess(), [_ws(vcs_workflow="merge_then_apply")], "octocat", "1"
        )
    assert create2.await_args.kwargs["speculative"] is True


async def test_a_pr_with_no_prior_run_is_left_to_the_poller():
    """Nothing was cancelled and there is no branch to infer, so the poller
    makes the PR's first run as it always did."""
    db = _db_returning([], None)

    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock),
        patch("terrapod.services.vcs_poller._create_vcs_run", new_callable=AsyncMock) as create,
    ):
        await vcs_command_dispatcher._route_plan(db, _sess(), [_ws()], "octocat", "1")

    create.assert_not_awaited()


async def test_one_workspace_failing_does_not_strand_the_others():
    """A command naming several workspaces must not lose the rest because one
    could not be re-planned."""
    ws_a, ws_b = _ws(name="a"), _ws(name="b")
    live = _prior_run()

    active_res = MagicMock()
    active_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[live])))
    prior_res = MagicMock()
    prior_res.scalar_one_or_none = MagicMock(return_value=live)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[active_res, prior_res, active_res, prior_res])
    db.get = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), provider="github"))

    with (
        patch("terrapod.services.run_service.cancel_run", new_callable=AsyncMock),
        patch(
            "terrapod.services.vcs_poller._create_vcs_run",
            new_callable=AsyncMock,
            side_effect=[
                RuntimeError("provider down"),
                SimpleNamespace(id=uuid.uuid4(), vcs_actor_login=None, vcs_actor_user_id=None),
            ],
        ) as create,
    ):
        await vcs_command_dispatcher._route_plan(db, _sess(), [ws_a, ws_b], "octocat", "1")

    assert create.await_count == 2
