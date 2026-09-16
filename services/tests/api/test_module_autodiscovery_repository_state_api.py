"""The rules API keeps per-repository state in step (#1620).

A change that re-baselines a rule deletes its state rows along with its own
scan columns, and a scan writes the repository row as well as the rule.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from terrapod.db.models import ModuleAutodiscoveryRepository
from tests.api.test_module_autodiscovery_rules import _call, _FakeDB, _github, _saved_rule


class _RecordingDB(_FakeDB):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return await super().execute(stmt)


def _rule_with_state(**kw):
    rule = _saved_rule(**kw)
    rule.repositories = [
        ModuleAutodiscoveryRepository(repo_path="org/a", last_scanned_sha="s1"),
        ModuleAutodiscoveryRepository(repo_path="org/b", last_scanned_sha="s1"),
    ]
    return rule


def _deleted_state(db: _RecordingDB) -> list:
    return [o for o in db.deleted if isinstance(o, ModuleAutodiscoveryRepository)]


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestRepositoryState:
    async def test_a_rebaseline_deletes_the_rules_repository_state(self, *_):
        rule = _rule_with_state(
            first_scan_at=datetime.now(UTC), last_scanned_sha="s1", seen_subdirectories=["a"]
        )
        rows = list(rule.repositories)
        db = _RecordingDB(rule)
        body = {"data": {"attributes": {"pattern": "**"}}}
        assert (await _call(db, "PATCH", f"/{rule.id}", json=body)).status_code == 200
        assert _deleted_state(db) == rows

    async def test_a_rebaseline_deletes_through_the_orm_so_it_replicates(self, *_):
        """Regression (#1666). The rows replicate, and a Core `DELETE` never
        reaches the outbox: the follower would keep the old baseline, and a
        promoted node would skip the re-baseline the operator asked for."""
        rule = _rule_with_state(first_scan_at=datetime.now(UTC), last_scanned_sha="s1")
        db = _RecordingDB(rule)
        body = {"data": {"attributes": {"pattern": "**"}}}
        assert (await _call(db, "PATCH", f"/{rule.id}", json=body)).status_code == 200
        assert not any(s.startswith("DELETE FROM") for s in db.statements)

    async def test_a_change_that_keeps_the_baseline_keeps_the_state(self, *_):
        rule = _rule_with_state(first_scan_at=datetime.now(UTC), last_scanned_sha="s1")
        db = _RecordingDB(rule)
        body = {"data": {"attributes": {"name": "renamed", "provider": "aws"}}}
        assert (await _call(db, "PATCH", f"/{rule.id}", json=body)).status_code == 200
        assert not _deleted_state(db)

    async def test_a_scan_writes_the_repository_row(self, *_):
        rule = _saved_rule()
        db = _FakeDB(rule, registered=[("x", "azurerm", "modules/create")])
        with _github(sha="s7"):
            resp = await _call(db, "POST", f"/{rule.id}/scan")
        assert resp.status_code == 200, resp.text
        (row,) = rule.repositories
        assert row.last_scanned_sha == rule.last_scanned_sha == "s7"
        assert row.seen_subdirectories == rule.seen_subdirectories
        assert row.default_branch == "main"
        assert row.last_skips == [
            {"subdirectory": "modules/create", "reason": "already-registered"}
        ]
