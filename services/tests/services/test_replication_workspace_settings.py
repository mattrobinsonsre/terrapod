"""Replication of how a workspace's runs behave (#1141).

Everything that hangs off a workspace and governs its runs: policy sets,
notifications, run tasks, execution hooks, run triggers, and the remote-state
allow-list.

Three of these are **gates**, and for a gate the failure mode of partial
replication is not an error — it is a silently weaker posture:

- a **mandatory policy set** that loses its `enforcement_level` becomes an
  advisory note, and applies that should have been blocked proceed;
- a **mandatory run task** does the same;
- the **remote-state allow-list** is access control, so a consumer that comes
  back to life through a backfill can read state somebody deliberately cut it
  off from.

That is why the enforcement levels get their own assertions rather than being
left implied by "the columns carry". A test that only checks the row arrived
would pass on a node whose guardrails had all quietly become suggestions.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.crypto.types import EncryptedText
from terrapod.db.models import (
    ExecutionHook,
    ExecutionHookWorkspace,
    NotificationConfiguration,
    Policy,
    PolicySet,
    RunTask,
    RunTrigger,
    WorkspaceRemoteStateConsumer,
)
from terrapod.services import replication, replication_registry

POLICY_SETS = replication_registry.POLICY_SETS
POLICIES = replication_registry.POLICIES
NOTIFICATIONS = replication_registry.NOTIFICATION_CONFIGURATIONS
RUN_TASKS = replication_registry.RUN_TASKS
HOOKS = replication_registry.EXECUTION_HOOKS
HOOK_ASSIGNMENTS = replication_registry.EXECUTION_HOOK_WORKSPACES
TRIGGERS = replication_registry.RUN_TRIGGERS
CONSUMERS = replication_registry.WORKSPACE_REMOTE_STATE_CONSUMERS

WS_ID = "44444444-4444-4444-4444-444444444444"
OTHER_WS_ID = "55555555-5555-5555-5555-555555555555"
ID_A = "aaaaaaaa-0000-0000-0000-000000000001"
ID_B = "aaaaaaaa-0000-0000-0000-000000000002"


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


def _policy_set(**kw):
    base = {
        "id": ID_A,
        "name": "no-public-buckets",
        "description": "",
        "enforcement_level": "mandatory",
        "enabled": True,
        "global_scope": True,
        "created_by": "admin",
        **_stamps(),
    }
    base.update(kw)
    return PolicySet(**base)


def _policy(**kw):
    base = {
        "id": ID_B,
        "policy_set_id": ID_A,
        "name": "deny-public-acl",
        "description": "",
        "rego": "package terrapod\ndeny[msg] { false }\n",
        **_stamps(),
    }
    base.update(kw)
    return Policy(**base)


def _notification(**kw):
    base = {
        "id": ID_A,
        "workspace_id": WS_ID,
        "name": "slack-platform",
        "destination_type": "slack",
        "url": "https://hooks.example.com/services/T000/B000/xxxx",
        "token": "a-shared-secret",
        "enabled": True,
        "triggers": ["run:errored"],
        "email_addresses": [],
        **_stamps(),
    }
    base.update(kw)
    return NotificationConfiguration(**base)


def _run_task(**kw):
    base = {
        "id": ID_A,
        "workspace_id": WS_ID,
        "name": "compliance-check",
        "url": "https://tasks.example.com/hook",
        "hmac_key": "shared",
        "enabled": True,
        "stage": "post_plan",
        "enforcement_level": "mandatory",
        **_stamps(),
    }
    base.update(kw)
    return RunTask(**base)


def _hook(**kw):
    base = {
        "id": ID_A,
        "name": "vault-login",
        "description": "",
        "hook_point": "pre_init",
        "script": "#!/bin/sh\necho hello\n",
        "enabled": True,
        "priority": 10,
        **_stamps(),
    }
    base.update(kw)
    return ExecutionHook(**base)


class TestGatesCarryTheirEnforcement:
    """The assertions that matter more than "the row arrived"."""

    async def test_a_mandatory_policy_set_stays_mandatory(self):
        """Carrying the set but losing `mandatory` turns a hard stop into an
        advisory note — applies that should have been blocked proceed, and
        nothing reports a weakened posture."""
        db = AsyncMock()
        existing = _policy_set(enforcement_level="advisory")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, POLICY_SETS, {"id": ID_A, "enforcement_level": "mandatory"}
        )

        assert existing.enforcement_level == "mandatory"

    async def test_a_mandatory_run_task_stays_mandatory(self):
        db = AsyncMock()
        existing = _run_task(enforcement_level="advisory")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, RUN_TASKS, {"id": ID_A, "enforcement_level": "mandatory"}
        )

        assert existing.enforcement_level == "mandatory"

    async def test_a_disabled_policy_set_stays_disabled(self):
        """The other direction matters too: a set an operator switched off must
        not come back to life evaluating policy nobody expects."""
        db = AsyncMock()
        existing = _policy_set(enabled=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, POLICY_SETS, {"id": ID_A, "enabled": False})

        assert existing.enabled is False

    def test_the_enforcement_levels_are_in_the_payloads(self):
        assert replication.serialize_row(POLICY_SETS, _policy_set())["enforcement_level"] == (
            "mandatory"
        )
        assert replication.serialize_row(RUN_TASKS, _run_task())["enforcement_level"] == (
            "mandatory"
        )


class TestPolicySets:
    @pytest.mark.replication_matrix("policy_sets", "backfill-from-empty")
    async def test_backfill_carries_the_scope_rules(self):
        db = _rows_db([_policy_set(allow_labels={"env": "prod"}, global_scope=False)])

        page = await replication.read_backfill(db, POLICY_SETS)

        assert page[0]["name"] == "no-public-buckets"
        assert page[0]["allow_labels"] == {"env": "prod"}
        assert page[0]["global_scope"] is False

    @pytest.mark.replication_matrix("policy_sets", "delta-apply")
    async def test_the_vcs_sync_cursor_reaches_the_peer(self):
        """Policy sets sourced from VCS track their own commit. A node that has
        not seen the cursor re-syncs the whole set on its first poll."""
        db = AsyncMock()
        existing = _policy_set(vcs_last_commit_sha="b" * 40)
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, POLICY_SETS, {"id": ID_A, "vcs_last_commit_sha": "a" * 40}
        )

        assert existing.vcs_last_commit_sha == "a" * 40

    @pytest.mark.replication_matrix("policy_sets", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _policy_set()
        db.scalar.return_value = existing
        payload = replication.serialize_row(POLICY_SETS, existing)

        await replication.apply_upsert(db, POLICY_SETS, payload)
        await replication.apply_upsert(db, POLICY_SETS, payload)

        assert existing.enforcement_level == "mandatory"
        assert existing.enabled is True

    @pytest.mark.replication_matrix("policy_sets", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, POLICY_SETS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("policy_sets", "backfill-converges-deletion")
    async def test_a_deleted_set_does_not_survive_a_backfill(self):
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, POLICY_SETS, {ID_A})

        assert removed == [ID_B]


class TestPolicies:
    @pytest.mark.replication_matrix("policies", "backfill-from-empty")
    async def test_backfill_carries_the_rego(self):
        """Without the Rego there is no policy — an empty set evaluates to
        'nothing denied', which looks exactly like a passing evaluation."""
        db = _rows_db([_policy()])

        page = await replication.read_backfill(db, POLICIES)

        assert "package terrapod" in page[0]["rego"]
        assert page[0]["policy_set_id"] == ID_A

    @pytest.mark.replication_matrix("policies", "delta-apply")
    async def test_an_edited_policy_reaches_the_peer(self):
        db = AsyncMock()
        existing = _policy(rego="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, POLICIES, {"id": ID_B, "rego": "new"})

        assert existing.rego == "new"

    @pytest.mark.replication_matrix("policies", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _policy()
        db.scalar.return_value = existing
        payload = replication.serialize_row(POLICIES, existing)

        await replication.apply_upsert(db, POLICIES, payload)
        await replication.apply_upsert(db, POLICIES, payload)

        assert existing.name == "deny-public-acl"

    @pytest.mark.replication_matrix("policies", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, POLICIES, ID_B)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("policies", "backfill-converges-deletion")
    async def test_a_deleted_policy_does_not_survive_a_backfill(self):
        db = _keys_db([(ID_B,), (ID_A,)])

        removed = await replication.reconcile_deletions(db, POLICIES, {ID_B})

        assert removed == [ID_A]


class TestNotificationConfigurations:
    @pytest.mark.replication_matrix("notification_configurations", "backfill-from-empty")
    async def test_backfill_carries_the_destination_and_triggers(self):
        db = _rows_db([_notification()])

        page = await replication.read_backfill(db, NOTIFICATIONS)

        assert page[0]["destination_type"] == "slack"
        assert page[0]["triggers"] == ["run:errored"]

    @pytest.mark.replication_matrix("notification_configurations", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _notification(enabled=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, NOTIFICATIONS, {"id": ID_A, "enabled": False})

        assert existing.enabled is False

    @pytest.mark.replication_matrix("notification_configurations", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _notification()
        db.scalar.return_value = existing
        payload = replication.serialize_row(NOTIFICATIONS, existing)

        await replication.apply_upsert(db, NOTIFICATIONS, payload)
        await replication.apply_upsert(db, NOTIFICATIONS, payload)

        assert existing.name == "slack-platform"
        assert existing.token == "a-shared-secret"

    @pytest.mark.replication_matrix("notification_configurations", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, NOTIFICATIONS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("notification_configurations", "backfill-converges-deletion")
    async def test_a_deleted_config_does_not_survive_a_backfill(self):
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, NOTIFICATIONS, {ID_A})

        assert removed == [ID_B]

    @pytest.mark.replication_matrix("notification_configurations", "encrypted-columns")
    def test_the_token_is_encrypted_at_rest_and_travels_decrypted(self):
        from sqlalchemy import inspect as sa_inspect

        encrypted = {
            col.key
            for col in sa_inspect(NotificationConfiguration).column_attrs
            if isinstance(col.expression.type, EncryptedText)
        }

        assert encrypted == {"token"}
        assert replication.serialize_row(NOTIFICATIONS, _notification())["token"] == (
            "a-shared-secret"
        )


class TestRunTasks:
    @pytest.mark.replication_matrix("run_tasks", "backfill-from-empty")
    async def test_backfill_carries_the_stage_and_url(self):
        db = _rows_db([_run_task()])

        page = await replication.read_backfill(db, RUN_TASKS)

        assert page[0]["stage"] == "post_plan"
        assert page[0]["url"] == "https://tasks.example.com/hook"

    @pytest.mark.replication_matrix("run_tasks", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _run_task(stage="pre_plan")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, RUN_TASKS, {"id": ID_A, "stage": "pre_apply"})

        assert existing.stage == "pre_apply"

    @pytest.mark.replication_matrix("run_tasks", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _run_task()
        db.scalar.return_value = existing
        payload = replication.serialize_row(RUN_TASKS, existing)

        await replication.apply_upsert(db, RUN_TASKS, payload)
        await replication.apply_upsert(db, RUN_TASKS, payload)

        assert existing.name == "compliance-check"
        assert existing.enforcement_level == "mandatory"

    @pytest.mark.replication_matrix("run_tasks", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, RUN_TASKS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("run_tasks", "backfill-converges-deletion")
    async def test_a_deleted_task_does_not_survive_a_backfill(self):
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, RUN_TASKS, {ID_A})

        assert removed == [ID_B]

    @pytest.mark.replication_matrix("run_tasks", "encrypted-columns")
    async def test_the_hmac_key_survives_the_per_node_round_trip(self):
        """`hmac_key` became `EncryptedText` in #1140, which is what made the
        matrix gate start demanding this row — the gate derives it from the model,
        so a class that gains an encrypted column fails until the round trip is
        proven.

        The property: the payload carries the key decrypted (the peer cannot use
        this node's ciphertext), and applying writes it back through the column so
        the receiving node re-encrypts under its own key."""
        from sqlalchemy import inspect as sa_inspect

        from terrapod.crypto.types import EncryptedText as _Enc

        column = sa_inspect(RunTask).local_table.c["hmac_key"]
        assert isinstance(column.type, _Enc)

        assert replication.serialize_row(RUN_TASKS, _run_task())["hmac_key"] == "shared"

        db = AsyncMock()
        existing = _run_task(hmac_key="theirs")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, RUN_TASKS, {"id": ID_A, "hmac_key": "rotated"})

        assert existing.hmac_key == "rotated"

    async def test_a_task_with_no_hmac_key_stays_without_one(self):
        """An unsigned task is a real configuration. `None` must stay `None`
        rather than becoming a string the dispatcher would then sign with."""
        db = AsyncMock()
        existing = _run_task(hmac_key="stale")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, RUN_TASKS, replication.serialize_row(RUN_TASKS, _run_task(hmac_key=None))
        )

        assert existing.hmac_key is None


class TestExecutionHooks:
    @pytest.mark.replication_matrix("execution_hooks", "backfill-from-empty")
    async def test_backfill_carries_the_script_and_where_it_runs(self):
        db = _rows_db([_hook()])

        page = await replication.read_backfill(db, HOOKS)

        assert page[0]["hook_point"] == "pre_init"
        assert "echo hello" in page[0]["script"]

    @pytest.mark.replication_matrix("execution_hooks", "delta-apply")
    async def test_priority_reaches_the_peer(self):
        """`priority` decides execution order, which is behaviour rather than
        presentation: two hooks in the wrong order can leave a run set up
        differently from the same run on the leader."""
        db = AsyncMock()
        existing = _hook(priority=10)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, HOOKS, {"id": ID_A, "priority": 1})

        assert existing.priority == 1

    @pytest.mark.replication_matrix("execution_hooks", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _hook()
        db.scalar.return_value = existing
        payload = replication.serialize_row(HOOKS, existing)

        await replication.apply_upsert(db, HOOKS, payload)
        await replication.apply_upsert(db, HOOKS, payload)

        assert existing.hook_point == "pre_init"
        assert existing.priority == 10

    @pytest.mark.replication_matrix("execution_hooks", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, HOOKS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("execution_hooks", "backfill-converges-deletion")
    async def test_a_deleted_hook_does_not_survive_a_backfill(self):
        """A hook that comes back runs a script on the operator's infrastructure
        that they deleted deliberately."""
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, HOOKS, {ID_A})

        assert removed == [ID_B]


class TestExecutionHookAssignments:
    @pytest.mark.replication_matrix("execution_hook_workspaces", "backfill-from-empty")
    async def test_backfill_carries_both_sides(self):
        db = _rows_db([ExecutionHookWorkspace(hook_id=ID_A, workspace_id=WS_ID)])

        page = await replication.read_backfill(db, HOOK_ASSIGNMENTS)

        assert page[0]["hook_id"] == ID_A
        assert page[0]["workspace_id"] == WS_ID

    @pytest.mark.replication_matrix("execution_hook_workspaces", "delta-apply")
    async def test_an_assignment_applies(self):
        db = AsyncMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db, HOOK_ASSIGNMENTS, {"hook_id": ID_A, "workspace_id": WS_ID}
        )

        db.add.assert_called_once()

    @pytest.mark.replication_matrix("execution_hook_workspaces", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = ExecutionHookWorkspace(hook_id=ID_A, workspace_id=WS_ID)
        db.scalar.return_value = existing
        payload = replication.serialize_row(HOOK_ASSIGNMENTS, existing)

        await replication.apply_upsert(db, HOOK_ASSIGNMENTS, payload)
        await replication.apply_upsert(db, HOOK_ASSIGNMENTS, payload)

        assert str(existing.hook_id) == ID_A
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("execution_hook_workspaces", "delete")
    async def test_unassigning_applies(self):
        db = AsyncMock()
        entity_id = replication.encode_entity_id(HOOK_ASSIGNMENTS, [ID_A, WS_ID])

        await replication.apply_delete(db, HOOK_ASSIGNMENTS, entity_id)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("execution_hook_workspaces", "backfill-converges-deletion")
    async def test_an_unassignment_converges(self):
        db = _keys_db([(ID_A, WS_ID), (ID_A, OTHER_WS_ID)])
        kept = replication.encode_entity_id(HOOK_ASSIGNMENTS, [ID_A, WS_ID])

        removed = await replication.reconcile_deletions(db, HOOK_ASSIGNMENTS, {kept})

        assert removed == [replication.encode_entity_id(HOOK_ASSIGNMENTS, [ID_A, OTHER_WS_ID])]


class TestRunTriggers:
    @pytest.mark.replication_matrix("run_triggers", "backfill-from-empty")
    async def test_backfill_carries_the_dependency_direction(self):
        """Which way round the pair goes IS the dependency. Reversing it would
        fire the wrong workspace's runs."""
        db = _rows_db(
            [
                RunTrigger(
                    id=ID_A,
                    workspace_id=WS_ID,
                    source_workspace_id=OTHER_WS_ID,
                    created_at=datetime.now(UTC),
                )
            ]
        )

        page = await replication.read_backfill(db, TRIGGERS)

        assert page[0]["workspace_id"] == WS_ID
        assert page[0]["source_workspace_id"] == OTHER_WS_ID

    @pytest.mark.replication_matrix("run_triggers", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = RunTrigger(id=ID_A, workspace_id=WS_ID, source_workspace_id=WS_ID)
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, TRIGGERS, {"id": ID_A, "source_workspace_id": OTHER_WS_ID}
        )

        assert str(existing.source_workspace_id) == OTHER_WS_ID

    @pytest.mark.replication_matrix("run_triggers", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = RunTrigger(
            id=ID_A,
            workspace_id=WS_ID,
            source_workspace_id=OTHER_WS_ID,
            created_at=datetime.now(UTC),
        )
        db.scalar.return_value = existing
        payload = replication.serialize_row(TRIGGERS, existing)

        await replication.apply_upsert(db, TRIGGERS, payload)
        await replication.apply_upsert(db, TRIGGERS, payload)

        assert str(existing.source_workspace_id) == OTHER_WS_ID

    @pytest.mark.replication_matrix("run_triggers", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, TRIGGERS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("run_triggers", "backfill-converges-deletion")
    async def test_a_deleted_trigger_does_not_survive_a_backfill(self):
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, TRIGGERS, {ID_A})

        assert removed == [ID_B]


class TestRemoteStateConsumers:
    """Access control. Its deletions matter as much as its rows."""

    @pytest.mark.replication_matrix("workspace_remote_state_consumers", "backfill-from-empty")
    async def test_backfill_carries_producer_and_consumer(self):
        db = _rows_db(
            [
                WorkspaceRemoteStateConsumer(
                    id=ID_A,
                    producer_workspace_id=WS_ID,
                    consumer_workspace_id=OTHER_WS_ID,
                    created_at=datetime.now(UTC),
                    created_by="admin",
                )
            ]
        )

        page = await replication.read_backfill(db, CONSUMERS)

        assert page[0]["producer_workspace_id"] == WS_ID
        assert page[0]["consumer_workspace_id"] == OTHER_WS_ID

    @pytest.mark.replication_matrix("workspace_remote_state_consumers", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = WorkspaceRemoteStateConsumer(
            id=ID_A,
            producer_workspace_id=WS_ID,
            consumer_workspace_id=WS_ID,
            created_by="admin",
        )
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, CONSUMERS, {"id": ID_A, "consumer_workspace_id": OTHER_WS_ID}
        )

        assert str(existing.consumer_workspace_id) == OTHER_WS_ID

    @pytest.mark.replication_matrix("workspace_remote_state_consumers", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = WorkspaceRemoteStateConsumer(
            id=ID_A,
            producer_workspace_id=WS_ID,
            consumer_workspace_id=OTHER_WS_ID,
            created_at=datetime.now(UTC),
            created_by="admin",
        )
        db.scalar.return_value = existing
        payload = replication.serialize_row(CONSUMERS, existing)

        await replication.apply_upsert(db, CONSUMERS, payload)
        await replication.apply_upsert(db, CONSUMERS, payload)

        assert str(existing.consumer_workspace_id) == OTHER_WS_ID

    @pytest.mark.replication_matrix("workspace_remote_state_consumers", "delete")
    async def test_revoking_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, CONSUMERS, ID_A)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix(
        "workspace_remote_state_consumers", "backfill-converges-deletion"
    )
    async def test_a_revoked_consumer_does_not_come_back(self):
        """The security case: a consumer that reappears can read state somebody
        deliberately cut it off from — the same shape as #1115, applied to state
        rather than a token."""
        db = _keys_db([(ID_A,), (ID_B,)])

        removed = await replication.reconcile_deletions(db, CONSUMERS, {ID_A})

        assert removed == [ID_B]


class TestOrdering:
    def test_everything_here_comes_after_what_it_references(self):
        order = list(replication.registered())

        assert order.index("vcs_connections") < order.index("policy_sets")
        assert order.index("policy_sets") < order.index("policies")
        for name in (
            "notification_configurations",
            "run_tasks",
            "run_triggers",
            "workspace_remote_state_consumers",
            "execution_hook_workspaces",
        ):
            assert order.index("workspaces") < order.index(name), name
        assert order.index("execution_hooks") < order.index("execution_hook_workspaces")

    def test_the_settings_scope_is_now_complete(self):
        """Everything a workspace's runs depend on replicates. What remains is
        run/artifact history and object-storage content, both later phases."""
        registered = set(replication.registered())

        assert {
            "workspaces",
            "variables",
            "variable_sets",
            "policy_sets",
            "policies",
            "notification_configurations",
            "run_tasks",
            "execution_hooks",
            "run_triggers",
            "workspace_remote_state_consumers",
        } <= registered
