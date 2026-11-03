"""A PR session is closed by the provider's answer, not by a sibling
workspace's branch (#2349).

`_reconcile_closed_pr_sessions` closes every open `PRSession` on a
(connection, repo) that is absent from the open-PR set it is handed, and
cancels that session's runs. Its scope is right -- the session table is keyed
on (connection, repo, pr_number) -- but the set came from
`_list_open_prs(..., base_branch)`, which asks the provider to filter to one
branch. A repo-scoped predicate fed a branch-scoped answer makes every PR
targeting another branch indistinguishable from a closed one, so two
workspaces on one repository tracking different branches closed each other's
sessions on every poll cycle.

Mocked at the PROVIDER boundary rather than at `_list_all_open_prs`, so that
reverting the fix is a real A/B: both listers stay reachable and whichever the
code chooses gets an answer.

The planning-filter test matters nearly as much as the fix. Widening the
listing without filtering locally would have every workspace plan every PR on
the repository -- considerably worse than the fault being fixed, and nothing
else here would notice.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import github_service
from terrapod.services.vcs_poller import _poll_workspace_prs
from terrapod.services.vcs_provider import OpenPullRequests, PullRequest


def _conn():
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="github",
        server_url="",
        token="t",
        github_app_id="1",
        github_installation_id="2",
    )


def _ws(branch="main"):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "ws"
    ws.vcs_branch = branch
    # No prefixes and no working directory, so the planning loop takes its
    # provider-free path and reaches run creation directly.
    ws.working_directory = ""
    ws.trigger_prefixes = []
    ws.vcs_last_commit_sha = ""
    # Not apply_then_merge: comment polling would reach the provider, and this
    # file is about which PRs count as open, not about commands.
    ws.vcs_workflow = "merge_then_apply"
    ws.allow_fork_pr_plans = False
    return ws


def _db():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


def _pr(number, base_ref):
    return PullRequest(
        number=number,
        head_sha=f"sha{number}",
        head_ref=f"feature/{number}",
        title=f"pr {number}",
        from_fork=False,
        base_ref=base_ref,
    )


ON_MAIN = _pr(1, "main")
ON_DEVELOP = _pr(2, "develop")


def _as_github_dicts(prs, base_branch):
    """What the branch-scoped provider call returns: server-side filtered."""
    return [
        {
            "number": p.number,
            "head_sha": p.head_sha,
            "head_ref": p.head_ref,
            "title": p.title,
            "from_fork": p.from_fork,
            "base_ref": p.base_ref,
        }
        for p in prs
        if p.base_ref == base_branch
    ]


async def _cycle(ws, *, open_on_repo, complete=True):
    """One PR poll. Returns (set handed to reconciliation, PR numbers planned)."""
    reconciled: dict[str, set[int]] = {}
    planned: list[int] = []

    async def _reconcile(db, conn, repo, open_pr_numbers):
        reconciled["numbers"] = set(open_pr_numbers)

    async def _create(*a, **k):
        # `*a, **k` rather than the real parameter list: the stub has no
        # opinion about the other fifteen arguments, and enumerating them makes
        # it break whenever one moves.
        planned.append(k["pr_number"])
        return MagicMock(id=uuid.uuid4())

    with (
        patch.object(
            github_service,
            "list_open_pull_requests_for_repo",
            new=AsyncMock(return_value=OpenPullRequests(list(open_on_repo), complete=complete)),
        ),
        patch.object(
            github_service,
            "list_open_pull_requests",
            new=AsyncMock(
                side_effect=lambda conn, owner, repo, base: _as_github_dicts(open_on_repo, base)
            ),
        ),
        patch("terrapod.services.vcs_poller._reconcile_closed_pr_sessions", new=_reconcile),
        patch("terrapod.services.vcs_poller._poll_pr_comments", new=AsyncMock()),
        patch("terrapod.services.vcs_poller._create_vcs_run", new=_create),
        patch("terrapod.services.vcs_poller._upsert_pr_session", new=AsyncMock()),
        patch("terrapod.services.vcs_poller.enqueue_trigger", new=AsyncMock()),
    ):
        await _poll_workspace_prs(_db(), ws, _conn(), "org", "repo", ws.vcs_branch, meta=None)

    return reconciled.get("numbers"), planned


class TestReconciliationSeesTheWholeRepository:
    async def test_a_pr_on_another_branch_is_not_reported_closed(self):
        """The defect. Polling the `main` workspace must not make the PR into
        `develop` look closed, because its session lives on the same
        (connection, repo) and would be closed and its runs cancelled."""
        numbers, _ = await _cycle(_ws("main"), open_on_repo=[ON_MAIN, ON_DEVELOP])
        assert numbers == {1, 2}, (
            "reconciliation was handed a branch-scoped set; PR 2's session would be closed"
        )

    async def test_the_tracked_branch_having_no_prs_closes_nothing(self):
        """The amplified form from the report: a workspace whose tracked branch
        resolves to no PRs -- including one that does not exist -- used to get
        an empty list and close every open session on the repository, every
        cycle."""
        numbers, planned = await _cycle(_ws("no-such-branch"), open_on_repo=[ON_MAIN, ON_DEVELOP])
        assert numbers == {1, 2}
        assert planned == [], "a nonexistent branch must plan nothing"

    async def test_a_genuinely_closed_pr_is_still_reported_closed(self):
        """The control. The fix must not amount to disabling reconciliation."""
        numbers, _ = await _cycle(_ws("main"), open_on_repo=[ON_MAIN])
        assert numbers == {1}

    async def test_an_incomplete_listing_reconciles_nothing_at_all(self):
        """A truncated listing is not a smaller answer to the same question --
        it is a wrong one, in the direction that closes live sessions."""
        numbers, _ = await _cycle(_ws("main"), open_on_repo=[ON_MAIN], complete=False)
        assert numbers is None, "reconciled against a listing that stopped short"


class TestPlanningStaysOnTheTrackedBranch:
    async def test_only_prs_targeting_the_tracked_branch_are_planned(self):
        """Without the local filter the widened listing would have every
        workspace plan every PR on the repository."""
        _, planned = await _cycle(_ws("main"), open_on_repo=[ON_MAIN, ON_DEVELOP])
        assert planned == [1]

    async def test_the_sibling_workspace_plans_its_own_branchs_pr(self):
        """Stated as a pair with the above: the filter must select, not just
        exclude."""
        _, planned = await _cycle(_ws("develop"), open_on_repo=[ON_MAIN, ON_DEVELOP])
        assert planned == [2]


class TestTheRepoWideListingIsPaginatedAndSaysSo:
    """The hazard the widening introduces, and the reason `complete` exists.

    Both providers cap a page at 100. The branch-scoped listers this replaces
    took one page and reported nothing about truncation, which was survivable
    while the question was "which PRs target this branch" and is not when the
    answer decides which sessions to close: a truncated list closes live
    sessions, which is the same defect in a quieter form.
    """

    def _page(self, numbers, *, next_url=None):
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.headers = {}
        r.links = {"next": {"url": next_url}} if next_url else {}
        r.json.return_value = [
            {
                "number": n,
                "head": {"sha": f"sha{n}", "ref": f"feature/{n}"},
                "base": {"ref": "main"},
                "title": f"pr {n}",
                "user": {"login": "someone"},
            }
            for n in numbers
        ]
        return r

    async def test_github_follows_the_next_link_to_the_end(self):
        pages = [self._page([1, 2], next_url="https://api/next"), self._page([3])]
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(github_service, "_github_request", new=AsyncMock(side_effect=pages)),
            patch.object(github_service, "_is_fork", return_value=False),
        ):
            got = await github_service.list_open_pull_requests_for_repo(_conn(), "org", "repo")

        assert [p.number for p in got.prs] == [1, 2, 3]
        assert got.complete is True
        assert [p.base_ref for p in got.prs] == ["main", "main", "main"]

    async def test_github_reports_incomplete_when_it_hits_the_page_cap(self):
        """Never-ending pagination must stop AND say that it stopped, or the
        reconciler would conclude from a truncated set."""
        endless = AsyncMock(side_effect=lambda *a, **k: self._page([1], next_url="https://api/n"))
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(github_service, "_github_request", new=endless),
            patch.object(github_service, "_is_fork", return_value=False),
        ):
            got = await github_service.list_open_pull_requests_for_repo(_conn(), "org", "repo")

        assert got.complete is False
        assert endless.await_count == github_service._OPEN_PR_PAGE_CAP

    async def test_gitlab_follows_the_next_link_and_reports_completeness(self):
        from terrapod.services import gitlab_service

        def _mr_page(iids, next_url=None):
            r = MagicMock()
            r.status_code = 200
            r.raise_for_status = MagicMock()
            r.headers = {}
            r.links = {"next": {"url": next_url}} if next_url else {}
            r.json.return_value = [
                {
                    "iid": i,
                    "sha": f"sha{i}",
                    "source_branch": f"feature/{i}",
                    "target_branch": "develop",
                    "title": f"mr {i}",
                }
                for i in iids
            ]
            return r

        pages = [_mr_page([1], "https://gl/next"), _mr_page([2])]
        with (
            patch.object(gitlab_service, "_gitlab_request", new=AsyncMock(side_effect=pages)),
            patch.object(gitlab_service, "_is_fork", return_value=False),
        ):
            got = await gitlab_service.list_open_merge_requests_for_repo(_conn(), "org", "repo")

        assert [p.number for p in got.prs] == [1, 2]
        assert [p.base_ref for p in got.prs] == ["develop", "develop"]
        assert got.complete is True

    async def test_gitlab_orders_by_created_at_because_id_is_refused(self):
        """Measured against gitlab.com, not reasoned about.

        `order_by=id` is what the keyset-paginating sibling
        (`list_group_projects`) passes, and the merge-requests endpoint answers
        `400 {"error": "order_by does not have a valid value"}` for it. Copying
        it here would have failed on every GitLab deployment, and no mocked
        test can see that -- which is exactly why this one pins the value the
        live call established rather than asserting the request succeeds.

        `created_at` is also the ordering this needs rather than merely one
        that is accepted: it is immutable, so nothing moves between pages while
        the walk is in progress.
        """
        from terrapod.services import gitlab_service

        page = MagicMock()
        page.status_code = 200
        page.raise_for_status = MagicMock()
        page.headers = {}
        page.links = {}
        page.json.return_value = []

        with patch.object(
            gitlab_service, "_gitlab_request", new=AsyncMock(return_value=page)
        ) as req:
            await gitlab_service.list_open_merge_requests_for_repo(_conn(), "org", "repo")

        params = req.await_args.kwargs["params"]
        assert params["order_by"] == "created_at", f"GitLab 400s on {params['order_by']!r}"
        assert params["order_by"] != "id"
        assert params["sort"] == "asc"

    async def test_github_orders_by_an_immutable_key(self):
        """`updated` is mutable, so under page-based pagination an item can move
        between pages mid-walk -- and an item missed on an otherwise complete
        walk reads as a closed pull request, which is the defect being fixed.
        The branch-scoped sibling may keep `updated` because it takes one page.
        """
        page = self._page([])
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(
                github_service, "_github_request", new=AsyncMock(return_value=page)
            ) as req,
        ):
            await github_service.list_open_pull_requests_for_repo(_conn(), "org", "repo")

        params = req.await_args.kwargs["params"]
        assert params["sort"] == "created", "a mutable sort key reorders pages mid-walk"
        assert params["direction"] == "asc"
