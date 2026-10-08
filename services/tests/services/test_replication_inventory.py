"""Replication of the ansible inventory (#1967, #1968).

Eight classes, and what a failover loses without them is a **target set**, not
a feature. An ansible configure runs against the hosts the inventory resolves
to, so a promoted node missing a host, a group membership or a group variable
does not fail loudly -- it resolves a SMALLER inventory and configures fewer
machines than the operator asked for, reporting success.

That is precisely the outcome the resolution path is built to avoid: it fails
closed rather than answering with a partial host set, because a configure
targeting too little is worse than no answer. Losing these rows would
reintroduce it on the far side of a promotion, where nobody is looking.

Three of the eight carry an `EncryptedText` value, because a host variable is
an ordinary place for an `ansible_become_password`. The key that protects it
does not go with them -- see `test_replication_node_local.py` for that rule.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.crypto.types import EncryptedText
from terrapod.db.models import (
    InventoryGlobalVar,
    InventoryGroup,
    InventoryGroupChild,
    InventoryGroupVar,
    InventoryHost,
    InventoryHostGroup,
    InventoryHostVar,
    InventorySettings,
)
from terrapod.services import replication, replication_registry

SETTINGS = replication_registry.INVENTORY_SETTINGS
HOSTS = replication_registry.INVENTORY_HOSTS
GROUPS = replication_registry.INVENTORY_GROUPS
HOST_GROUPS = replication_registry.INVENTORY_HOST_GROUPS
GROUP_CHILDREN = replication_registry.INVENTORY_GROUP_CHILDREN
HOST_VARS = replication_registry.INVENTORY_HOST_VARS
GROUP_VARS = replication_registry.INVENTORY_GROUP_VARS
GLOBAL_VARS = replication_registry.INVENTORY_GLOBAL_VARS

WS_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
OTHER_WS_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
VCS_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
HOST_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_HOST_ID = uuid.UUID("1a1a1a1a-1a1a-1a1a-1a1a-1a1a1a1a1a1a")
GROUP_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
CHILD_GROUP_ID = uuid.UUID("2b2b2b2b-2b2b-2b2b-2b2b-2b2b2b2b2b2b")
LINK_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NEST_ID = uuid.UUID("3c3c3c3c-3c3c-3c3c-3c3c-3c3c3c3c3c3c")
VAR_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
OTHER_VAR_ID = uuid.UUID("9d9d9d9d-9d9d-9d9d-9d9d-9d9d9d9d9d9d")

#: A become password is the realistic secret in a host variable, which is why
#: all three variable columns are encrypted at rest.
SECRET = "correct-horse-battery-staple"


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


def _settings(**kw):
    base = {
        "workspace_id": WS_ID,
        "include_platform": True,
        "vcs_connection_id": VCS_ID,
        "repo_url": "https://example.invalid/org/ansible",
        "branch": "main",
        "working_directory": "inventory",
        "ignore_paths": ["archive/"],
        **_stamps(),
    }
    base.update(kw)
    return InventorySettings(**base)


def _host(host_id=HOST_ID, **kw):
    base = {"id": host_id, "workspace_id": WS_ID, "name": "web-01", **_stamps()}
    base.update(kw)
    return InventoryHost(**base)


def _group(group_id=GROUP_ID, **kw):
    base = {"id": group_id, "workspace_id": WS_ID, "name": "web", **_stamps()}
    base.update(kw)
    return InventoryGroup(**base)


def _membership(link_id=LINK_ID, **kw):
    base = {
        "id": link_id,
        "workspace_id": WS_ID,
        "host_id": HOST_ID,
        "group_id": GROUP_ID,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return InventoryHostGroup(**base)


def _nesting(link_id=NEST_ID, **kw):
    base = {
        "id": link_id,
        "workspace_id": WS_ID,
        "parent_group_id": GROUP_ID,
        "child_group_id": CHILD_GROUP_ID,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return InventoryGroupChild(**base)


def _host_var(var_id=VAR_ID, **kw):
    base = {
        "id": var_id,
        "workspace_id": WS_ID,
        "host_id": HOST_ID,
        "key": "ansible_become_password",
        "value": SECRET,
        "structured": False,
        "sensitive": True,
        **_stamps(),
    }
    base.update(kw)
    return InventoryHostVar(**base)


def _group_var(var_id=VAR_ID, **kw):
    base = {
        "id": var_id,
        "workspace_id": WS_ID,
        "group_id": GROUP_ID,
        "key": "http_port",
        "value": "8080",
        "structured": True,
        "sensitive": False,
        **_stamps(),
    }
    base.update(kw)
    return InventoryGroupVar(**base)


def _global_var(var_id=VAR_ID, **kw):
    base = {
        "id": var_id,
        "workspace_id": WS_ID,
        "key": "ansible_python_interpreter",
        "value": "/usr/bin/python3",
        "structured": False,
        "sensitive": False,
        **_stamps(),
    }
    base.update(kw)
    return InventoryGlobalVar(**base)


class TestRegistration:
    """The two structural facts a reader would otherwise have to derive."""

    def test_the_settings_replicate_on_the_workspace_not_a_surrogate_id(self):
        """One inventory per workspace, so the workspace IS the key.

        A spec that assumed `id` would encode every settings row's entity id as
        `None` and apply nothing -- and the failure would be an empty inventory
        configuration on the promoted node rather than an error.
        """
        assert SETTINGS.pk_attrs == ("workspace_id",)

    def test_the_eight_are_registered_after_the_workspace_they_hang_off(self):
        """A follower applies a backfill page in registration order, so a child
        whose parent is not there yet fails its insert.

        Asserted on the registry's own ordering rather than on the source text:
        reordering the `register(...)` calls is exactly the edit that would
        break this, and it would leave the file reading perfectly sensibly.
        """
        names = list(replication.registered())
        inventory = [
            "inventory_settings",
            "inventory_hosts",
            "inventory_groups",
            "inventory_host_groups",
            "inventory_group_children",
            "inventory_host_vars",
            "inventory_group_vars",
            "inventory_global_vars",
        ]
        positions = [names.index(name) for name in inventory]
        # The eight among themselves: settings, then the two named entities,
        # then the links that join them, then the variables that hang off them.
        assert positions == sorted(positions), dict(zip(inventory, positions, strict=True))

        # And all eight after the two classes they reference. Asserted against
        # the MAXIMUM rather than in one sorted list, because `vcs_connections`
        # is registered well before `workspaces` and a single ordering would
        # read as a constraint between those two that does not exist.
        parents = max(names.index("workspaces"), names.index("vcs_connections"))
        assert positions[0] > parents, (
            f"inventory_settings is at {positions[0]}, before "
            f"workspaces/vcs_connections at {parents}"
        )


class TestInventorySettings:
    @pytest.mark.replication_matrix("inventory_settings", "backfill-from-empty")
    async def test_backfill_carries_the_binding_and_both_narrowing_fields(self):
        """`include_platform` and `ignore_paths` are the two that look like
        node-local operational state and are not.

        `include_platform: False` means "resolve the VCS source alone", which is
        how an operator moves a repo's inventory in before declaring anything --
        a node that defaults it back to True resolves rows deliberately taken
        out of play. `ignore_paths` is part of what the source IS, so dropping
        it widens the source and hosts APPEAR, which is as wrong as hosts
        disappearing.
        """
        db = _rows_db([_settings(include_platform=False)])

        page = await replication.read_backfill(db, SETTINGS)

        assert page[0]["workspace_id"] == str(WS_ID)
        assert page[0]["include_platform"] is False
        assert page[0]["ignore_paths"] == ["archive/"]
        assert page[0]["working_directory"] == "inventory"

    @pytest.mark.replication_matrix("inventory_settings", "delta-apply")
    async def test_a_rebound_source_reaches_the_peer(self):
        db = AsyncMock()
        existing = _settings(branch="main")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, SETTINGS, {"workspace_id": str(WS_ID), "branch": "release"}
        )

        assert existing.branch == "release"

    @pytest.mark.replication_matrix("inventory_settings", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _settings()
        db.scalar.return_value = existing
        payload = replication.serialize_row(SETTINGS, existing)

        await replication.apply_upsert(db, SETTINGS, payload)

        assert existing.branch == "main"
        assert existing.include_platform is True
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_settings", "delete")
    async def test_delete_applies_on_the_workspace_id(self):
        db = AsyncMock()

        await replication.apply_delete(db, SETTINGS, str(WS_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_settings", "backfill-converges-deletion")
    async def test_an_unbound_source_does_not_come_back(self):
        """Removing the settings row is how an operator unbinds a git source.

        A row that survives a backfill re-binds it, so the promoted node fetches
        and merges a repository the leader had deliberately stopped reading.
        """
        db = _keys_db([(WS_ID,), (OTHER_WS_ID,)])

        removed = await replication.reconcile_deletions(db, SETTINGS, {str(WS_ID)})

        assert removed == [str(OTHER_WS_ID)]


class TestInventoryHosts:
    @pytest.mark.replication_matrix("inventory_hosts", "backfill-from-empty")
    async def test_backfill_carries_the_host(self):
        db = _rows_db([_host()])

        page = await replication.read_backfill(db, HOSTS)

        assert page[0]["name"] == "web-01"
        assert page[0]["workspace_id"] == str(WS_ID)

    @pytest.mark.replication_matrix("inventory_hosts", "delta-apply")
    async def test_a_rename_reaches_the_peer(self):
        """A rename is not cosmetic: `name` is ansible's `inventory_hostname`,
        so a node holding the old one targets a host that no play names."""
        db = AsyncMock()
        existing = _host(name="web-01")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, HOSTS, {"id": str(HOST_ID), "name": "web-02"})

        assert existing.name == "web-02"

    @pytest.mark.replication_matrix("inventory_hosts", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _host()
        db.scalar.return_value = existing
        payload = replication.serialize_row(HOSTS, existing)

        await replication.apply_upsert(db, HOSTS, payload)

        assert existing.name == "web-01"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_hosts", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, HOSTS, str(HOST_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_hosts", "backfill-converges-deletion")
    async def test_a_decommissioned_host_does_not_come_back(self):
        """The #1115 defect on the class where it is most visible: a host that
        returns is a machine the promoted node tries to configure and which may
        no longer exist, or may belong to someone else now."""
        db = _keys_db([(HOST_ID,), (OTHER_HOST_ID,)])

        removed = await replication.reconcile_deletions(db, HOSTS, {str(HOST_ID)})

        assert removed == [str(OTHER_HOST_ID)]


class TestInventoryGroups:
    @pytest.mark.replication_matrix("inventory_groups", "backfill-from-empty")
    async def test_backfill_carries_the_group(self):
        db = _rows_db([_group()])

        page = await replication.read_backfill(db, GROUPS)

        assert page[0]["name"] == "web"

    @pytest.mark.replication_matrix("inventory_groups", "delta-apply")
    async def test_a_rename_reaches_the_peer(self):
        """Every `--limit` an operator has written names the group by name, so a
        node holding the old one answers a limit with no hosts."""
        db = AsyncMock()
        existing = _group(name="web")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, GROUPS, {"id": str(GROUP_ID), "name": "frontend"})

        assert existing.name == "frontend"

    @pytest.mark.replication_matrix("inventory_groups", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _group()
        db.scalar.return_value = existing
        payload = replication.serialize_row(GROUPS, existing)

        await replication.apply_upsert(db, GROUPS, payload)

        assert existing.name == "web"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_groups", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, GROUPS, str(GROUP_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_groups", "backfill-converges-deletion")
    async def test_a_removed_group_does_not_come_back(self):
        db = _keys_db([(GROUP_ID,), (CHILD_GROUP_ID,)])

        removed = await replication.reconcile_deletions(db, GROUPS, {str(GROUP_ID)})

        assert removed == [str(CHILD_GROUP_ID)]


class TestInventoryHostGroups:
    """Membership. A link lost at promotion is a host in no group, so every play
    limited to that group skips it silently -- which is the shape of failure
    this whole file exists for."""

    @pytest.mark.replication_matrix("inventory_host_groups", "backfill-from-empty")
    async def test_backfill_carries_both_ends(self):
        """Both ends, and not swapped. A link is only meaningful as a pair, and
        a serializer that dropped one would leave the row looking present."""
        db = _rows_db([_membership()])

        page = await replication.read_backfill(db, HOST_GROUPS)

        assert page[0]["host_id"] == str(HOST_ID)
        assert page[0]["group_id"] == str(GROUP_ID)
        assert page[0]["workspace_id"] == str(WS_ID)

    @pytest.mark.replication_matrix("inventory_host_groups", "delta-apply")
    async def test_a_new_membership_is_inserted(self):
        """A link is immutable -- there is nothing to change about it that is
        not a different row -- so the delta that matters is the insert."""
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db,
            HOST_GROUPS,
            {
                "id": str(LINK_ID),
                "workspace_id": str(WS_ID),
                "host_id": str(HOST_ID),
                "group_id": str(GROUP_ID),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert added.host_id == HOST_ID
        assert added.group_id == GROUP_ID

    @pytest.mark.replication_matrix("inventory_host_groups", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _membership()
        db.scalar.return_value = existing
        payload = replication.serialize_row(HOST_GROUPS, existing)

        await replication.apply_upsert(db, HOST_GROUPS, payload)

        assert existing.host_id == HOST_ID
        assert existing.group_id == GROUP_ID
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_host_groups", "delete")
    async def test_delete_applies_and_is_the_only_way_to_unlink(self):
        db = AsyncMock()

        await replication.apply_delete(db, HOST_GROUPS, str(LINK_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_host_groups", "backfill-converges-deletion")
    async def test_a_removed_membership_does_not_come_back(self):
        """A membership that returns silently WIDENS a play: the host is
        configured by a group it was taken out of, which is the opposite-signed
        version of the same defect and just as invisible."""
        db = _keys_db([(LINK_ID,), (NEST_ID,)])

        removed = await replication.reconcile_deletions(db, HOST_GROUPS, {str(LINK_ID)})

        assert removed == [str(NEST_ID)]


class TestInventoryGroupChildren:
    @pytest.mark.replication_matrix("inventory_group_children", "backfill-from-empty")
    async def test_backfill_carries_parent_and_child_the_right_way_round(self):
        """Direction is load-bearing and a swap is silent: ansible resolves
        membership downwards, so an inverted nesting gives the child the
        parent's hosts instead."""
        db = _rows_db([_nesting()])

        page = await replication.read_backfill(db, GROUP_CHILDREN)

        assert page[0]["parent_group_id"] == str(GROUP_ID)
        assert page[0]["child_group_id"] == str(CHILD_GROUP_ID)

    @pytest.mark.replication_matrix("inventory_group_children", "delta-apply")
    async def test_a_new_nesting_is_inserted(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db,
            GROUP_CHILDREN,
            {
                "id": str(NEST_ID),
                "workspace_id": str(WS_ID),
                "parent_group_id": str(GROUP_ID),
                "child_group_id": str(CHILD_GROUP_ID),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

        added = db.add.call_args.args[0]
        assert added.parent_group_id == GROUP_ID
        assert added.child_group_id == CHILD_GROUP_ID

    @pytest.mark.replication_matrix("inventory_group_children", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _nesting()
        db.scalar.return_value = existing
        payload = replication.serialize_row(GROUP_CHILDREN, existing)

        await replication.apply_upsert(db, GROUP_CHILDREN, payload)

        assert existing.parent_group_id == GROUP_ID
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_group_children", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, GROUP_CHILDREN, str(NEST_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_group_children", "backfill-converges-deletion")
    async def test_a_removed_nesting_does_not_come_back(self):
        """A parent reaches its child's hosts transitively, so a nesting that
        returns reconnects a whole subtree an operator detached."""
        db = _keys_db([(NEST_ID,), (LINK_ID,)])

        removed = await replication.reconcile_deletions(db, GROUP_CHILDREN, {str(NEST_ID)})

        assert removed == [str(LINK_ID)]


class TestTheThreeVariableClassesAreEncryptedOnBothSides:
    @pytest.mark.replication_matrix("inventory_host_vars", "encrypted-columns")
    @pytest.mark.replication_matrix("inventory_group_vars", "encrypted-columns")
    @pytest.mark.replication_matrix("inventory_global_vars", "encrypted-columns")
    def test_the_value_column_is_encrypted_at_rest(self):
        """One assertion for all three, because they are the same column.

        `sensitive` is a separate DISPLAY flag and does not gate this: a column
        cannot be conditionally encrypted, so the two are orthogonal and a
        non-sensitive group variable is encrypted just the same.
        """
        from sqlalchemy import inspect as sa_inspect

        for model in (InventoryHostVar, InventoryGroupVar, InventoryGlobalVar):
            encrypted = {
                col.key
                for col in sa_inspect(model).column_attrs
                if isinstance(col.expression.type, EncryptedText)
            }
            assert encrypted == {"value"}, model.__name__

    @pytest.mark.replication_matrix("inventory_host_vars", "delta-apply")
    async def test_applying_writes_back_through_the_encrypted_column(self):
        """So the receiving node re-encrypts under its OWN key.

        This is the per-node path the whole encryption design rests on: the key
        never travels, so a value arrives decrypted and is re-encrypted on
        write. Setting the attribute is what invokes the type; bypassing it with
        a bulk statement would store plaintext.
        """
        db = AsyncMock()
        existing = _host_var(value="stale")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, HOST_VARS, {"id": str(VAR_ID), "value": SECRET})

        assert existing.value == SECRET


class TestInventoryHostVars:
    @pytest.mark.replication_matrix("inventory_host_vars", "backfill-from-empty")
    async def test_backfill_carries_the_variable_and_both_flags(self):
        """`structured` decides whether the value is read as source or as text,
        so a node that loses it hands ansible a different value than the leader
        would -- the quiet failure, not the loud one."""
        db = _rows_db([_host_var(structured=True, sensitive=False)])

        page = await replication.read_backfill(db, HOST_VARS)

        assert page[0]["key"] == "ansible_become_password"
        assert page[0]["host_id"] == str(HOST_ID)
        assert page[0]["structured"] is True
        assert page[0]["sensitive"] is False

    @pytest.mark.replication_matrix("inventory_host_vars", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _host_var()
        db.scalar.return_value = existing
        payload = replication.serialize_row(HOST_VARS, existing)

        await replication.apply_upsert(db, HOST_VARS, payload)

        assert existing.value == SECRET
        assert existing.sensitive is True
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_host_vars", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, HOST_VARS, str(VAR_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_host_vars", "backfill-converges-deletion")
    async def test_a_removed_credential_does_not_come_back(self):
        """A become password that returns to life is the #1115 defect on
        material someone deliberately revoked -- and here it comes back as
        something ansible will actually authenticate with."""
        db = _keys_db([(VAR_ID,), (OTHER_VAR_ID,)])

        removed = await replication.reconcile_deletions(db, HOST_VARS, {str(VAR_ID)})

        assert removed == [str(OTHER_VAR_ID)]


class TestInventoryGroupVars:
    @pytest.mark.replication_matrix("inventory_group_vars", "backfill-from-empty")
    async def test_backfill_carries_the_variable_and_its_group(self):
        db = _rows_db([_group_var()])

        page = await replication.read_backfill(db, GROUP_VARS)

        assert page[0]["key"] == "http_port"
        assert page[0]["group_id"] == str(GROUP_ID)
        assert page[0]["structured"] is True

    @pytest.mark.replication_matrix("inventory_group_vars", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _group_var()
        db.scalar.return_value = existing
        payload = replication.serialize_row(GROUP_VARS, existing)

        await replication.apply_upsert(db, GROUP_VARS, payload)

        assert existing.value == "8080"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_group_vars", "delta-apply")
    async def test_a_changed_value_reaches_the_peer(self):
        """A group variable applies to every host in the group, so losing a
        change to one misconfigures a whole tier rather than one machine.

        Its own row rather than leaning on the host variable's: the write goes
        through the same `EncryptedText` column but a DIFFERENT table and a
        different parent, and a spec pointing at the wrong model would still
        pass the host test.
        """
        db = AsyncMock()
        existing = _group_var(value="8080")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, GROUP_VARS, {"id": str(VAR_ID), "value": "8443"})

        assert existing.value == "8443"

    @pytest.mark.replication_matrix("inventory_group_vars", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, GROUP_VARS, str(VAR_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_group_vars", "backfill-converges-deletion")
    async def test_a_removed_group_variable_does_not_come_back(self):
        db = _keys_db([(VAR_ID,), (OTHER_VAR_ID,)])

        removed = await replication.reconcile_deletions(db, GROUP_VARS, {str(VAR_ID)})

        assert removed == [str(OTHER_VAR_ID)]


class TestInventoryGlobalVars:
    """`group_vars/all`. Parented on the workspace, because `all` cannot BE a
    group: it is the rendered document's own root key."""

    @pytest.mark.replication_matrix("inventory_global_vars", "backfill-from-empty")
    async def test_backfill_carries_the_variable_under_all(self):
        """And it has no host or group parent, which is the thing that makes it
        a third class rather than a row in one of the other two."""
        db = _rows_db([_global_var()])

        page = await replication.read_backfill(db, GLOBAL_VARS)

        assert page[0]["key"] == "ansible_python_interpreter"
        assert page[0]["workspace_id"] == str(WS_ID)
        assert "host_id" not in page[0]
        assert "group_id" not in page[0]

    @pytest.mark.replication_matrix("inventory_global_vars", "delta-apply")
    async def test_a_changed_value_reaches_the_peer(self):
        """A variable under `all` applies to every host, so losing a change to
        one misconfigures the entire inventory rather than one machine."""
        db = AsyncMock()
        existing = _global_var(value="/usr/bin/python3")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, GLOBAL_VARS, {"id": str(VAR_ID), "value": "/usr/bin/python3.12"}
        )

        assert existing.value == "/usr/bin/python3.12"

    @pytest.mark.replication_matrix("inventory_global_vars", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _global_var()
        db.scalar.return_value = existing
        payload = replication.serialize_row(GLOBAL_VARS, existing)

        await replication.apply_upsert(db, GLOBAL_VARS, payload)

        assert existing.value == "/usr/bin/python3"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("inventory_global_vars", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, GLOBAL_VARS, str(VAR_ID))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("inventory_global_vars", "backfill-converges-deletion")
    async def test_a_removed_variable_under_all_does_not_come_back(self):
        db = _keys_db([(VAR_ID,), (OTHER_VAR_ID,)])

        removed = await replication.reconcile_deletions(db, GLOBAL_VARS, {str(VAR_ID)})

        assert removed == [str(OTHER_VAR_ID)]
