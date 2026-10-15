"""The declaration's service-layer consequences (#1966).

`plan_apply_enabled` and `configure_enabled` are ordinary settable columns and
the API tests cover the write boundary. What needs testing HERE is the one
place the change to `config_directory` could silently invert a behaviour: two
service-layer predicates used to ask "is `config_directory` non-NULL" and now
ask "is `configure_enabled` true", and the obvious-looking alternative --
testing `config_directory` for emptiness -- is wrong in the direction that
hides data.

`""` is the ordinary, and probably commonest, way to say "the configure
operation runs at the configuration version's root". So an emptiness test would
report a configure-capable workspace as having no inventory and would skip
fetching its git source: the right hosts, the right groups, and every variable
missing. That is the #1929 failure mode arriving through a new door, which is
why it gets a test rather than only a comment.

Both predicates are tested as a CONTRAST, because the property is a
relationship between two fields rather than the value of one. A single fixture
cannot tell "keyed on the boolean" from "keyed on emptiness" -- they agree on
every workspace except the one that matters.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import inventory_service, run_service, workspace_settings

pytestmark = pytest.mark.asyncio


def _ws(*, configure_enabled: bool, config_directory: str):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.configure_enabled = configure_enabled
    ws.config_directory = config_directory
    return ws


class TestHasAnythingKeysOnTheBooleanNotOnEmptiness:
    """The pair that tells the two implementations apart.

    Row counts are stubbed to zero throughout, deliberately: with rows present
    `has_anything` short-circuits before it ever reads the workspace, so a
    fixture that had any would pass under either implementation and prove
    nothing. The guard has to be reachable for the test to mean anything.
    """

    @pytest.mark.parametrize(
        "configure_enabled,config_directory,expected,because",
        [
            (
                True,
                "",
                True,
                'a configure operation rooted at the configuration version\'s root -- "" '
                "is a value, not an absence, and this is the case an emptiness test gets wrong",
            ),
            (
                True,
                "ansible",
                True,
                "a configure operation rooted at a subdirectory",
            ),
            (
                False,
                "",
                False,
                "no configure operation and nothing declared: the ordinary "
                "terraform/tofu-only workspace, which must see no inventory tab",
            ),
            (
                False,
                "ansible",
                False,
                "a stored path with the operation turned OFF -- the path is where it "
                "WOULD run, so it must not by itself produce an inventory",
            ),
        ],
    )
    async def test_the_declaration_decides(
        self, configure_enabled, config_directory, expected, because
    ):
        db = AsyncMock()
        ws = _ws(configure_enabled=configure_enabled, config_directory=config_directory)
        with (
            patch.object(inventory_service, "count_hosts", AsyncMock(return_value=0)),
            patch.object(inventory_service, "count_groups", AsyncMock(return_value=0)),
            patch.object(inventory_service, "get_inventory_workspace", AsyncMock(return_value=ws)),
        ):
            got = await inventory_service.has_anything(db, ws.id)
        assert got is expected, f"{because}: got {got}, want {expected}"

    async def test_declared_rows_alone_are_enough(self):
        """A workspace with hosts has an inventory whatever it declares.

        The rows are the inventory; the boolean says whether a configure
        operation consumes it. Keeping these separate is what lets an operator
        declare inventory from Terraform before turning the operation on.
        """
        db = AsyncMock()
        with (
            patch.object(inventory_service, "count_hosts", AsyncMock(return_value=3)),
            patch.object(inventory_service, "count_groups", AsyncMock(return_value=0)),
            patch.object(
                inventory_service,
                "get_inventory_workspace",
                AsyncMock(side_effect=AssertionError("short-circuited before the workspace read")),
            ),
        ):
            assert await inventory_service.has_anything(db, uuid.uuid4()) is True


class TestThePlanApplyDeclarationIsEnforcedNotJustStored:
    """`require_plan_apply_operation` is the enforcement twin of
    `validate_operations` (#2116).

    #1966 shipped the declaration and nothing on the run path read it, so a
    workspace declaring `plan_apply_enabled=False` was accepted by every write
    surface and ignored by all fifteen run creators. These are a CONTRAST
    rather than two independent cases: the property is a relationship between
    the two states of one column, so a fixture holding only one of them passes
    whichever way the guard is written -- including not written at all.
    """

    async def test_a_workspace_declaring_no_plan_apply_is_refused(self):
        ws = MagicMock()
        ws.plan_apply_enabled = False
        with pytest.raises(workspace_settings.PlanApplyNotEnabled) as exc:
            workspace_settings.require_plan_apply_operation(ws)
        # The message has to name the column an operator would go and change.
        assert "plan-apply-enabled" in str(exc.value)

    async def test_a_workspace_declaring_plan_apply_is_allowed(self):
        ws = MagicMock()
        ws.plan_apply_enabled = True
        assert workspace_settings.require_plan_apply_operation(ws) is None

    async def test_the_refusal_is_a_value_error(self):
        """So a caller using a bare `except ValueError` still catches it, which
        is the `LabelValidationError` precedent -- and so the HTTP surfaces
        that already map `ValueError` to 422 keep working."""
        assert issubclass(workspace_settings.PlanApplyNotEnabled, ValueError)

    async def test_configure_enabled_is_not_consulted(self):
        """Configure operations do not exist yet (#1972), so a workspace
        declaring one has nothing to run and nothing to refuse. If this starts
        failing, something has taught the plan/apply guard about configure and
        the two concerns have been conflated."""
        for configure in (True, False):
            ws = MagicMock()
            ws.plan_apply_enabled = True
            ws.configure_enabled = configure
            assert workspace_settings.require_plan_apply_operation(ws) is None


class TestTheChokepointRefusesRatherThanBuildingTheRow:
    async def test_create_run_refuses_a_configure_only_workspace(self):
        """Driven through `create_run` itself, not through the guard: the fix is
        'the chokepoint now calls the rule', so the test has to be the call."""
        ws = MagicMock()
        ws.plan_apply_enabled = False
        db = AsyncMock()
        with patch(
            "terrapod.services.run_service.ha_role.ensure_leader",
            new_callable=AsyncMock,
        ):
            with pytest.raises(run_service.PlanApplyNotEnabled):
                await run_service.create_run(db, workspace=ws)
        # Nothing was written: the refusal comes before the row is built.
        db.add.assert_not_called()
