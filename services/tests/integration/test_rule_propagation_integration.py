"""Integration tests for propagating a rule's changed template fields to its workspaces (#2549)."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from terrapod.db.models import (
    AgentPool,
    AuditLog,
    AutodiscoveryRule,
    Run,
    RunTask,
    VCSConnection,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.services.rule_propagation_service import propagate_rule_update
from tests.integration.conftest import admin_user, regular_user

pytestmark = pytest.mark.integration

ACTOR = "admin@test.com"
REQUEST_ID = "req-2549-propagate"


async def _seed(
    workspaces: dict[str, dict],
    run_tasks: dict[str, list[dict]] | None = None,
    rule_columns: dict | None = None,
):
    """A rule and the named workspaces it created, with optional run-task rows each."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        conn = VCSConnection(
            provider="github",
            name=f"conn-{tag}",
            server_url="",
            token="x",
            status="active",
            github_installation_id=int(tag[:7], 16),
        )
        db.add(conn)
        await db.flush()
        rule = AutodiscoveryRule(
            vcs_connection_id=conn.id,
            repo_url="https://github.com/acme/repo",
            pattern="stacks/*/*.tf",
            name=f"rule-{tag}",
            **(rule_columns or {}),
        )
        db.add(rule)
        await db.flush()
        for name, columns in workspaces.items():
            ws = Workspace(name=f"{tag}-{name}", autodiscovery_rule_id=rule.id, **columns)
            db.add(ws)
            await db.flush()
            for spec in (run_tasks or {}).get(name, []):
                db.add(RunTask(workspace_id=ws.id, **spec))
        return rule.id


async def _propagate(rule_id, previous, changed, *, dry_run=False, user=None):
    async with get_db_session() as db:
        rule = await db.get(AutodiscoveryRule, rule_id)
        result = await propagate_rule_update(
            db, rule, previous, changed, user or admin_user(ACTOR), REQUEST_ID, dry_run=dry_run
        )
        if dry_run:
            await db.rollback()
        return result


async def _workspaces(rule_id) -> dict[str, Workspace]:
    """The rule's workspaces re-read in a fresh session, by their short name."""
    async with get_db_session() as db:
        rows = (
            (await db.execute(select(Workspace).where(Workspace.autodiscovery_rule_id == rule_id)))
            .scalars()
            .all()
        )
        return {w.name.split("-", 1)[1]: w for w in rows}


async def _run_tasks(rule_id) -> dict[str, dict[str, RunTask]]:
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(Workspace.name, RunTask)
                .join(RunTask, RunTask.workspace_id == Workspace.id)
                .where(Workspace.autodiscovery_rule_id == rule_id)
            )
        ).all()
    out: dict[str, dict[str, RunTask]] = {}
    for ws_name, row in rows:
        out.setdefault(ws_name.split("-", 1)[1], {})[row.name] = row
    return out


async def _audit_rows() -> list[AuditLog]:
    async with get_db_session() as db:
        query = select(AuditLog).where(AuditLog.action == "workspace.rule_propagate")
        return list((await db.execute(query)).scalars().all())


ENGINE = ({"engine-version": "1.8"}, {"engine-version": "1.9"})


class TestPropagateRuleUpdate:
    async def test_only_workspaces_still_on_the_previous_value_are_updated(self, app):
        rule_id = await _seed(
            {"stale": {"engine_version": "1.8"}, "own": {"engine_version": "1.5"}}
        )

        result = await _propagate(rule_id, *ENGINE)

        ws = await _workspaces(rule_id)
        assert (ws["stale"].engine_version, ws["own"].engine_version) == ("1.9", "1.5")
        assert [w["name"].split("-", 1)[1] for w in result.would_change] == ["stale"]
        assert result.would_change[0]["diff"] == {"engine_version": {"from": "1.8", "to": "1.9"}}
        [own] = result.customised
        assert own["fields"] == {
            "engine-version": {"current": "1.5", "previous": "1.8", "new": "1.9"}
        }
        assert (result.matched, result.unchanged, result.skipped_lifecycle) == (2, [], 0)

    async def test_an_archived_workspace_is_skipped_and_counted(self, app):
        rule_id = await _seed(
            {
                "live": {"engine_version": "1.8"},
                "gone": {"engine_version": "1.8", "lifecycle_state": "archived"},
            }
        )

        result = await _propagate(rule_id, *ENGINE)

        ws = await _workspaces(rule_id)
        assert (ws["live"].engine_version, ws["gone"].engine_version) == ("1.9", "1.8")
        assert (result.matched, result.skipped_lifecycle) == (1, 1)

    async def test_one_refused_workspace_refuses_the_whole_call(self, app):
        rule_id = await _seed(
            {
                "a-tf": {"engine_version": "1.8"},
                "b-pulumi": {"engine": "pulumi", "engine_version": "1.9"},
            }
        )
        previous = {"engine-version": "1.8", "security-scan-enforcement": "advisory"}
        changed = {"engine-version": "1.9", "security-scan-enforcement": "enforced"}

        with pytest.raises(HTTPException) as exc:
            await _propagate(rule_id, previous, changed)

        assert exc.value.status_code == 422
        assert "b-pulumi" in exc.value.detail
        ws = await _workspaces(rule_id)
        assert ws["a-tf"].engine_version == "1.8"
        assert ws["a-tf"].security_scan_enforcement == "advisory"
        assert await _audit_rows() == []

    async def test_a_dry_run_changes_nothing_once_rolled_back(self, app):
        rule_id = await _seed({"stale": {"engine_version": "1.8"}})

        result = await _propagate(rule_id, *ENGINE, dry_run=True)

        assert result.dry_run is True
        assert [w["diff"] for w in result.would_change] == [
            {"engine_version": {"from": "1.8", "to": "1.9"}}
        ]
        assert (await _workspaces(rule_id))["stale"].engine_version == "1.8"
        assert await _audit_rows() == []

    async def test_one_audit_row_per_changed_workspace_carrying_the_request_id(self, app):
        rule_id = await _seed(
            {
                "a": {"engine_version": "1.8"},
                "b": {"engine_version": "1.8"},
                "own": {"engine_version": "1.5"},
            }
        )

        result = await _propagate(rule_id, *ENGINE)

        rows = await _audit_rows()
        assert sorted(r.resource_id for r in rows) == sorted(w["id"] for w in result.would_change)
        assert len(rows) == 2
        for row in rows:
            assert row.request_id == REQUEST_ID
            assert row.actor_email == ACTOR
            assert row.detail == f"autodiscovery-rule {rule_id}: ['engine_version']"

    async def test_no_run_is_created(self, app):
        rule_id = await _seed({"a": {"engine_version": "1.8"}, "b": {"engine_version": "1.8"}})

        await _propagate(rule_id, *ENGINE)

        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(Run)) == 0

    async def test_a_partly_customised_workspace_is_applied_and_reported(self, app):
        rule_id = await _seed(
            {"mixed": {"engine_version": "1.8", "drift_detection_interval_seconds": 7200}}
        )
        previous = {"engine-version": "1.8", "drift-detection-interval-seconds": 3600}
        changed = {"engine-version": "1.9", "drift-detection-interval-seconds": 14400}

        result = await _propagate(rule_id, previous, changed)

        ws = (await _workspaces(rule_id))["mixed"]
        assert (ws.engine_version, ws.drift_detection_interval_seconds) == ("1.9", 7200)
        assert [w["diff"] for w in result.would_change] == [
            {"engine_version": {"from": "1.8", "to": "1.9"}}
        ]
        assert list(result.customised[0]["fields"]) == ["drift-detection-interval-seconds"]


class TestPropagateGuardsPerGroup:
    async def test_a_guard_judges_only_the_workspaces_it_would_change(self, app):
        rule_id = await _seed(
            {
                "tf": {"security_scan_enforcement": "advisory"},
                "pulumi": {"engine": "pulumi", "security_scan_enforcement": "off"},
            }
        )
        field = "security-scan-enforcement"

        result = await _propagate(rule_id, {field: "advisory"}, {field: "enforced"})

        ws = await _workspaces(rule_id)
        assert ws["tf"].security_scan_enforcement == "enforced"
        assert ws["pulumi"].security_scan_enforcement == "off"
        assert [w["name"].split("-", 1)[1] for w in result.customised] == ["pulumi"]


class TestPropagateRunKind:
    OPS = {"plan_apply_enabled": True, "configure_enabled": True, "default_run_kind": "configure"}

    async def test_a_customised_kind_the_flags_would_clear_is_refused(self, app):
        rule_id = await _seed({"own": self.OPS})
        previous = {"configure-enabled": True, "default-run-kind": "plan_apply"}
        changed = {"configure-enabled": False, "default-run-kind": "plan"}

        with pytest.raises(HTTPException) as exc:
            await _propagate(rule_id, previous, changed)

        assert exc.value.status_code == 422
        assert "own" in exc.value.detail
        ws = (await _workspaces(rule_id))["own"]
        assert (ws.configure_enabled, ws.default_run_kind) == (True, "configure")

    async def test_a_kind_the_flags_clear_is_reported_and_audited(self, app):
        rule_id = await _seed({"w": self.OPS})

        result = await _propagate(
            rule_id, {"configure-enabled": True}, {"configure-enabled": False}
        )

        ws = (await _workspaces(rule_id))["w"]
        assert (ws.configure_enabled, ws.default_run_kind) == (False, "")
        [entry] = result.would_change
        assert entry["diff"]["default_run_kind"] == {"from": "configure", "to": ""}
        [row] = await _audit_rows()
        assert row.detail == (
            f"autodiscovery-rule {rule_id}: ['configure_enabled', 'default_run_kind']"
        )


class TestPropagatePoolPermission:
    """Real pool RBAC: the caller's own roles decide, with the pool's owner as the grant."""

    async def _pool(self, owner: str) -> uuid.UUID:
        async with get_db_session() as db:
            pool = AgentPool(name=f"pool-{uuid.uuid4().hex[:8]}", owner_email=owner)
            db.add(pool)
            await db.flush()
            return pool.id

    async def test_a_caller_without_pool_assign_is_refused_and_nothing_changes(self, app):
        pool_id = await self._pool("someone-else@test.com")
        rule_id = await _seed({"w": {}})
        field = "agent-pool-id"

        with pytest.raises(HTTPException) as exc:
            await _propagate(rule_id, {field: None}, {field: pool_id}, user=regular_user())

        assert exc.value.status_code == 403
        assert "write permission on agent pool" in exc.value.detail
        assert await _audit_rows() == []

    async def test_a_caller_holding_pool_assign_applies_the_pool(self, app):
        user = regular_user()
        pool_id = await self._pool(user.email)
        rule_id = await _seed({"w": {}})
        field = "agent-pool-id"

        result = await _propagate(rule_id, {field: None}, {field: pool_id}, user=user)

        assert [w["diff"] for w in result.would_change] == [
            {"agent-pool-ids": {"from": [], "to": [f"apool-{pool_id}"]}}
        ]

    async def test_an_admin_is_not_asked_for_pool_assign(self, app):
        pool_id = await self._pool("someone-else@test.com")
        rule_id = await _seed({"w": {}})

        result = await _propagate(
            rule_id, {"agent-pool-id": None}, {"agent-pool-id": pool_id}, user=admin_user()
        )

        assert len(result.would_change) == 1


class TestPropagateLabels:
    async def test_labels_are_replaced_only_where_they_equal_the_previous_set(self, app):
        rule_id = await _seed(
            {
                "same": {"labels": {"team": "a"}},
                "extra": {"labels": {"team": "a", "mine": "x"}},
            }
        )

        result = await _propagate(rule_id, {"labels": {"team": "a"}}, {"labels": {"team": "b"}})

        ws = await _workspaces(rule_id)
        assert ws["same"].labels == {"team": "b"}
        assert ws["extra"].labels == {"team": "a", "mine": "x"}
        assert [w["name"].split("-", 1)[1] for w in result.customised] == ["extra"]


def _task(name: str = "scan", **overrides) -> dict:
    spec = {
        "name": name,
        "url": "https://hooks.example/new",
        "hmac_key": "new-key",
        "stage": "pre_plan",
        "enforcement_level": "mandatory",
        "enabled": True,
    }
    return {**spec, **overrides}


def _short(entries) -> list[str]:
    return sorted(e["name"].split("-", 1)[1] for e in entries)


class TestPropagateRunTasks:
    OLD = _task(url="https://hooks.example/old", hmac_key="old-key")

    async def _run(self, *, dry_run=False):
        rule_id = await _seed(
            {"absent": {}, "stale": {}, "own": {}},
            run_tasks={
                "stale": [self.OLD],
                "own": [_task(url="https://hooks.example/mine", hmac_key="own-key")],
            },
        )
        field = "run-task-templates"
        result = await _propagate(
            rule_id, {field: [self.OLD]}, {field: [_task(), _task("lint")]}, dry_run=dry_run
        )
        return rule_id, result

    async def test_run_tasks_are_upserted_by_name_and_customised_ones_skipped(self, app):
        rule_id, result = await self._run()

        rows = await _run_tasks(rule_id)
        assert sorted(rows["stale"]) == ["lint", "scan"]
        assert rows["stale"]["scan"].url == "https://hooks.example/new"
        assert rows["stale"]["scan"].hmac_key == "new-key"
        assert sorted(rows["own"]) == ["scan"]
        assert rows["own"]["scan"].url == "https://hooks.example/mine"
        assert rows["own"]["scan"].hmac_key == "own-key"
        by_name = {w["name"].split("-", 1)[1]: w["diff"] for w in result.would_change}
        assert by_name == {"stale": {"run-tasks": ["scan", "lint"]}}
        assert _short(result.customised) == ["absent", "own"]

    async def test_a_template_the_rule_had_and_a_person_deleted_is_not_resurrected(self, app):
        rule_id, _ = await self._run()

        assert "absent" not in await _run_tasks(rule_id)

    async def test_a_template_new_to_the_rule_is_inserted_where_absent(self, app):
        rule_id = await _seed({"absent": {}})
        field = "run-task-templates"

        result = await _propagate(rule_id, {field: []}, {field: [_task()]})

        assert sorted((await _run_tasks(rule_id))["absent"]) == ["scan"]
        assert [w["name"].split("-", 1)[1] for w in result.would_change] == ["absent"]
        assert result.customised == []

    async def test_the_result_never_carries_a_secret(self, app):
        _, result = await self._run(dry_run=True)

        assert result.customised
        assert not [s for s in ("new-key", "old-key", "own-key") if s in repr(result)]


class TestPropagateOidcAudiences:
    FIELD = "oidc-audiences"
    OLD = {"aws": ["sts.old.example"]}
    NEW = {"aws": ["sts.new.example"], "vault.eu": ["https://vault.example/"]}

    async def test_the_whole_map_is_replaced_only_where_it_equals_the_previous_one(self, app):
        rule_id = await _seed(
            {"stale": {"oidc_audiences": self.OLD}, "own": {"oidc_audiences": {"gcp": ["x"]}}}
        )

        result = await _propagate(rule_id, {self.FIELD: self.OLD}, {self.FIELD: self.NEW})

        ws = await _workspaces(rule_id)
        assert ws["stale"].oidc_audiences == self.NEW
        assert ws["own"].oidc_audiences == {"gcp": ["x"]}
        assert [w["name"].split("-", 1)[1] for w in result.customised] == ["own"]

    async def test_an_invalid_map_is_refused_and_nothing_changes(self, app):
        rule_id = await _seed({"stale": {"oidc_audiences": self.OLD}})

        with pytest.raises(HTTPException) as exc:
            await _propagate(rule_id, {self.FIELD: self.OLD}, {self.FIELD: {"aws/../vault": ["x"]}})

        assert exc.value.status_code == 422
        assert (await _workspaces(rule_id))["stale"].oidc_audiences == self.OLD
        assert await _audit_rows() == []


class TestPropagateScheduledConfigure:
    OFF = {
        "scheduled_configure_enabled": False,
        "scheduled_configure_cron": "",
        "scheduled_configure_timezone": "UTC",
    }
    PREVIOUS = {
        "scheduled-configure-enabled": False,
        "scheduled-configure-cron": "",
        "scheduled-configure-timezone": "UTC",
    }
    ON = {
        "scheduled-configure-enabled": True,
        "scheduled-configure-cron": "0 2 * * *",
        "scheduled-configure-timezone": "UTC",
    }

    @staticmethod
    def _trio(ws: Workspace) -> tuple:
        return (
            ws.scheduled_configure_enabled,
            ws.scheduled_configure_cron,
            ws.scheduled_configure_timezone,
        )

    async def _seed(self):
        return await _seed(
            {
                "stale": dict(self.OFF),
                "own-cron": {**self.OFF, "scheduled_configure_cron": "30 4 * * 1"},
                "on": {
                    "scheduled_configure_enabled": True,
                    "scheduled_configure_cron": "0 2 * * *",
                },
            },
            rule_columns=dict(self.OFF),
        )

    async def test_the_trio_is_applied_together_and_a_customised_workspace_keeps_its_own(self, app):
        rule_id = await self._seed()

        result = await _propagate(rule_id, self.PREVIOUS, self.ON)

        ws = await _workspaces(rule_id)
        assert self._trio(ws["stale"]) == (True, "0 2 * * *", "UTC")
        assert ws["stale"].scheduled_configure_last_at is not None
        assert self._trio(ws["own-cron"]) == (False, "30 4 * * 1", "UTC")
        assert ws["own-cron"].scheduled_configure_last_at is None
        assert [w["name"].split("-", 1)[1] for w in result.would_change] == ["stale"]
        [own] = result.customised
        assert own["name"].endswith("-own-cron")
        assert set(own["fields"]) == {"scheduled-configure-enabled", "scheduled-configure-cron"}
        assert own["fields"]["scheduled-configure-cron"] == {
            "current": "30 4 * * 1",
            "previous": "",
            "new": "0 2 * * *",
        }

    async def test_a_workspace_already_on_the_new_schedule_is_unchanged(self, app):
        rule_id = await self._seed()

        result = await _propagate(rule_id, self.PREVIOUS, self.ON)

        assert [w["name"].split("-", 1)[1] for w in result.unchanged] == ["on"]

    async def test_a_dry_run_reports_the_schedule_and_changes_nothing(self, app):
        rule_id = await self._seed()

        result = await _propagate(rule_id, self.PREVIOUS, self.ON, dry_run=True)

        assert result.dry_run is True
        assert [w["name"].split("-", 1)[1] for w in result.would_change] == ["stale"]
        assert self._trio((await _workspaces(rule_id))["stale"]) == (False, "", "UTC")

    async def test_a_zone_edit_leaves_a_workspace_with_its_own_cron_in_its_own_zone(self, app):
        rule_id = await self._seed()

        await _propagate(
            rule_id,
            {"scheduled-configure-timezone": "UTC"},
            {"scheduled-configure-timezone": "Europe/London"},
        )

        ws = await _workspaces(rule_id)
        assert ws["own-cron"].scheduled_configure_timezone == "UTC"
        assert ws["on"].scheduled_configure_timezone == "UTC"
        assert ws["stale"].scheduled_configure_timezone == "Europe/London"
