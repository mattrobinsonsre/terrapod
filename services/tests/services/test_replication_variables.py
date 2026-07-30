"""Replication of variables and variable sets (#1138).

These are the classes that decide whether a failover works at all. A promoted
node with workspaces but no variables is terraform with no inputs and no
credentials: every run fails at plan, on every workspace, immediately. Not
degraded — dead.

The subtle failure is worse than the obvious one. Precedence is *priority set →
workspace variable → non-priority set*, so a node that carries every value but
loses `priority` hands a run a **different** value than the leader would, and
nothing anywhere reports a problem. Those two booleans get their own assertions
rather than being left implied by "the columns carry".

`TestTheKeysThemselvesNeverTravel` is the other half of the same story: these
classes carry the most secret material over the peer link, and the key that
protects it must not go with them.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.crypto.types import EncryptedText
from terrapod.db.models import (
    CryptoKey,
    Variable,
    VariableSet,
    VariableSetVariable,
    VariableSetWorkspace,
)
from terrapod.services import replication, replication_registry

VARIABLES = replication_registry.VARIABLES
SETS = replication_registry.VARIABLE_SETS
SET_VARS = replication_registry.VARIABLE_SET_VARIABLES
ASSIGNMENTS = replication_registry.VARIABLE_SET_WORKSPACES

WS_ID = "44444444-4444-4444-4444-444444444444"
OTHER_WS_ID = "55555555-5555-5555-5555-555555555555"
VAR_ID = "99999999-9999-9999-9999-999999999999"
SET_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

SECRET = "AKIAIOSFODNN7EXAMPLE"


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


def _var(var_id=VAR_ID, **kw):
    base = {
        "id": var_id,
        "workspace_id": WS_ID,
        "key": "AWS_ACCESS_KEY_ID",
        "value": SECRET,
        "category": "env",
        "sensitive": True,
        "hcl": False,
        **_stamps(),
    }
    base.update(kw)
    return Variable(**base)


def _set(set_id=SET_ID, **kw):
    base = {
        "id": set_id,
        "name": "prod-credentials",
        "description": "",
        "global_set": False,
        "priority": False,
        **_stamps(),
    }
    base.update(kw)
    return VariableSet(**base)


def _set_var(var_id=OTHER_ID, **kw):
    base = {
        "id": var_id,
        "variable_set_id": SET_ID,
        "key": "region",
        "value": "eu-west-1",
        "category": "terraform",
        "sensitive": False,
        "hcl": False,
        **_stamps(),
    }
    base.update(kw)
    return VariableSetVariable(**base)


class TestPrecedenceIsCarried:
    """The failure that reports nothing. `global_set` and `priority` decide which
    value a run actually receives — a node that carries every value but disagrees
    on either one silently runs terraform with different inputs.
    """

    async def test_priority_reaches_the_peer(self):
        """A priority set overrides the workspace's own variable. Losing the flag
        inverts that, so the workspace value wins instead — a different plan, no
        error."""
        db = AsyncMock()
        existing = _set(priority=False)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, SETS, {"id": SET_ID, "priority": True})

        assert existing.priority is True

    async def test_global_reaches_the_peer(self):
        """A global set applies to every workspace with no assignment rows at all.
        Losing the flag makes it apply to nothing."""
        db = AsyncMock()
        existing = _set(global_set=False)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, SETS, {"id": SET_ID, "global_set": True})

        assert existing.global_set is True

    def test_both_flags_are_in_the_payload(self):
        payload = replication.serialize_row(SETS, _set(global_set=True, priority=True))

        assert payload["global_set"] is True
        assert payload["priority"] is True


class TestSensitiveValuesTravelUnmasked:
    """The API masks a sensitive value on read. Replication must not: a peer that
    stored the mask would hand terraform the literal placeholder, and the run
    would fail with a credential error that points nowhere near the cause.
    """

    def test_a_workspace_variable_carries_its_real_value(self):
        payload = replication.serialize_row(VARIABLES, _var())

        assert payload["value"] == SECRET
        assert payload["sensitive"] is True

    def test_a_set_variable_carries_its_real_value(self):
        payload = replication.serialize_row(SET_VARS, _set_var(value=SECRET, sensitive=True))

        assert payload["value"] == SECRET

    @pytest.mark.replication_matrix("variables", "encrypted-columns")
    def test_the_value_column_is_encrypted_at_rest_on_both_sides(self):
        from sqlalchemy import inspect as sa_inspect

        for model in (Variable, VariableSetVariable):
            encrypted = {
                col.key
                for col in sa_inspect(model).column_attrs
                if isinstance(col.expression.type, EncryptedText)
            }
            assert encrypted == {"value"}, model.__name__

    @pytest.mark.replication_matrix("variable_set_variables", "encrypted-columns")
    async def test_applying_writes_back_through_the_encrypted_column(self):
        """So the receiving node re-encrypts under its own key. This is the same
        per-node path #1132 established, exercised on the class that carries the
        most of it."""
        db = AsyncMock()
        existing = _set_var(value="stale")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, SET_VARS, {"id": OTHER_ID, "value": SECRET})

        assert existing.value == SECRET


class TestTheKeysThemselvesNeverTravel:
    """`crypto_keys` holds this node's data-encryption key wrapped by THIS node's
    KEK. Sending it is useless to the peer — it cannot unwrap it — and it puts key
    material on a link that has no need of it.

    Per-node encryption exists precisely so the key never has to travel: values
    are decrypted on send and re-encrypted under the receiver's own key. This is
    asserted next to the classes that carry the secrets, because that is where
    someone would be tempted to "just replicate the keys too" to make it simpler.
    """

    def test_crypto_keys_is_not_registered(self):
        assert "crypto_keys" not in replication.registered(), (
            "crypto_keys must never replicate: the wrapped DEK is meaningless to "
            "the peer and putting it on the link leaks key material for nothing"
        )

    def test_no_registered_class_is_the_crypto_key_model(self):
        """Belt and braces: catches a registration under a different class name."""
        models = {spec.model for spec in replication.registered().values()}

        assert CryptoKey not in models


class TestVariables:
    @pytest.mark.replication_matrix("variables", "backfill-from-empty")
    async def test_backfill_carries_the_variable_and_its_flags(self):
        db = _rows_db([_var(hcl=True, category="terraform")])

        page = await replication.read_backfill(db, VARIABLES)

        assert page[0]["key"] == "AWS_ACCESS_KEY_ID"
        assert page[0]["category"] == "terraform"
        assert page[0]["hcl"] is True
        assert page[0]["workspace_id"] == WS_ID

    @pytest.mark.replication_matrix("variables", "delta-apply")
    async def test_a_rotated_value_reaches_the_peer(self):
        db = AsyncMock()
        existing = _var(value="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, VARIABLES, {"id": VAR_ID, "value": "new"})

        assert existing.value == "new"

    @pytest.mark.replication_matrix("variables", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _var()
        db.scalar.return_value = existing
        payload = replication.serialize_row(VARIABLES, existing)

        await replication.apply_upsert(db, VARIABLES, payload)
        await replication.apply_upsert(db, VARIABLES, payload)

        assert existing.key == "AWS_ACCESS_KEY_ID"
        assert existing.value == SECRET
        assert existing.sensitive is True

    @pytest.mark.replication_matrix("variables", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, VARIABLES, VAR_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("variables", "backfill-converges-deletion")
    async def test_a_deleted_variable_does_not_survive_a_backfill(self):
        """A credential that comes back to life is the #1115 defect again — and
        here it comes back as an input terraform will actually use."""
        db = _keys_db([(VAR_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, VARIABLES, {VAR_ID})

        assert removed == [OTHER_ID]


class TestVariableSets:
    @pytest.mark.replication_matrix("variable_sets", "backfill-from-empty")
    async def test_backfill_carries_the_set(self):
        db = _rows_db([_set()])

        page = await replication.read_backfill(db, SETS)

        assert page[0]["name"] == "prod-credentials"

    @pytest.mark.replication_matrix("variable_sets", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _set(description="")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, SETS, {"id": SET_ID, "description": "shared creds"})

        assert existing.description == "shared creds"

    @pytest.mark.replication_matrix("variable_sets", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _set()
        db.scalar.return_value = existing
        payload = replication.serialize_row(SETS, existing)

        await replication.apply_upsert(db, SETS, payload)
        await replication.apply_upsert(db, SETS, payload)

        assert existing.name == "prod-credentials"
        assert existing.priority is False

    @pytest.mark.replication_matrix("variable_sets", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, SETS, SET_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("variable_sets", "backfill-converges-deletion")
    async def test_a_deleted_set_does_not_survive_a_backfill(self):
        db = _keys_db([(SET_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, SETS, {SET_ID})

        assert removed == [OTHER_ID]


class TestVariableSetVariables:
    @pytest.mark.replication_matrix("variable_set_variables", "backfill-from-empty")
    async def test_backfill_carries_the_member_variable(self):
        db = _rows_db([_set_var()])

        page = await replication.read_backfill(db, SET_VARS)

        assert page[0]["key"] == "region"
        assert page[0]["value"] == "eu-west-1"
        assert page[0]["variable_set_id"] == SET_ID

    @pytest.mark.replication_matrix("variable_set_variables", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _set_var(value="us-east-1")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, SET_VARS, {"id": OTHER_ID, "value": "eu-west-1"})

        assert existing.value == "eu-west-1"

    @pytest.mark.replication_matrix("variable_set_variables", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _set_var()
        db.scalar.return_value = existing
        payload = replication.serialize_row(SET_VARS, existing)

        await replication.apply_upsert(db, SET_VARS, payload)
        await replication.apply_upsert(db, SET_VARS, payload)

        assert existing.key == "region"
        assert existing.value == "eu-west-1"

    @pytest.mark.replication_matrix("variable_set_variables", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, SET_VARS, OTHER_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("variable_set_variables", "backfill-converges-deletion")
    async def test_a_deleted_member_does_not_survive_a_backfill(self):
        db = _keys_db([(OTHER_ID,), (VAR_ID,)])

        removed = await replication.reconcile_deletions(db, SET_VARS, {OTHER_ID})

        assert removed == [VAR_ID]


class TestVariableSetAssignments:
    """Composite-key junction, and — like the agent-pool set — edited as a
    collection on its parent, so these rows are what record an assignment change
    rather than the set row."""

    @pytest.mark.replication_matrix("variable_set_workspaces", "backfill-from-empty")
    async def test_backfill_carries_both_sides(self):
        db = _rows_db([VariableSetWorkspace(variable_set_id=SET_ID, workspace_id=WS_ID)])

        page = await replication.read_backfill(db, ASSIGNMENTS)

        assert page[0]["variable_set_id"] == SET_ID
        assert page[0]["workspace_id"] == WS_ID

    @pytest.mark.replication_matrix("variable_set_workspaces", "delta-apply")
    async def test_an_assignment_applies(self):
        """There is nothing to update on this row beyond its key, so a delta IS
        the insert — which is exactly how an assignment reaches the peer."""
        db = AsyncMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db, ASSIGNMENTS, {"variable_set_id": SET_ID, "workspace_id": WS_ID}
        )

        db.add.assert_called_once()

    @pytest.mark.replication_matrix("variable_set_workspaces", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = VariableSetWorkspace(variable_set_id=SET_ID, workspace_id=WS_ID)
        db.scalar.return_value = existing
        payload = replication.serialize_row(ASSIGNMENTS, existing)

        await replication.apply_upsert(db, ASSIGNMENTS, payload)
        await replication.apply_upsert(db, ASSIGNMENTS, payload)

        assert str(existing.variable_set_id) == SET_ID
        assert str(existing.workspace_id) == WS_ID
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("variable_set_workspaces", "delete")
    async def test_unassigning_applies(self):
        db = AsyncMock()
        entity_id = replication.encode_entity_id(ASSIGNMENTS, [SET_ID, WS_ID])

        await replication.apply_delete(db, ASSIGNMENTS, entity_id)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("variable_set_workspaces", "backfill-converges-deletion")
    async def test_an_unassignment_converges(self):
        """Otherwise a promoted node feeds a workspace credentials somebody
        deliberately stopped giving it."""
        db = _keys_db([(SET_ID, WS_ID), (SET_ID, OTHER_WS_ID)])
        kept = replication.encode_entity_id(ASSIGNMENTS, [SET_ID, WS_ID])

        removed = await replication.reconcile_deletions(db, ASSIGNMENTS, {kept})

        assert removed == [replication.encode_entity_id(ASSIGNMENTS, [SET_ID, OTHER_WS_ID])]


class TestOrdering:
    def test_variables_come_after_workspaces(self):
        order = list(replication.registered())

        assert order.index("workspaces") < order.index("variables")

    def test_set_members_and_assignments_come_after_their_set(self):
        order = list(replication.registered())

        assert order.index("variable_sets") < order.index("variable_set_variables")
        assert order.index("variable_sets") < order.index("variable_set_workspaces")
        assert order.index("workspaces") < order.index("variable_set_workspaces")
