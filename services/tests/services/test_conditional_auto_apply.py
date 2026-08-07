"""Conditional auto-apply (#1274).

The unit under test is the decision: given a mode and a plan shape, does the
run apply itself or wait for a human? Every ambiguous case must resolve to
"wait" — this is the one place in the product where getting it wrong applies
infrastructure changes nobody looked at.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import run_service
from terrapod.services.run_service import (
    AUTO_APPLY_MODES,
    describe_plan_shape,
    plan_shape_permits_auto_apply,
    resolve_auto_apply_mode,
)


def _run(additions=0, changes=0, destructions=0, replacements=0):
    return SimpleNamespace(
        resource_additions=additions,
        resource_changes=changes,
        resource_destructions=destructions,
        resource_replacements=replacements,
    )


class TestResolveMode:
    """`auto_apply` and `auto_apply_mode` can disagree across a rolling
    upgrade. The composition must always err towards less automation."""

    @pytest.mark.parametrize(
        ("auto_apply", "mode", "expected"),
        [
            # The historical boolean on its own.
            (False, "never", "never"),
            (True, "never", "always"),
            # Conditional modes.
            (True, "create", "create"),
            (True, "create_update", "create_update"),
            # An old replica clearing the boolean must win: less, never more.
            (False, "create", "never"),
            (False, "create_update", "never"),
            (False, "always", "never"),
            # A value written by a newer replica this code doesn't know:
            # fall back to the boolean's own meaning rather than guessing.
            (True, "some_future_mode", "always"),
            (True, None, "always"),
        ],
    )
    def test_composition(self, auto_apply, mode, expected):
        obj = SimpleNamespace(auto_apply=auto_apply, auto_apply_mode=mode)
        assert resolve_auto_apply_mode(obj) == expected

    def test_object_without_the_column_at_all(self):
        # Defensive: a stale/partial object must not raise, and must not
        # invent an auto-apply.
        assert resolve_auto_apply_mode(SimpleNamespace(auto_apply=False)) == "never"
        assert resolve_auto_apply_mode(SimpleNamespace()) == "never"


class TestPlanShape:
    def test_create_allows_pure_additions(self):
        assert plan_shape_permits_auto_apply(_run(additions=7), "create") is True

    def test_create_refuses_an_in_place_update(self):
        assert plan_shape_permits_auto_apply(_run(additions=1, changes=1), "create") is False

    def test_create_update_allows_additions_and_updates(self):
        run = _run(additions=3, changes=4)
        assert plan_shape_permits_auto_apply(run, "create_update") is True

    @pytest.mark.parametrize("mode", ["create", "create_update"])
    def test_destroy_always_refuses(self, mode):
        assert plan_shape_permits_auto_apply(_run(destructions=1), mode) is False

    @pytest.mark.parametrize("mode", ["create", "create_update"])
    def test_replacement_always_refuses(self, mode):
        # The one that a naive "no destroys" check misses: a replace is the
        # {create, delete} action pair and is counted as a replacement, NOT a
        # destruction, so it would sail straight through.
        run = _run(additions=1, replacements=1, destructions=0)
        assert plan_shape_permits_auto_apply(run, mode) is False

    def test_empty_plan_is_permitted(self):
        # A zero-change plan never reaches here in practice (complete_plan
        # short-circuits it to `applied`), but "nothing to do" is trivially
        # within any standard.
        assert plan_shape_permits_auto_apply(_run(), "create") is True

    @pytest.mark.parametrize("mode", ["never", "always", "nonsense"])
    def test_non_conditional_modes_are_not_this_function_s_business(self, mode):
        assert plan_shape_permits_auto_apply(_run(additions=1), mode) is None


class TestUnknownShapeFailsClosed:
    """Null counts mean the plan JSON never landed or didn't parse. That is
    NOT a pass — it is exactly the run a human should look at."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"additions": None},
            {"changes": None},
            {"destructions": None},
            {"replacements": None},
        ],
    )
    @pytest.mark.parametrize("mode", ["create", "create_update"])
    def test_any_null_count_is_undecidable(self, kwargs, mode):
        run = _run(**kwargs)
        assert plan_shape_permits_auto_apply(run, mode) is None

    def test_all_counts_null(self):
        run = SimpleNamespace(
            resource_additions=None,
            resource_changes=None,
            resource_destructions=None,
            resource_replacements=None,
        )
        assert plan_shape_permits_auto_apply(run, "create") is None


class TestDeclinedReason:
    def test_names_what_blocked_it(self):
        reason = describe_plan_shape(_run(destructions=2, replacements=1))
        assert "2 destroys" in reason
        assert "1 replace" in reason

    def test_singular_and_plural(self):
        assert describe_plan_shape(_run(destructions=1)) == "1 destroy"
        assert describe_plan_shape(_run(destructions=3)) == "3 destroys"

    def test_update_named_for_create_mode(self):
        assert "1 update" in describe_plan_shape(_run(changes=1))

    def test_no_counts_says_so_rather_than_lying(self):
        run = SimpleNamespace(
            resource_destructions=None, resource_replacements=None, resource_changes=None
        )
        assert describe_plan_shape(run) == "plan shape unavailable"


def test_mode_list_is_the_documented_set():
    # The API validates against this tuple and the docs enumerate it; a
    # silent addition would be accepted by the API but understood by nothing.
    assert AUTO_APPLY_MODES == ("never", "always", "create", "create_update")


# ── The decider, not just the decision (#1294) ─────────────────────────────
#
# Everything above tests the pure predicates. These test the function that
# *acts* on them — which had no coverage at all, and which is where the
# release review found three of its four blockers. A green predicate suite
# proved the decision was right while the decider was entirely unexercised.


def _decider_run(**kw):
    """A `planned`, apply-capable run on a conditional mode."""
    base = {
        "id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "status": "planned",
        "plan_only": False,
        # what create_run sets for any non-`never` mode
        "auto_apply": True,
        "auto_apply_mode": "create",
        "auto_apply_declined_reason": None,
        "resource_additions": 1,
        "resource_changes": 0,
        "resource_destructions": 0,
        "resource_replacements": 0,
        "has_changes": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestEvaluateConditionalAutoApply:
    async def test_a_permitted_plan_is_confirmed(self):
        run = _decider_run()
        db = AsyncMock()
        with patch.object(
            run_service, "_auto_apply_if_permitted", AsyncMock(return_value=run)
        ) as gate:
            await run_service.evaluate_conditional_auto_apply(db, run)
        gate.assert_awaited_once()

    async def test_a_destroy_is_declined_and_the_reason_recorded(self):
        # `create` must never apply a plan that destroys. The reason is what
        # the UI renders to explain why the run is sitting there.
        run = _decider_run(resource_destructions=2)
        db = AsyncMock()
        with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
            await run_service.evaluate_conditional_auto_apply(db, run)
        gate.assert_not_awaited()
        assert run.status == "planned"
        assert run.auto_apply_declined_reason, "the operator needs to know why it stopped"

    async def test_an_unknown_plan_shape_is_declined(self):
        # Plan JSON never arrived / failed to parse — fail toward "a human
        # looks at it", never toward applying something unexamined.
        run = _decider_run(
            resource_additions=None, resource_changes=None, resource_destructions=None
        )
        db = AsyncMock()
        with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
            await run_service.evaluate_conditional_auto_apply(db, run)
        gate.assert_not_awaited()
        assert run.status == "planned"

    async def test_a_run_that_is_not_planned_is_left_alone(self):
        # Without this guard a run already applying/errored/discarded would be
        # driven to `confirmed` a second time.
        for status in ("planning", "applying", "errored", "discarded", "applied"):
            run = _decider_run(status=status)
            db = AsyncMock()
            with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
                await run_service.evaluate_conditional_auto_apply(db, run)
            gate.assert_not_awaited(), f"status={status} must not auto-apply"

    async def test_a_plan_only_run_never_auto_applies(self):
        run = _decider_run(plan_only=True)
        db = AsyncMock()
        with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
            await run_service.evaluate_conditional_auto_apply(db, run)
        gate.assert_not_awaited()

    async def test_always_and_never_do_not_travel_this_path(self):
        # `always` decides in complete_plan (on the plan-result POST, before
        # the counts exist); `never` decides nowhere. Only the conditional
        # modes are settled here.
        for mode in ("always", "never"):
            run = _decider_run(auto_apply_mode=mode)
            db = AsyncMock()
            with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
                await run_service.evaluate_conditional_auto_apply(db, run)
            gate.assert_not_awaited(), f"mode={mode} must not decide here"


class TestAutoApplyGuardsAreSharedByBothEntryPoints:
    """`_auto_apply_if_permitted` was extracted so `always` and the
    conditional modes cannot drift apart on the guards. Nothing pinned that
    the new caller actually honours them — which is the whole point of the
    extraction."""

    async def test_a_manual_workspace_lock_blocks_the_conditional_path(self):
        # An unattended apply past an operator's explicit lock is exactly the
        # failure the lock exists to prevent.
        run = _decider_run()
        ws = SimpleNamespace(id=run.workspace_id, locked=True, lock_id="held-by-a-human")
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        with (
            patch.object(run_service, "_staleness_reason", AsyncMock(return_value=None)),
            patch.object(run_service, "transition_run", AsyncMock()) as transition,
        ):
            await run_service._auto_apply_if_permitted(db, run)
        transition.assert_not_awaited(), "must not confirm past a manual lock"

    async def test_an_unlocked_workspace_is_confirmed(self):
        run = _decider_run()
        ws = SimpleNamespace(id=run.workspace_id, locked=False, lock_id=None)
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        with (
            patch.object(run_service, "_staleness_reason", AsyncMock(return_value=None)),
            patch.object(run_service, "transition_run", AsyncMock(return_value=run)) as transition,
        ):
            await run_service._auto_apply_if_permitted(db, run)
        assert transition.await_args.args[2] == "confirmed"

    async def test_a_stale_plan_is_discarded_not_applied(self):
        # #646/#647: state moved or the TTL lapsed between plan and apply.
        run = _decider_run()
        ws = SimpleNamespace(id=run.workspace_id, locked=False, lock_id=None)
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        with (
            patch.object(
                run_service, "_staleness_reason", AsyncMock(return_value="state moved on")
            ),
            patch.object(run_service, "discard_run", AsyncMock(return_value=run)) as discard,
            patch.object(run_service, "transition_run", AsyncMock()) as transition,
        ):
            await run_service._auto_apply_if_permitted(db, run)
        discard.assert_awaited_once()
        transition.assert_not_awaited(), "a stale plan must never be applied"
