"""What a module autodiscovery rule's `repo-url` names (#1620).

Classification and namespace listings, with the provider mocked at its request
chokepoint (`_github_request` / `_gitlab_request`) so every real wrapper —
lookups, pagination, the ETag cache, the repository cap — runs.
"""

import uuid
from unittest.mock import patch

import httpx
import pytest

from terrapod.db.models import VCSConnection
from terrapod.services import github_service, gitlab_service
from terrapod.services import module_autodiscovery_targets as targets

_GH = "terrapod.services.github_service"
_GL = "terrapod.services.gitlab_service"


def _github_conn(login="org", server_url=""):
    return VCSConnection(
        id=uuid.uuid4(),
        provider="github",
        name="gh",
        server_url=server_url,
        token="k",
        status="active",
        github_installation_id=7,
        github_account_login=login,
    )


def _gitlab_conn(server_url="https://gitlab.example.com"):
    return VCSConnection(
        id=uuid.uuid4(), provider="gitlab", name="gl", server_url=server_url, token="t"
    )


def _resp(status, body=None, headers=None, url="https://api.test/x"):
    return httpx.Response(
        status, json=body, headers=headers or {}, request=httpx.Request("GET", url)
    )


def _repo_json(path, rid, **kw):
    owner, _, _name = path.rpartition("/")
    data = {
        "id": rid,
        "full_name": path,
        "html_url": f"https://github.com/{path}",
        "default_branch": "main",
        "owner": {"login": owner, "id": kw.pop("owner_id", 1)},
        "archived": False,
        "fork": False,
        "disabled": False,
        "size": 10,
        "pushed_at": "2026-09-01T00:00:00Z",
        "created_at": "2025-01-01T00:00:00Z",
    }
    data.update(kw)
    return data


class _Server:
    """Answers provider requests by path; anything unrouted is a 404."""

    def __init__(self, routes, base):
        self.routes = routes
        self.base = base
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def _answer(self, url, params, headers):
        path = url.removeprefix(self.base)
        self.calls.append(path)
        self.headers.append(dict(headers or {}))
        route = self.routes.get(path)
        if route is None:
            return _resp(404, {"message": "Not Found"}, url=url)
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(params or {}, headers or {})
        return route

    async def github(self, method, url, token, *, conn, params=None, headers=None, **kw):
        return self._answer(url, params, headers)

    async def gitlab(self, method, url, conn, *, params=None, headers=None, **kw):
        return self._answer(url, params, headers)


def _github(routes):
    server = _Server(routes, "https://api.github.com")
    return server, (
        patch(f"{_GH}._github_request", new=server.github),
        patch(f"{_GH}.get_installation_token", new=_token),
    )


async def _token(conn):
    return "t"


def _gitlab(routes, base="https://gitlab.example.com/api/v4"):
    server = _Server(routes, base)
    return server, patch(f"{_GL}._gitlab_request", new=server.gitlab)


# ── Strings ──────────────────────────────────────────────────────────────


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,path",
        [
            ("https://github.com/org/terraform-aws-vpc", "org/terraform-aws-vpc"),
            ("https://GitHub.com/org/repo.git", "org/repo"),
            ("https://github.com/org/repo/", "org/repo"),
            ("git@github.com:org/repo.git", "org/repo"),
            ("org/repo", "org/repo"),
            ("  Org  ", "Org"),
            ("https://github.com/org", "org"),
            ("https://github.com/org/terraform-*", "org/terraform-*"),
        ],
    )
    def test_github_forms(self, raw, path):
        assert targets.normalise(_github_conn(), raw) == path

    def test_github_enterprise_web_root_is_the_api_host(self):
        conn = _github_conn(server_url="https://ghe.example.com/api/v3")
        assert targets.normalise(conn, "https://ghe.example.com/org/r.git") == "org/r"
        assert targets.web_base(conn) == "https://ghe.example.com"
        with pytest.raises(targets.TargetError) as e:
            targets.normalise(conn, "https://github.com/org/r")
        assert e.value.status == 422

    def test_gitlab_relative_url_root_is_stripped(self):
        conn = _gitlab_conn("https://git.example.com/gitlab")
        assert targets.normalise(conn, "https://git.example.com/gitlab/g/sub/p") == "g/sub/p"
        assert targets.normalise(conn, "https://GIT.example.com/GitLab/g/p.git/") == "g/p"
        assert targets.normalise(conn, "git@git.example.com:g/p.git") == "g/p"
        assert targets.normalise(conn, "g/sub") == "g/sub"

    @pytest.mark.parametrize(
        "raw", ["", "   ", "https://gitlab.com/org/repo", "org//repo", "https://github.com/"]
    )
    def test_rejected(self, raw):
        with pytest.raises(targets.TargetError) as e:
            targets.normalise(_github_conn(), raw)
        assert e.value.status == 422


class TestGlobs:
    def test_the_last_segment_only(self):
        assert targets.split_glob("org/terraform-*") == ("org", "terraform-*")
        assert targets.split_glob("g/sub/mod-[ab]?") == ("g/sub", "mod-[ab]?")
        assert targets.split_glob("org/repo") == ("org/repo", "")
        for bad in ("org/*/x", "*/repo", "g/**/terraform-*"):
            with pytest.raises(targets.TargetError) as e:
                targets.split_glob(bad)
            assert e.value.status == 422 and "last segment" in e.value.detail

    def test_a_pattern_needs_a_namespace(self):
        with pytest.raises(targets.TargetError) as e:
            targets.split_glob("terraform-*")
        assert e.value.status == 422

    def test_a_repository_name_is_matched_ignoring_case(self):
        ref = github_service.repository_ref(_repo_json("org/Terraform-AWS-vpc", 1))
        assert targets.matches_glob(ref, "terraform-aws-*")
        assert not targets.matches_glob(ref, "terraform-azurerm-*")

    def test_url_keys_ignore_case_git_and_slashes(self):
        assert targets.url_key("https://GitHub.com/Org/Repo.git/") == targets.url_key(
            "https://github.com/org/repo"
        )


# ── GitHub ───────────────────────────────────────────────────────────────


class TestClassifyGitHub:
    async def test_owner_repo_is_one_repository_by_id(self):
        server, (p1, p2) = _github(
            {"/repos/org/terraform-aws-vpc": _resp(200, _repo_json("org/terraform-aws-vpc", 99))}
        )
        with p1, p2:
            target = await targets.classify(
                _github_conn(), "https://github.com/org/terraform-aws-vpc"
            )
        assert (target.kind, target.id, target.path) == (
            "repository",
            "99",
            "org/terraform-aws-vpc",
        )
        assert target.ref is not None and target.ref.url.endswith("/org/terraform-aws-vpc")

    async def test_a_missing_repository_is_422(self):
        _, (p1, p2) = _github({})
        with p1, p2, pytest.raises(targets.TargetError) as e:
            await targets.classify(_github_conn(), "org/gone")
        assert e.value.status == 422

    @pytest.mark.parametrize(
        "route", [_resp(500, {}), _resp(403, {}), httpx.ConnectError("refused")]
    )
    async def test_a_provider_outage_is_502(self, route):
        _, (p1, p2) = _github({"/repos/org/r": route})
        with p1, p2, pytest.raises(targets.TargetError) as e:
            await targets.classify(_github_conn(), "org/r")
        assert e.value.status == 502

    async def test_the_account_is_a_namespace(self):
        # The account is looked up by the login the connection records.
        server, (p1, p2) = _github({"/users/Org": _resp(200, {"login": "Org", "id": 5})})
        with p1, p2:
            target = await targets.classify(_github_conn(login="Org"), "https://github.com/org")
        assert (target.kind, target.id, target.path) == ("namespace", "5", "Org")

    async def test_a_pattern_over_the_account(self):
        _, (p1, p2) = _github({"/users/org": _resp(200, {"login": "org", "id": 5})})
        with p1, p2:
            target = await targets.classify(_github_conn(), "org/terraform-*")
        assert (target.kind, target.id, target.glob) == ("pattern", "5", "terraform-*")
        assert target.path == "org/terraform-*"

    @pytest.mark.parametrize("raw", ["other", "other/terraform-*"])
    async def test_another_account_is_422(self, raw):
        server, (p1, p2) = _github({"/users/other": _resp(200, {"login": "other", "id": 6})})
        with p1, p2, pytest.raises(targets.TargetError) as e:
            await targets.classify(_github_conn(login="org"), raw)
        assert e.value.status == 422 and "installed on 'org'" in e.value.detail
        assert server.calls == []

    async def test_three_segments_is_422(self):
        with pytest.raises(targets.TargetError) as e:
            await targets.classify(_github_conn(), "org/repo/extra")
        assert e.value.status == 422

    async def test_without_a_recorded_login_the_installations_account_is_used(self):
        page = _resp(200, {"repositories": [_repo_json("acct/anything", 1, owner_id=8)]})
        _, (p1, p2) = _github(
            {
                "/installation/repositories": page,
                "/users/acct": _resp(200, {"login": "acct", "id": 8}),
            }
        )
        with p1, p2:
            target = await targets.classify(_github_conn(login=""), "acct")
        assert (target.kind, target.id) == ("namespace", "8")


# ── GitLab ───────────────────────────────────────────────────────────────


class TestClassifyGitLab:
    async def test_a_project_is_tried_before_a_group(self):
        project = {
            "id": 31,
            "path_with_namespace": "g/sub/p",
            "web_url": "https://gitlab.example.com/g/sub/p",
            "namespace": {"id": 3, "full_path": "g/sub"},
        }
        server, p = _gitlab({"/projects/g%2Fsub%2Fp": _resp(200, project)})
        with p:
            target = await targets.classify(_gitlab_conn(), "https://gitlab.example.com/g/sub/p")
        assert (target.kind, target.id, target.path) == ("repository", "31", "g/sub/p")
        assert server.calls == ["/projects/g%2Fsub%2Fp"]

    async def test_a_path_that_is_not_a_project_is_a_group(self):
        group = {"id": 3, "full_path": "g/sub", "web_url": "https://gitlab.example.com/g/sub"}
        server, p = _gitlab({"/groups/g%2Fsub": _resp(200, group)})
        with p:
            target = await targets.classify(_gitlab_conn(), "g/sub")
        assert (target.kind, target.id, target.path) == ("namespace", "3", "g/sub")
        assert server.calls == ["/projects/g%2Fsub", "/groups/g%2Fsub"]

    async def test_one_segment_is_a_group_without_asking_for_a_project(self):
        server, p = _gitlab({"/groups/g": _resp(200, {"id": 2, "full_path": "g"})})
        with p:
            target = await targets.classify(_gitlab_conn(), "g")
        assert target.kind == "namespace" and server.calls == ["/groups/g"]

    async def test_a_glob_ranges_over_its_prefix_group(self):
        server, p = _gitlab({"/groups/g%2Fsub": _resp(200, {"id": 3, "full_path": "g/sub"})})
        with p:
            target = await targets.classify(_gitlab_conn(), "g/sub/terraform-*")
        assert (target.kind, target.id, target.glob) == ("pattern", "3", "terraform-*")
        assert server.calls == ["/groups/g%2Fsub"]

    async def test_a_user_namespace_is_422(self):
        _, p = _gitlab({"/namespaces/someone": _resp(200, {"kind": "user", "id": 9})})
        with p, pytest.raises(targets.TargetError) as e:
            await targets.classify(_gitlab_conn(), "someone")
        assert e.value.status == 422 and "user namespace" in e.value.detail

    async def test_nothing_at_all_is_422(self):
        _, p = _gitlab({})
        with p, pytest.raises(targets.TargetError) as e:
            await targets.classify(_gitlab_conn(), "g/nothing")
        assert e.value.status == 422

    async def test_a_group_lookup_outage_is_502(self):
        _, p = _gitlab({"/groups/g": _resp(503, {})})
        with p, pytest.raises(targets.TargetError) as e:
            await targets.classify(_gitlab_conn(), "g")
        assert e.value.status == 502

    async def test_the_relative_url_root_reaches_the_api(self):
        conn = _gitlab_conn("https://git.example.com/gitlab")
        server, p = _gitlab(
            {"/groups/g": _resp(200, {"id": 2, "full_path": "g"})},
            base="https://git.example.com/gitlab/api/v4",
        )
        with p:
            target = await targets.classify(conn, "https://git.example.com/gitlab/g")
        assert target.kind == "namespace"


def test_owner_repo_reads_a_bare_path():
    assert targets.owner_repo(_github_conn(), "org/repo") == ("org", "repo")
    assert targets.owner_repo(_gitlab_conn(), "g/sub/p") == ("g/sub", "p")
    assert targets.owner_repo(_github_conn(), "org") is None
    assert targets.owner_repo(_github_conn(), "org/terraform-*") is None


# ── Listings ─────────────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _pages(*pages):
    """An installation listing, page by page, with `Link: rel=next`."""

    def answer(params, headers):
        n = params["page"]
        link = (
            {"link": f'<https://api.github.com/installation/repositories?page={n + 1}>; rel="next"'}
            if n < len(pages)
            else {}
        )
        return _resp(200, {"repositories": pages[n - 1]}, {**link, "etag": f'"e{n}"'})

    return answer


class TestGitHubListing:
    async def test_pages_are_followed_and_forks_are_reported(self):
        answer = _pages(
            [_repo_json("org/a", 1), _repo_json("org/b", 2, fork=True)],
            [_repo_json("org/c", 3, disabled=True, archived=True, size=0)],
        )
        server, (p1, p2) = _github({"/installation/repositories": answer})
        with p1, p2:
            listing = await github_service.list_installation_repositories(
                _github_conn(), max_repositories=10
            )
        assert listing.complete and [r.path for r in listing.repositories] == [
            "org/a",
            "org/b",
            "org/c",
        ]
        a, b, c = listing.repositories
        assert b.fork and c.disabled and c.archived
        # size 0 is not emptiness: GitHub computes it asynchronously, and it
        # stays 0 for a while after a repository's first push.
        assert not c.empty and not a.empty
        assert a.change_marker == "2026-09-01T00:00:00Z" and a.created_at.year == 2025
        assert len(server.calls) == 2

    async def test_the_cap_makes_the_listing_incomplete(self):
        answer = _pages([_repo_json("org/a", 1), _repo_json("org/b", 2)], [_repo_json("org/c", 3)])
        _, (p1, p2) = _github({"/installation/repositories": answer})
        with p1, p2:
            listing = await github_service.list_installation_repositories(
                _github_conn(), max_repositories=2
            )
        assert not listing.complete and len(listing.repositories) == 2
        # Exactly the cap, and nothing after it: complete.
        answer = _pages([_repo_json("org/a", 1), _repo_json("org/b", 2)])
        _, (p1, p2) = _github({"/installation/repositories": answer})
        with p1, p2:
            listing = await github_service.list_installation_repositories(
                _github_conn(), max_repositories=2
            )
        assert listing.complete

    async def test_an_unchanged_page_is_served_from_its_etag(self):
        redis = _FakeRedis()
        conn = _github_conn()
        answer = _pages([_repo_json("org/a", 1, fork=True)])
        server, (p1, p2) = _github({"/installation/repositories": answer})
        with p1, p2, patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await github_service.list_installation_repositories(conn, max_repositories=10)
            server.routes["/installation/repositories"] = _resp(304, None)
            listing = await github_service.list_installation_repositories(conn, max_repositories=10)
        assert server.headers[-1]["If-None-Match"] == '"e1"'
        (ref,) = listing.repositories
        assert ref.path == "org/a" and ref.fork and ref.created_at.year == 2025

    async def test_a_listing_error_raises(self):
        _, (p1, p2) = _github({"/installation/repositories": _resp(502, {})})
        with p1, p2, pytest.raises(httpx.HTTPStatusError):
            await github_service.list_installation_repositories(_github_conn(), max_repositories=5)

    async def test_a_rule_sees_its_accounts_repositories_matching_its_glob(self):
        answer = _pages(
            [
                _repo_json("org/terraform-aws-b", 2),
                _repo_json("org/app", 3),
                _repo_json("org/terraform-aws-a", 1),
                _repo_json("other/terraform-aws-x", 4, owner_id=2),
            ]
        )
        _, (p1, p2) = _github({"/installation/repositories": answer})
        with p1, p2:
            listing = await targets.list_repositories(
                _github_conn(), "pattern", "1", "terraform-*", max_repositories=10
            )
        assert [r.path for r in listing.repositories] == [
            "org/terraform-aws-a",
            "org/terraform-aws-b",
        ]


def _project(path, pid, ns_id=3, **kw):
    data = {
        "id": pid,
        "path_with_namespace": path,
        "web_url": f"https://gitlab.example.com/{path}",
        "default_branch": "main",
        "namespace": {"id": ns_id, "full_path": path.rpartition("/")[0]},
        "archived": False,
        "empty_repo": False,
        "last_activity_at": "2026-09-01T00:00:00Z",
        "created_at": "2025-01-01T00:00:00Z",
    }
    data.update(kw)
    return data


class TestGitLabListing:
    async def test_keyset_pages_are_followed_by_link(self):
        seen_params = []
        next_url = "https://gitlab.example.com/api/v4/groups/3/projects?cursor=abc"

        def first(params, headers):
            seen_params.append(params)
            return _resp(
                200,
                [
                    _project("g/a", 1),
                    _project("g/sub/b", 2, ns_id=4, forked_from_project={"id": 9}),
                ],
                {"link": f'<{next_url}>; rel="next"'},
            )

        def second(params, headers):
            seen_params.append(params)
            return _resp(200, [_project("g/c", 3, empty_repo=True, default_branch=None)])

        server, p = _gitlab({"/groups/3/projects": first, "/groups/3/projects?cursor=abc": second})
        with p:
            listing = await gitlab_service.list_group_projects(
                _gitlab_conn(), "3", include_subgroups=True, max_repositories=10
            )
        assert listing.complete and [r.path for r in listing.repositories] == [
            "g/a",
            "g/sub/b",
            "g/c",
        ]
        assert seen_params[0]["pagination"] == "keyset" and seen_params[0]["order_by"] == "id"
        assert seen_params[0]["include_subgroups"] == "true"
        assert seen_params[0]["with_shared"] == "false"
        assert seen_params[1] == {}  # the next link carries its own query
        _a, b, c = listing.repositories
        assert b.fork and c.empty and c.default_branch == ""

    async def test_the_cap_and_a_gone_group(self):
        answer = _resp(200, [_project("g/a", 1), _project("g/b", 2)])
        _, p = _gitlab({"/groups/3/projects": answer})
        with p:
            listing = await gitlab_service.list_group_projects(
                _gitlab_conn(), "3", include_subgroups=False, max_repositories=1
            )
        assert not listing.complete and len(listing.repositories) == 1
        _, p = _gitlab({})
        with p:
            assert (
                await gitlab_service.list_group_projects(
                    _gitlab_conn(), "3", include_subgroups=True, max_repositories=5
                )
                is None
            )
            with pytest.raises(targets.TargetGone):
                await targets.list_repositories(
                    _gitlab_conn(), "namespace", "3", "", max_repositories=5
                )

    async def test_a_pattern_asks_for_direct_children_only(self):
        seen = []

        def answer(params, headers):
            seen.append(params["include_subgroups"])
            return _resp(200, [_project("g/terraform-a", 1), _project("g/app", 2)])

        _, p = _gitlab({"/groups/3/projects": answer})
        with p:
            listing = await targets.list_repositories(
                _gitlab_conn(), "pattern", "3", "terraform-*", max_repositories=5
            )
        assert seen == ["false"] and [r.path for r in listing.repositories] == ["g/terraform-a"]
