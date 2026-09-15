"""Polling org-wide module autodiscovery rules (#1620).

GitHub is served by a small fake behind `github_service._github_request`, so
the real listing, branch-head and tree wrappers run, and so does the real
service. The database is a fake that answers each query by what it selects and
rolls a savepoint's additions back when it fails.
"""

import asyncio
import re
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.config import ModuleAutodiscoveryConfig, settings
from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    RegistryModule,
    VCSConnection,
)
from terrapod.services import github_service, vcs_rate_limit
from terrapod.services import module_autodiscovery_service as svc
from terrapod.services.vcs_rate_limit import RateLimitSnapshot

OLD = "2025-01-01T00:00:00Z"
FUTURE = "2030-01-01T00:00:00Z"
TREE = ["main.tf", "modules/a/main.tf", "examples/x/main.tf", "README.md"]
A, B, C = "org/terraform-aws-a", "org/terraform-aws-b", "org/terraform-aws-c"

CONN = VCSConnection(
    id=uuid.uuid4(),
    provider="github",
    name="gh",
    server_url="",
    token="k",
    status="active",
    github_installation_id=7,
    github_account_login="org",
)


def _resp(status, body=None):
    return httpx.Response(
        status, json=body, request=httpx.Request("GET", "https://api.github.com/x")
    )


class FakeGitHub:
    """An installation's repositories, their branch heads and file trees."""

    def __init__(self):
        self.repos: dict[str, dict] = {}
        self.heads: dict[str, str] = {}
        self.trees: dict[str, list[str]] = {}
        self.fail: dict[str, int] = {}
        self.calls: list[str] = []

    def add(self, path, rid, *, head="s1", tree=TREE, created=OLD, pushed="p1", **kw):
        owner = path.rpartition("/")[0]
        self.repos[path] = {
            "id": rid,
            "full_name": path,
            "html_url": f"https://github.com/{path}",
            "default_branch": "main",
            "owner": {"login": owner, "id": kw.pop("owner_id", 1)},
            "archived": False,
            "fork": False,
            "disabled": False,
            "size": 10,
            "pushed_at": pushed,
            "created_at": created,
            **kw,
        }
        self.heads[path] = head
        self.trees[path] = list(tree)

    def push(self, path, *, head, pushed, tree=None):
        self.heads[path] = head
        self.repos[path]["pushed_at"] = pushed
        if tree is not None:
            self.trees[path] = list(tree)

    def rename(self, old, new):
        data = self.repos.pop(old)
        data.update(full_name=new, html_url=f"https://github.com/{new}")
        self.repos[new] = data
        self.heads[new] = self.heads.pop(old)
        self.trees[new] = self.trees.pop(old)

    def remove(self, path):
        del self.repos[path]

    def trees_listed(self):
        return [
            c.split("/git/trees/")[0].removeprefix("/repos/")
            for c in self.calls
            if "/git/trees/" in c
        ]

    def branches_read(self):
        return [
            c.split("/branches/")[0].removeprefix("/repos/")
            for c in self.calls
            if "/branches/" in c
        ]

    async def request(self, method, url, token, *, conn, params=None, headers=None, **kw):
        path = url.removeprefix("https://api.github.com")
        self.calls.append(path)
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                return _resp(status, {})
        if path == "/installation/repositories":
            return _resp(200, {"repositories": [self.repos[p] for p in sorted(self.repos)]})
        bare = path.split("?")[0]
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/branches/(.+)", bare):
            key = f"{m[1]}/{m[2]}"
            if key not in self.repos or not self.heads.get(key):
                # GitHub answers 404 for a branch of an empty repository.
                return _resp(404, {})
            return _resp(200, {"commit": {"sha": self.heads[key]}})
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/git/trees/(.+)", bare):
            key = f"{m[1]}/{m[2]}"
            if key not in self.repos:
                return _resp(404, {})
            tree = [{"path": p, "type": "blob"} for p in self.trees[key]]
            return _resp(200, {"truncated": False, "tree": tree})
        if m := re.fullmatch(r"/repositories/(\d+)", bare):
            found = [r for r in self.repos.values() if str(r["id"]) == m[1]]
            return _resp(200, found[0]) if found else _resp(404, {})
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)", bare):
            key = f"{m[1]}/{m[2]}"
            return _resp(200, self.repos[key]) if key in self.repos else _resp(404, {})
        return _resp(404, {})


async def _token(conn):
    return "t"


@contextmanager
def _serve(gh: FakeGitHub):
    with (
        patch.object(github_service, "_github_request", new=gh.request),
        patch.object(github_service, "get_installation_token", new=_token),
    ):
        yield


class _DB:
    """The service's queries, answered by what each one selects."""

    def __init__(self, rules, *, registered=(), covered=(), fail_flushes=()):
        self.rules = list(rules)
        self.base = list(registered)  # (name, provider, subdirectory, vcs_repo_url)
        self.covered = list(covered)
        self.fail_flushes = set(fail_flushes)
        self.added: list = []
        self.flushes = 0
        self.refreshed: list = []

    def modules(self) -> list[RegistryModule]:
        return [m for m in self.added if isinstance(m, RegistryModule)]

    async def execute(self, stmt):
        sql = str(stmt)
        result = MagicMock()
        # The rules select joins vcs_connections too (the relationship is
        # joined-loaded); only the covered query filters on target_kind.
        if "module_autodiscovery_rules.target_kind =" in sql:
            result.all.return_value = self.covered
        elif "FROM module_autodiscovery_rules" in sql:
            result.scalars.return_value.all.return_value = self.rules
        elif "registry_modules.subdirectory" in sql:
            result.all.return_value = [
                *self.base,
                *((m.name, m.provider, m.subdirectory, m.vcs_repo_url) for m in self.modules()),
            ]
        else:
            result.all.return_value = [(r[0],) for r in self.base] + [
                (m.name,) for m in self.modules()
            ]
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        if self.flushes in self.fail_flushes:
            raise RuntimeError("value too long for type character varying(64)")

    async def refresh(self, obj):
        self.refreshed.append(obj)

    @asynccontextmanager
    async def _nested(self):
        mark = len(self.added)
        try:
            yield
        except BaseException:
            del self.added[mark:]
            raise

    def begin_nested(self):
        return self._nested()


def _rule(**kw):
    fields = {
        "id": uuid.uuid4(),
        "name": "org-rule",
        "vcs_connection_id": CONN.id,
        "repo_url": "org",
        "target_kind": "namespace",
        "target_id": "1",
        "branch": "",
        "pattern": "**/*.tf",
        "ignore_patterns": [],
        "name_template": "",
        "provider": "",
        "vcs_tag_pattern": "v*",
        "labels": {},
        "owner_email": None,
        "enabled": True,
        "first_scan_at": None,
        "last_scanned_sha": "",
        "seen_subdirectories": [],
        "last_error": "",
    }
    fields.update(kw)
    rule = ModuleAutodiscoveryRule(**fields)
    rule.vcs_connection = CONN
    return rule


def _rows(rule) -> dict[str, ModuleAutodiscoveryRepository]:
    return {r.repo_path: r for r in rule.repositories}


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    cfg = ModuleAutodiscoveryConfig()
    monkeypatch.setattr(settings.registry, "module_autodiscovery", cfg)
    return cfg


async def _poll(db, gh) -> int:
    with _serve(gh):
        return await svc.poll_rules(db)


# ── Baselines ─────────────────────────────────────────────────────────────


class TestBaselines:
    async def test_the_first_listing_takes_a_baseline_and_registers_nothing(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        rule = _rule()
        db = _DB([rule])
        assert await _poll(db, gh) == 0
        rows = _rows(rule)
        assert set(rows) == {A, B}
        for row in rows.values():
            assert (row.origin, row.status, row.last_scanned_sha) == ("baseline", "active", "s1")
            assert row.seen_subdirectories == ["", "modules/a"]
            assert row.default_branch == "main" and row.change_marker == "p1"
        assert rows[A].candidates == [
            {"subdirectory": "", "name": "a", "provider": "aws"},
            {"subdirectory": "modules/a", "name": "a-a", "provider": "aws"},
        ]
        assert db.modules() == []
        assert rule.first_scan_at is not None and rule.last_enumerated_at is not None
        assert rule.last_error == "" and rule.last_scanned_sha == ""

    async def test_a_repository_created_after_the_baseline_registers_everything(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule(labels={"team": "platform"})
        db = _DB([rule])
        await _poll(db, gh)
        gh.add("org/terraform-aws-new", 3, created=FUTURE)
        assert await _poll(db, gh) == 2
        row = _rows(rule)["org/terraform-aws-new"]
        assert row.origin == "new"
        by_sub = {m.subdirectory: m for m in db.modules()}
        assert set(by_sub) == {"", "modules/a"}
        assert {m.name for m in by_sub.values()} == {"new", "new-a"}
        m = by_sub["modules/a"]
        assert (m.provider, m.vcs_repo_url) == ("aws", "https://github.com/org/terraform-aws-new")
        assert m.module_autodiscovery_rule_id == rule.id and m.labels == {"team": "platform"}

    async def test_an_older_repository_entering_scope_is_a_baseline(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        # Added to the App's selection, or transferred in: it already existed.
        gh.add(B, 2, created=OLD)
        assert await _poll(db, gh) == 0
        assert _rows(rule)[B].origin == "baseline" and db.modules() == []
        # After its baseline, a directory that appears registers.
        gh.push(B, head="s2", pushed="p2", tree=[*TREE, "modules/c/main.tf"])
        assert await _poll(db, gh) == 1
        (m,) = db.modules()
        assert (m.subdirectory, m.vcs_repo_url) == ("modules/c", f"https://github.com/{B}")

    async def test_a_new_repository_github_still_sizes_at_zero_registers_its_modules(self):
        """Found live: GitHub reports `size` 0 for a while after the first push.

        The repository has content and a branch head; reading 0 as empty left
        it marked empty until its next push, so its modules never registered.
        """
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.add(C, 3, created=FUTURE, size=0, tree=TREE)
        assert await _poll(db, gh) == 2
        assert _rows(rule)[C].status == "active"
        assert {m.subdirectory for m in db.modules()} == {"", "modules/a"}

    async def test_an_empty_new_repository_registers_its_modules_as_they_arrive(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        # Truly empty: no commit, so no branch head (and size 0).
        gh.add(C, 3, created=FUTURE, size=0, head="", tree=[])
        gh.calls.clear()
        assert await _poll(db, gh) == 0
        assert _rows(rule)[C].status == "no-branch" and db.modules() == []
        gh.push(C, head="s2", pushed="p2", tree=TREE)
        assert await _poll(db, gh) == 2
        assert {m.subdirectory for m in db.modules()} == {"", "modules/a"}


# ── Cost ──────────────────────────────────────────────────────────────────


class TestCost:
    async def test_an_unchanged_marker_costs_only_the_listing(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.calls.clear()
        await _poll(db, gh)
        assert gh.calls == ["/installation/repositories"]

    async def test_a_moved_marker_with_the_same_head_lists_no_tree(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.repos[A]["pushed_at"] = "p2"  # a push to another branch
        gh.calls.clear()
        await _poll(db, gh)
        assert gh.branches_read() == [A] and gh.trees_listed() == []
        assert _rows(rule)[A].change_marker == "p2"

    async def test_tree_listings_are_capped_and_taken_round_robin(self, limits):
        limits.tree_listings_per_cycle = 2
        gh = FakeGitHub()
        paths = [f"org/terraform-aws-{c}" for c in "abcde"]
        for i, path in enumerate(paths):
            gh.add(path, i + 1)
        rule = _rule()
        db = _DB([rule])
        cycles = []
        for _ in range(3):
            gh.calls.clear()
            await _poll(db, gh)
            cycles.append(gh.trees_listed())
        assert cycles == [paths[:2], paths[2:4], paths[4:]]

    async def test_below_the_tree_floor_it_lists_but_reads_no_repository(self, monkeypatch):
        snapshot = RateLimitSnapshot(
            5000, 500, int(time.time()) + 3600, int(time.time()), "core", 3600
        )
        monkeypatch.setattr(vcs_rate_limit, "get_snapshot", AsyncMock(return_value=snapshot))
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        await _poll(_DB([rule]), gh)
        assert gh.calls == ["/installation/repositories"]
        assert A in _rows(rule) and "20% floor" in rule.last_error

    async def test_below_the_enumeration_floor_it_does_nothing(self, monkeypatch):
        snapshot = RateLimitSnapshot(
            5000, 100, int(time.time()) + 3600, int(time.time()), "core", 3600
        )
        monkeypatch.setattr(vcs_rate_limit, "get_snapshot", AsyncMock(return_value=snapshot))
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        await _poll(_DB([rule]), gh)
        assert gh.calls == [] and list(rule.repositories) == []
        assert "5% floor" in rule.last_error

    async def test_a_snapshot_from_before_the_budget_refilled_is_ignored(self, monkeypatch):
        stale = RateLimitSnapshot(
            5000, 0, int(time.time()) - 10, int(time.time()) - 600, "core", 3600
        )
        monkeypatch.setattr(vcs_rate_limit, "get_snapshot", AsyncMock(return_value=stale))
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        await _poll(_DB([rule]), gh)
        assert gh.trees_listed() == [A]

    async def test_the_time_budget_stops_the_cycle(self, monkeypatch, limits):
        class Clock:
            t = 0.0

            def monotonic(self):
                self.t += 4
                return self.t

            def time(self):
                return time.time()

        limits.time_budget_seconds = 10
        monkeypatch.setattr(svc, "time", Clock())
        gh = FakeGitHub()
        for i, c in enumerate("abc"):
            gh.add(f"org/terraform-aws-{c}", i + 1)
        rule = _rule()
        await _poll(_DB([rule]), gh)
        # Deadline 4+10; before listing 8; first repository 12; second 16 — spent.
        assert gh.trees_listed() == ["org/terraform-aws-a"]

    async def test_large_trees_are_matched_off_the_event_loop(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        with patch.object(svc.asyncio, "to_thread", wraps=asyncio.to_thread) as spy:
            await _poll(_DB([_rule()]), gh)
        assert spy.call_args.args[0] is svc._candidates


class TestBackoff:
    async def test_a_failing_repository_is_backed_off_and_the_rest_go_on(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        gh.fail[f"/repos/{A}/branches"] = 500
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        a, b = _rows(rule)[A], _rows(rule)[B]
        assert (a.status, a.failure_count) == ("error", 1) and "500" in a.last_error
        assert a.next_check_at > datetime.now(UTC) + timedelta(seconds=250)
        assert b.last_scanned_sha == "s1"

        gh.calls.clear()
        await _poll(db, gh)
        assert A not in gh.branches_read()  # still backing off

        a.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        gh.fail.clear()
        await _poll(db, gh)
        assert (a.status, a.failure_count, a.last_scanned_sha) == ("active", 0, "s1")

    async def test_the_backoff_doubles(self):
        assert svc._backoff(1) == timedelta(seconds=300)
        assert svc._backoff(2) == timedelta(seconds=600)
        assert svc._backoff(30) == timedelta(hours=6)

    async def test_a_database_error_backs_one_repository_off_and_keeps_the_rest(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        rule = _rule()
        # Flush 1 follows the listing; 2 records A; 3 records B.
        db = _DB([rule], fail_flushes={2})
        await _poll(db, gh)
        a, b = _rows(rule)[A], _rows(rule)[B]
        assert a.status == "error" and "could not record the scan" in a.last_error
        assert a in db.refreshed
        assert b.status == "active" and b.last_scanned_sha == "s1"


# ── Scope ─────────────────────────────────────────────────────────────────


class TestScope:
    async def test_forks_and_disabled_repositories_are_out_of_scope(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add("org/terraform-aws-fork", 2, fork=True, created=FUTURE)
        gh.add("org/terraform-aws-off", 3, disabled=True)
        rule = _rule()
        await _poll(_DB([rule]), gh)
        assert set(_rows(rule)) == {A}

    async def test_an_archived_repository_keeps_its_state_and_is_not_read(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.repos[A]["archived"] = True
        gh.push(A, head="s2", pushed="p2")
        gh.calls.clear()
        await _poll(db, gh)
        row = _rows(rule)[A]
        assert row.status == "archived" and row.seen_subdirectories == ["", "modules/a"]
        assert gh.branches_read() == []
        gh.repos[A]["archived"] = False
        await _poll(db, gh)
        assert row.status == "active" and row.last_scanned_sha == "s2"

    async def test_a_repository_gone_from_a_complete_listing_is_out_of_scope(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2, created=FUTURE)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.remove(A)
        await _poll(db, gh)
        assert _rows(rule)[A].status == "out-of-scope"
        assert _rows(rule)[B].status == "active"

    async def test_an_incomplete_listing_marks_nothing_out_of_scope(self, limits):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        enumerated = rule.last_enumerated_at
        limits.max_repositories = 1  # the listing now stops before B
        await _poll(db, gh)
        assert _rows(rule)[B].status == "active"
        assert "stopped at 1" in rule.last_error
        assert rule.last_enumerated_at == enumerated

    async def test_a_repository_a_single_repository_rule_names_is_covered(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add(B, 2)
        rule = _rule()
        covered = [(CONN.id, f"https://github.com/{A}.git", "", "", "github")]
        await _poll(_DB([rule], covered=covered), gh)
        assert _rows(rule)[A].status == "covered" and A not in gh.branches_read()
        assert _rows(rule)[B].status == "active"

    async def test_covered_matches_by_id_too(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        covered = [(CONN.id, "https://github.com/org/old-name", "1", "", "github")]
        await _poll(_DB([rule], covered=covered), gh)
        assert _rows(rule)[A].status == "covered"

    async def test_a_pattern_rule_keeps_to_its_glob(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add("org/app", 2)
        rule = _rule(repo_url="org/terraform-aws-*", target_kind="pattern")
        await _poll(_DB([rule]), gh)
        assert set(_rows(rule)) == {A}


class TestRenames:
    async def test_a_renamed_repository_is_followed_and_nothing_is_registered_twice(self):
        old, new = "org/terraform-aws-old", "org/terraform-aws-renamed"
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.add(old, 5, created=FUTURE)
        assert await _poll(db, gh) == 2  # registered under the old URL

        gh.rename(old, new)
        gh.push(new, head="s2", pushed="p2", tree=[*TREE, "modules/b/main.tf"])
        assert await _poll(db, gh) == 1
        rows = _rows(rule)
        assert old not in rows and rows[new].vcs_repo_id == "5"
        assert rows[new].previous_paths == [{"path": old, "url": f"https://github.com/{old}"}]
        new_modules = [m for m in db.modules() if m.vcs_repo_url.endswith("renamed")]
        assert [m.subdirectory for m in new_modules] == ["modules/b"]

    async def test_a_different_repository_at_an_old_path_starts_the_row_afresh(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.remove(A)
        gh.add(A, 9, created=FUTURE)  # deleted and recreated under the same name
        assert await _poll(db, gh) == 2
        row = _rows(rule)[A]
        assert (row.vcs_repo_id, row.origin) == ("9", "new")


class TestNaming:
    async def test_clashing_names_across_repositories_skip_every_candidate(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule(name_template="{leaf}")
        db = _DB([rule])
        await _poll(db, gh)
        gh.add("org/terraform-aws-x", 3, created=FUTURE)
        gh.add("org/terraform-aws-y", 4, created=FUTURE)
        assert await _poll(db, gh) == 0
        for path in ("org/terraform-aws-x", "org/terraform-aws-y"):
            skips = {s["subdirectory"]: s["reason"] for s in _rows(rule)[path].last_skips}
            assert skips == {"": "name-taken", "modules/a": "name-taken"}

    async def test_a_repository_without_a_provider_is_skipped_not_guessed(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        rule = _rule()
        db = _DB([rule])
        await _poll(db, gh)
        gh.add("org/platform", 3, created=FUTURE)
        assert await _poll(db, gh) == 0
        assert {s["reason"] for s in _rows(rule)["org/platform"].last_skips} == {"missing-provider"}

    def test_the_owner_placeholder(self):
        rule = _rule(name_template="{owner}-{repo}-{leaf}")
        ctx = svc.RepoContext(
            "https://gitlab.example.com/g/sub/terraform-aws-x", "g/sub/terraform-aws-x"
        )
        assert svc.derive_name(rule, "modules/a", ctx) == "g-sub-x-a"
        repo_rule = _rule(
            repo_url="https://github.com/org/terraform-aws-x", name_template="{owner}"
        )
        assert svc.derive_name(repo_rule, "") == "org"


class TestUrlNormalisation:
    async def test_a_module_registered_at_a_differently_written_url_counts(self):
        ctx = svc.RepoContext("https://github.com/org/terraform-aws-x", "org/terraform-aws-x")
        db = _DB([], registered=[("x", "aws", "", "https://GitHub.com/Org/Terraform-AWS-x.git/")])
        entries = await svc.preview(db, _rule(), ["main.tf", "modules/a/main.tf"], repo=ctx)
        by_sub = {e["subdirectory"]: e for e in entries}
        assert by_sub[""]["registered-as"] == {"name": "x", "provider": "aws"}
        assert by_sub["modules/a"]["registered-as"] is None
        assert by_sub[""]["repository"] == "org/terraform-aws-x"
        assert by_sub[""]["repo-url"] == "https://github.com/org/terraform-aws-x"

    async def test_a_renamed_repositorys_old_url_still_counts(self):
        ctx = svc.RepoContext(
            "https://github.com/org/new", "org/new", ("https://github.com/org/old",)
        )
        db = _DB([], registered=[("old", "aws", "modules/a", "https://github.com/org/old")])
        (entry,) = await svc.preview(db, _rule(provider="aws"), ["modules/a/main.tf"], repo=ctx)
        assert entry["registered-as"] == {"name": "old", "provider": "aws"}


# ── Single-repository rules: renames and the classification ─────────────


def _repo_rule(**kw):
    fields = {
        "repo_url": "https://github.com/org/terraform-aws-old",
        "target_kind": "repository",
        "target_id": "5",
        "first_scan_at": datetime.now(UTC),
        "last_scanned_sha": "s1",
        "seen_subdirectories": ["", "modules/a"],
    }
    fields.update(kw)
    return _rule(**fields)


class TestSingleRepositoryRules:
    async def test_a_renamed_repository_is_followed_by_its_id(self):
        gh = FakeGitHub()
        gh.add("org/terraform-aws-renamed", 5, head="s2", tree=[*TREE, "modules/b/main.tf"])
        rule = _repo_rule()
        db = _DB([rule])
        assert await _poll(db, gh) == 1
        (m,) = db.modules()
        assert (m.subdirectory, m.vcs_repo_url) == (
            "modules/b",
            "https://github.com/org/terraform-aws-renamed",
        )
        (row,) = rule.repositories
        assert row.repo_path == "org/terraform-aws-renamed"
        assert row.previous_paths == [
            {"path": "org/terraform-aws-old", "url": "https://github.com/org/terraform-aws-old"}
        ]
        # Stored as entered; the rename is reported, not rewritten.
        assert rule.repo_url == "https://github.com/org/terraform-aws-old"
        assert rule.last_error == "" and rule.last_scanned_sha == "s2"

        gh.calls.clear()
        await _poll(db, gh)
        assert not any(c.startswith("/repositories/") for c in gh.calls)

    async def test_a_deleted_repository_puts_the_rule_in_error_and_nothing_else(self):
        gh = FakeGitHub()
        gh.add("org/unrelated", 8)
        rule = _repo_rule()
        db = _DB([rule])
        assert await _poll(db, gh) == 0
        assert "no longer exists" in rule.last_error
        # Never re-classified by the poller: still the one repository it was.
        assert (rule.target_kind, rule.target_id) == ("repository", "5")
        assert rule.last_scanned_sha == "s1" and db.modules() == []
        assert not any(c.startswith(("/users/", "/installation/")) for c in gh.calls)

    async def test_a_gitlab_project_replaced_by_a_group_does_not_widen_the_rule(self):
        # The project was deleted and a group now lives at its path. Asking
        # "what is this path now?" would say namespace, and silently widen a
        # one-repository rule to a whole group. The poller never asks.
        conn = VCSConnection(
            id=uuid.uuid4(),
            provider="gitlab",
            name="gl",
            server_url="https://gitlab.example.com",
            token="t",
            status="active",
        )
        rule = _repo_rule(repo_url="https://gitlab.example.com/g/sub", target_id="31")
        rule.vcs_connection = conn
        calls = []

        async def gitlab(method, url, conn, **kw):
            path = url.removeprefix("https://gitlab.example.com/api/v4")
            calls.append(path)
            if path == "/groups/g%2Fsub":
                return _resp(200, {"id": 3, "full_path": "g/sub"})
            return _resp(404, {})

        with patch("terrapod.services.gitlab_service._gitlab_request", new=gitlab):
            assert await svc.poll_rules(_DB([rule])) == 0
        assert (rule.target_kind, rule.target_id) == ("repository", "31")
        assert "no longer exists" in rule.last_error
        assert not any(p.startswith("/groups/") for p in calls)

    async def test_a_rule_without_an_id_just_reports_the_error(self):
        rule = _repo_rule(target_id="")
        assert await _poll(_DB([rule]), FakeGitHub()) == 0
        assert rule.last_error and rule.target_kind == "repository"

    async def test_single_repository_rules_poll_before_org_wide_ones(self):
        gh = FakeGitHub()
        gh.add(A, 1)
        gh.add("org/terraform-aws-old", 5)
        org, single = _rule(name="org"), _repo_rule(name="single", target_id="")
        order = []
        real_single, real_org = svc._poll_rule, svc._poll_namespace_rule

        async def single_spy(db, rule, conn):
            order.append(rule.name)
            return await real_single(db, rule, conn)

        async def org_spy(db, rule, *a):
            order.append(rule.name)
            return await real_org(db, rule, *a)

        with (
            patch.object(svc, "_poll_rule", single_spy),
            patch.object(svc, "_poll_namespace_rule", org_spy),
        ):
            await _poll(_DB([org, single]), gh)
        assert order == ["single", "org"]
