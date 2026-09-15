"""Replication of module autodiscovery (#1666).

A module that an autodiscovery rule registered carries
`registry_modules.module_autodiscovery_rule_id`, an enforced foreign key. While
the rules were not replicated, a follower could not insert such a module at all:
the row was deferred on every backfill, the registry class never completed, and
a promoted node was missing both the rule and every module it had found.

The per-repository state (#1620) replicates alongside the rule because it is
the poller's memory. A promoted node that loses it re-baselines every
repository, and automatic registration then takes every candidate an operator
had deliberately left unregistered.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from terrapod.db.models import ModuleAutodiscoveryRepository, ModuleAutodiscoveryRule
from terrapod.services import replication, replication_registry, replication_sync

RULES = replication_registry.MODULE_AUTODISCOVERY_RULES
REPOS = replication_registry.MODULE_AUTODISCOVERY_REPOSITORIES
MODULES = replication_registry.REGISTRY_MODULES

CONN_ID = "11111111-1111-1111-1111-111111111111"
RULE_ID = "44444444-4444-4444-4444-444444444444"
REPO_ID = "55555555-5555-5555-5555-555555555555"
MODULE_ID = "66666666-6666-6666-6666-666666666666"
OTHER_ID = "77777777-7777-7777-7777-777777777777"


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _keys_db(keys):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = keys
    db.execute.return_value = result
    return db


def _stamps():
    now = datetime.now(UTC)
    return {"created_at": now, "updated_at": now}


def _rule(rule_id=RULE_ID, **kw):
    base = {
        "id": rule_id,
        "vcs_connection_id": CONN_ID,
        "repo_url": "example-org",
        "branch": "",
        "pattern": "**/*.tf",
        "ignore_patterns": ["examples/**"],
        "name": "org-modules",
        "enabled": True,
        "name_template": "{repo}-{leaf}",
        "provider": "",
        "vcs_tag_pattern": "v*",
        "labels": {"team": "platform"},
        "owner_email": None,
        "first_scan_at": datetime(2026, 9, 1, tzinfo=UTC),
        "last_scanned_sha": "",
        "seen_subdirectories": [],
        "target_kind": "namespace",
        "target_id": "4242",
        "last_enumerated_at": datetime(2026, 9, 14, tzinfo=UTC),
        "last_error": "",
        **_stamps(),
    }
    base.update(kw)
    return ModuleAutodiscoveryRule(**base)


def _repo(repo_id=REPO_ID, **kw):
    base = {
        "id": repo_id,
        "rule_id": RULE_ID,
        "repo_path": "example-org/terraform-aws-vpc",
        "repo_url": "https://github.com/example-org/terraform-aws-vpc",
        "vcs_repo_id": "9001",
        "default_branch": "main",
        "origin": "baseline",
        "status": "active",
        "change_marker": "2026-09-14T10:00:00Z",
        "last_scanned_sha": "abc123",
        "seen_subdirectories": ["", "modules/subnets", "modules/declined"],
        "candidates": [{"subdirectory": "", "name": "vpc", "provider": "aws"}],
        "last_skips": [{"subdirectory": "modules/declined", "reason": "not-selected"}],
        "previous_paths": [],
        "first_seen_at": datetime(2026, 9, 1, tzinfo=UTC),
        "last_checked_at": datetime(2026, 9, 14, 10, tzinfo=UTC),
        "next_check_at": datetime(2026, 9, 14, 11, tzinfo=UTC),
        "failure_count": 2,
        "last_error": "rate limited",
        **_stamps(),
    }
    base.update(kw)
    return ModuleAutodiscoveryRepository(**base)


def _module_payload(**kw):
    payload = {
        "id": MODULE_ID,
        "namespace": "default",
        "name": "vpc",
        "provider": "aws",
        "status": "active",
        "source": "vcs",
        "vcs_connection_id": CONN_ID,
        "vcs_repo_url": "https://github.com/example-org/terraform-aws-vpc",
        "vcs_tag_pattern": "v*",
        "module_autodiscovery_rule_id": RULE_ID,
        "created_at": "2026-09-14T10:00:00Z",
        "updated_at": "2026-09-14T10:00:00Z",
    }
    payload.update(kw)
    return payload


class TestModuleAutodiscoveryRules:
    @pytest.mark.replication_matrix("module_autodiscovery_rules", "backfill-from-empty")
    async def test_backfill_carries_the_match_the_target_and_the_template(self):
        """A second node added to a running install has no deltas to carry the
        rules, so backfill is how it gets them at all."""
        db = _rows_db([_rule()])

        page = await replication.read_backfill(db, RULES)

        assert page[0]["pattern"] == "**/*.tf"
        assert page[0]["ignore_patterns"] == ["examples/**"]
        assert page[0]["vcs_connection_id"] == CONN_ID
        assert (page[0]["target_kind"], page[0]["target_id"]) == ("namespace", "4242")
        assert page[0]["name_template"] == "{repo}-{leaf}"
        assert page[0]["first_scan_at"] == "2026-09-01T00:00:00Z"

    @pytest.mark.replication_matrix("module_autodiscovery_rules", "delta-apply")
    async def test_disabling_a_rule_reaches_the_peer(self):
        """A rule still enabled on a promoted node goes on registering modules
        from a repository the operator switched off."""
        db = AsyncMock()
        existing = _rule(enabled=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, RULES, {"id": RULE_ID, "enabled": False})

        assert existing.enabled is False

    @pytest.mark.replication_matrix("module_autodiscovery_rules", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _rule()
        db.scalar.return_value = existing
        payload = replication.serialize_row(RULES, existing)

        await replication.apply_upsert(db, RULES, payload)
        await replication.apply_upsert(db, RULES, payload)

        assert existing.pattern == "**/*.tf"
        assert existing.target_kind == "namespace"
        assert existing.first_scan_at == datetime(2026, 9, 1, tzinfo=UTC)

    @pytest.mark.replication_matrix("module_autodiscovery_rules", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, RULES, RULE_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("module_autodiscovery_rules", "backfill-converges-deletion")
    async def test_a_deleted_rule_does_not_survive_a_backfill(self):
        """A rule that comes back to life resumes registering modules from a
        repository somebody deliberately stopped watching."""
        db = _keys_db([(RULE_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, RULES, {RULE_ID})

        assert removed == [OTHER_ID]

    def test_nothing_is_excluded(self):
        """The polling columns look node-local and are not — see the comment
        above the registration."""
        assert RULES.exclude == frozenset()


class TestModuleAutodiscoveryRepositories:
    @pytest.mark.replication_matrix("module_autodiscovery_repositories", "backfill-from-empty")
    async def test_backfill_carries_the_baseline_and_the_poller_memory(self):
        db = _rows_db([_repo()])

        page = await replication.read_backfill(db, REPOS)

        row = page[0]
        assert row["rule_id"] == RULE_ID
        assert row["origin"] == "baseline"
        assert row["seen_subdirectories"] == ["", "modules/subnets", "modules/declined"]
        assert row["last_scanned_sha"] == "abc123"
        assert row["change_marker"] == "2026-09-14T10:00:00Z"
        assert row["candidates"] == [{"subdirectory": "", "name": "vpc", "provider": "aws"}]

    @pytest.mark.replication_matrix("module_autodiscovery_repositories", "delta-apply")
    async def test_a_new_scan_cursor_reaches_the_peer(self):
        """A promoted node on the old cursor re-scans the repository; one that
        lost `seen_subdirectories` registers what the operator declined."""
        db = AsyncMock()
        existing = _repo(last_scanned_sha="abc123", seen_subdirectories=[""])
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db,
            REPOS,
            {"id": REPO_ID, "last_scanned_sha": "def456", "seen_subdirectories": ["", "x"]},
        )

        assert existing.last_scanned_sha == "def456"
        assert existing.seen_subdirectories == ["", "x"]

    @pytest.mark.replication_matrix("module_autodiscovery_repositories", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _repo()
        db.scalar.return_value = existing
        payload = replication.serialize_row(REPOS, existing)

        await replication.apply_upsert(db, REPOS, payload)
        await replication.apply_upsert(db, REPOS, payload)

        assert existing.origin == "baseline"
        assert existing.failure_count == 2
        assert existing.next_check_at == datetime(2026, 9, 14, 11, tzinfo=UTC)

    @pytest.mark.replication_matrix("module_autodiscovery_repositories", "delete")
    async def test_delete_applies(self):
        """A re-baseline deletes a rule's rows through the ORM so this path
        runs; a Core DELETE would never have reached the outbox."""
        db = AsyncMock()

        await replication.apply_delete(db, REPOS, REPO_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix(
        "module_autodiscovery_repositories", "backfill-converges-deletion"
    )
    async def test_a_deleted_row_does_not_survive_a_backfill(self):
        """A stale row on the follower is an old baseline, which a promoted
        node would scan against instead of re-baselining."""
        db = _keys_db([(REPO_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, REPOS, {REPO_ID})

        assert removed == [OTHER_ID]

    async def test_the_backoff_survives_the_round_trip(self):
        """Carried on purpose: without it every repository is due at once after
        a failover, and one that was backing off is hammered again."""
        db = AsyncMock()
        target = _repo(next_check_at=None, failure_count=0, last_checked_at=None)
        db.scalar.return_value = target

        await replication.apply_upsert(db, REPOS, replication.serialize_row(REPOS, _repo()))

        assert target.next_check_at == datetime(2026, 9, 14, 11, tzinfo=UTC)
        assert target.last_checked_at == datetime(2026, 9, 14, 10, tzinfo=UTC)
        assert target.failure_count == 2
        assert target.last_error == "rate limited"

    def test_nothing_is_excluded(self):
        assert REPOS.exclude == frozenset()


class TestOrdering:
    def test_rules_come_after_their_connection_and_before_the_modules(self):
        order = list(replication.registered())

        for earlier, later in (
            ("vcs_connections", "module_autodiscovery_rules"),
            ("module_autodiscovery_rules", "module_autodiscovery_repositories"),
            ("module_autodiscovery_rules", "registry_modules"),
        ):
            assert order.index(earlier) < order.index(later), f"{earlier} must precede {later}"

    def test_the_foreign_keys_are_the_ones_the_order_answers(self):
        """If either model gains a foreign key, this says so rather than the
        generic ordering check reporting it at one remove."""

        def targets(model):
            return {fk.column.table.name for fk in sa_inspect(model).local_table.foreign_keys}

        assert targets(ModuleAutodiscoveryRule) == {"vcs_connections"}
        assert targets(ModuleAutodiscoveryRepository) == {"module_autodiscovery_rules"}
        assert "module_autodiscovery_rules" in targets(MODULES.model)


class _FKEnforcingDB:
    """A fresh follower that enforces foreign keys the way Postgres would.

    Each savepoint flushes what was added in it and checks every foreign key
    column against the rows already written, raising `IntegrityError` for a
    missing target — the failure a real follower hit.
    """

    def __init__(self, seeded: dict[str, set[uuid.UUID]]):
        self.rows = {table: set(ids) for table, ids in seeded.items()}
        self._pending: list = []

    async def scalar(self, _stmt):
        return None  # nothing here yet: every apply is an insert

    def add(self, obj):
        self._pending.append(obj)

    @asynccontextmanager
    async def begin_nested(self):
        self._pending = []
        yield
        for obj in self._pending:
            table = sa_inspect(type(obj)).local_table
            for fk in table.foreign_keys:
                value = getattr(obj, fk.parent.key)
                if value is not None and value not in self.rows.get(fk.column.table.name, set()):
                    raise IntegrityError(
                        f"insert into {table.name} violates {fk.parent.key}", None, Exception()
                    )
            self.rows.setdefault(table.name, set()).add(obj.id)


class TestADiscoveredModuleLandsOnTheFollower:
    """Drives backfill's own per-row apply (`_try_upsert`) against a follower
    that enforces foreign keys, in the order backfill walks the registry."""

    def _backfill_set(self):
        return {
            "module_autodiscovery_rules": replication.serialize_row(RULES, _rule()),
            "module_autodiscovery_repositories": replication.serialize_row(REPOS, _repo()),
            "registry_modules": _module_payload(),
        }

    async def test_in_registry_order_every_row_applies(self):
        db = _FKEnforcingDB({"vcs_connections": {uuid.UUID(CONN_ID)}})
        rows = self._backfill_set()

        applied = [
            name
            for name in replication.registered()
            if name in rows
            and await replication_sync._try_upsert(db, replication.get(name), rows[name])
        ]

        assert applied == [
            "module_autodiscovery_rules",
            "module_autodiscovery_repositories",
            "registry_modules",
        ]
        assert uuid.UUID(MODULE_ID) in db.rows["registry_modules"]

    async def test_the_module_does_not_land_before_its_rule(self):
        """The #1666 failure, and proof the fake above can fail: without the
        rule on the follower the discovered module is refused."""
        db = _FKEnforcingDB({"vcs_connections": {uuid.UUID(CONN_ID)}})

        landed = await replication_sync._try_upsert(db, MODULES, _module_payload())

        assert landed is False
        assert "registry_modules" not in db.rows

    async def test_a_hand_registered_module_never_needed_the_rule(self):
        """The foreign key is nullable; only discovered modules depend on it."""
        db = _FKEnforcingDB({"vcs_connections": {uuid.UUID(CONN_ID)}})

        landed = await replication_sync._try_upsert(
            db, MODULES, _module_payload(module_autodiscovery_rule_id=None)
        )

        assert landed is True
