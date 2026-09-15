"""Per-repository scan state for repository-target module rules (#1620).

A repository rule keeps exactly one state row, and keeps it in step with the
rule's own scan columns: those columns are what a replica on older code reads
and writes during a rolling upgrade, so whichever copy is further along wins.
The VCS provider is patched at `github_service`, beneath the service's own
helpers, so the real `resolve_head` / `list_files` run.
"""

from datetime import UTC, datetime

import pytest

from terrapod.db.models import ModuleAutodiscoveryRepository
from terrapod.services import module_autodiscovery_service as svc
from tests.services.test_module_autodiscovery_service import PATHS, _FakeDB, _github, _rule

_SEEN = ["", "modules/create", "modules/legacy", "modules/update"]


def _row(**kw):
    fields = {
        "repo_path": "org/terraform-azurerm-management-groups",
        "repo_url": "",
        "vcs_repo_id": "",
        "origin": "baseline",
        "status": "active",
        "last_scanned_sha": "",
        "seen_subdirectories": [],
        "candidates": [],
        "last_skips": [],
        "previous_paths": [],
    }
    fields.update(kw)
    return ModuleAutodiscoveryRepository(**fields)


class TestPathFromUrl:
    @pytest.mark.parametrize(
        "url,server,provider,path",
        [
            ("https://github.com/org/repo", "", "github", "org/repo"),
            ("https://github.com/org/repo.git", "", "github", "org/repo"),
            ("https://github.com/org/repo/", "", "github", "org/repo"),
            ("https://github.com/Org/Repo.GIT//", "", "github", "Org/Repo"),
            ("git@github.com:org/repo.git", "", "github", "org/repo"),
            ("https://gitlab.com/g/sub/p", "https://gitlab.com", "gitlab", "g/sub/p"),
            (
                "https://git.example.com/gitlab/g/sub/p.git",
                "https://git.example.com/gitlab",
                "gitlab",
                "g/sub/p",
            ),
            # A GitHub Enterprise API root is not a web path prefix.
            ("https://ghe.example.com/org/r", "https://ghe.example.com/api/v3", "github", "org/r"),
            ("org/repo", "", "github", "org/repo"),
        ],
    )
    def test_forms(self, url, server, provider, path):
        assert svc.path_from_url(url, server, provider) == path


class TestRepositoryState:
    def test_created_from_the_rules_own_columns(self):
        first = datetime(2026, 9, 1, tzinfo=UTC)
        rule = _rule(first_scan_at=first, last_scanned_sha="s1", seen_subdirectories=["", "a"])
        row = svc.repository_state(rule)
        assert list(rule.repositories) == [row]
        assert row.repo_path == "org/terraform-azurerm-management-groups"
        assert (row.origin, row.status) == ("baseline", "active")
        assert (row.last_scanned_sha, row.seen_subdirectories) == ("s1", ["", "a"])
        assert row.first_seen_at == first
        # Idempotent: the same row, not a second one.
        assert svc.repository_state(rule) is row and len(rule.repositories) == 1

    def test_an_older_replica_rebaselining_the_rule_resets_the_row_too(self):
        # The rule has no baseline, but its row remembers a scan: an older
        # replica reset the rule's columns and knows nothing of the row.
        rule = _rule(first_scan_at=None)
        row = _row(
            last_scanned_sha="s1",
            seen_subdirectories=["a"],
            candidates=[{"subdirectory": "a"}],
            last_skips=[{"subdirectory": "a", "reason": "name-taken"}],
        )
        rule.repositories.append(row)
        assert svc.repository_state(rule) is row
        assert (row.last_scanned_sha, row.seen_subdirectories, row.candidates) == ("", [], [])
        assert row.last_skips == []

    def test_a_baselined_rule_keeps_its_row_as_it_is(self):
        rule = _rule(first_scan_at=datetime.now(UTC), last_scanned_sha="s1")
        row = _row(last_scanned_sha="s1", seen_subdirectories=["a"])
        rule.repositories.append(row)
        svc.repository_state(rule)
        assert row.seen_subdirectories == ["a"]


class TestRecordScan:
    def test_writes_the_row_as_well_as_the_rule(self):
        rule = _rule()
        row = svc.repository_state(rule)
        svc.record_scan(rule, PATHS, "s9")
        assert row.last_scanned_sha == rule.last_scanned_sha == "s9"
        assert row.seen_subdirectories == rule.seen_subdirectories == _SEEN
        assert [c["subdirectory"] for c in row.candidates] == _SEEN
        assert row.candidates[1] == {
            "subdirectory": "modules/create",
            "name": "management-groups-create",
            "provider": "azurerm",
        }
        assert row.last_checked_at is not None

    def test_merges_what_either_copy_has_seen(self):
        rule = _rule(seen_subdirectories=["only-on-the-rule"])
        row = svc.repository_state(rule)
        row.seen_subdirectories = ["only-on-the-row"]
        svc.record_scan(rule, PATHS, "s9")
        assert {"only-on-the-rule", "only-on-the-row"} <= set(rule.seen_subdirectories)
        assert row.seen_subdirectories == rule.seen_subdirectories


class TestPoll:
    async def test_a_first_poll_writes_the_row_and_adds_nothing_else(self):
        rule = _rule()
        db = _FakeDB(rules=[rule])
        with _github(sha="s1", default_branch="trunk"):
            assert await svc.poll_rules(db) == 0
        (row,) = rule.repositories
        assert row.last_scanned_sha == rule.last_scanned_sha == "s1"
        assert row.seen_subdirectories == rule.seen_subdirectories == _SEEN
        assert row.default_branch == "trunk"
        # The row rides the rule's collection; no module, nothing else added.
        assert db.added == []

    async def test_a_head_an_older_replica_already_scanned_is_not_walked_again(self):
        # An older replica scanned s2 and wrote only the rule's columns.
        rule = _rule(
            first_scan_at=datetime.now(UTC), last_scanned_sha="s2", seen_subdirectories=_SEEN
        )
        rule.repositories.append(_row(last_scanned_sha="s1", seen_subdirectories=_SEEN))
        with _github(sha="s2") as tree:
            assert await svc.poll_rules(_FakeDB(rules=[rule])) == 0
        tree.assert_not_awaited()

    async def test_what_an_older_replica_saw_is_not_registered_again(self):
        # The older replica registered modules/new and recorded it on the rule;
        # the row never heard of it. The new code must not register it twice.
        rule = _rule(
            first_scan_at=datetime.now(UTC),
            last_scanned_sha="s2",
            seen_subdirectories=[*_SEEN, "modules/new"],
        )
        row = _row(last_scanned_sha="s1", seen_subdirectories=_SEEN)
        rule.repositories.append(row)
        db = _FakeDB(rules=[rule])
        with _github([*PATHS, "modules/new/main.tf"], sha="s3"):
            assert await svc.poll_rules(db) == 0
        assert db.added == []
        assert "modules/new" in row.seen_subdirectories and row.last_scanned_sha == "s3"

    async def test_skips_are_recorded_on_the_row(self):
        rule = _rule(
            first_scan_at=datetime.now(UTC), last_scanned_sha="s1", seen_subdirectories=_SEEN
        )
        db = _FakeDB(rules=[rule], taken={"management-groups-new"})
        with _github([*PATHS, "modules/new/main.tf"], sha="s2"):
            assert await svc.poll_rules(db) == 0
        assert rule.repositories[0].last_skips == [
            {"subdirectory": "modules/new", "reason": "name-taken"}
        ]
