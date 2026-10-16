"""The run spine for a configure operation (#1988, #1973).

A configure is a `Run` carrying `is_configure`, not its own object type (#1974,
settled 2026-10-16), so the spine is a vocabulary rather than a new lifecycle:
a flag, a `configuring` status between `applying` and `applied`, the timestamps
that make it legible, and a fold so the frozen TFE surface can still represent
a run that is in it.

**Nothing can create a configure run yet, deliberately** -- #1974 owns
triggering and #1972 owns executing -- so these tests drive the machinery
directly rather than end to end. That is the honest boundary and it is stated
here so a reader does not go looking for a dispatch test that would be lying.

The gates matter more than the behavioural tests, because the defect this
feature invites is DRIFT: four separate places independently decided what a
phase is, and a fifth derived one from two statuses. Every one of them was
correct while there were two phases.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.routers import runs as runs_router
from terrapod.api.routers.runs import _apply_status, _reported_run_status, _run_json
from terrapod.auth.runner_tokens import RUNNER_PHASES
from terrapod.services import run_service
from terrapod.services.run_reconciler import _phase_of
from terrapod.services.run_service import (
    LIVE_STATES_FOR_PHASE,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    PlanOnlyConfigureConflict,
    refuse_plan_only_configure,
)
from terrapod.storage.keys import apply_log_key, configure_log_key, plan_log_key

NOW = datetime.now(UTC)


def _run(status="configuring", **kw):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.workspace_id = uuid.uuid4()
    # Every timestamp explicit: an UNSET MagicMock attribute is TRUTHY, and
    # `_apply_status` now reads `configure_started_at`, so a fixture that
    # leaves it out silently reports every errored run as "apply succeeded,
    # configure failed".
    for f in (
        "plan_started_at",
        "plan_finished_at",
        "apply_started_at",
        "apply_finished_at",
        "configure_started_at",
        "configure_finished_at",
    ):
        setattr(run, f, kw.get(f))
    run.is_configure = kw.get("is_configure", False)
    run.error_message = ""
    run.source = "tfe-api"
    return run


# ───────────────────────── the fold, as a contrast ─────────────────────────


class TestConfiguringFoldsOnlyOnTheFrozenSurface:
    """The property is a RELATIONSHIP between two surfaces.

    A test that builds one of them passes whichever way the fold is written,
    including not written at all -- so every assertion here is a pair.
    """

    def test_the_same_run_reads_differently_per_surface(self):
        run = _run("configuring")
        assert _reported_run_status(run, tfe=False) == "configuring", (
            "the native API must report the real status; folding there would hide a "
            "phase the UI, provider and MCP are supposed to show"
        )
        assert _reported_run_status(run, tfe=True) == "applying", (
            "`/api/tfe/v2` is frozen and go-tfe switches on a fixed status set, so a "
            "value it does not know is treated as unrecognised"
        )

    @pytest.mark.parametrize(
        "status", ["pending", "queued", "planning", "planned", "confirmed", "applying", "applied"]
    )
    def test_every_other_status_is_identical_on_both_surfaces(self, status):
        # The fold must be exactly one entry wide. A Terraform user's run reads
        # byte-identically to before on both prefixes.
        run = _run(status)
        assert _reported_run_status(run, tfe=False) == status
        assert _reported_run_status(run, tfe=True) == status

    def test_the_serializer_folds_too(self):
        run = _run("configuring")
        native = _run_json(run, engine="terraform", tfe=False)["data"]["attributes"]["status"]
        tfe = _run_json(run, engine="terraform", tfe=True)["data"]["attributes"]["status"]
        assert (native, tfe) == ("configuring", "applying")

    def test_the_apply_sub_resource_stays_running_through_a_configure(self):
        """The concatenation design, and not an approximation.

        From the CLI's side the configure IS part of the apply, so the apply
        reads `running` until the whole run is terminal. `_apply_status` ends
        in `return s`, so an unlisted status would be handed to go-tfe
        verbatim -- and `configuring` is not a value it can switch on.
        """
        assert _apply_status(_run("configuring", apply_started_at=NOW)) == "running"


class TestTheFoldIsPassedAtEveryCallSite:
    """`tfe` is required with no default, and this is why.

    #1536 set the precedent: `engine` is required because a default is how a
    Pulumi run came to be reported as a Terraform one. Either default here is
    wrong in a way nothing would catch -- False leaks a status go-tfe cannot
    switch on, True hides the real one from the native API.
    """

    def test_every_run_json_call_passes_tfe(self):
        tree = ast.parse(pathlib.Path(runs_router.__file__).read_text())
        missing, total = [], 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if fn.name == "_run_json":
                continue
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and "_run_json" in ast.unparse(call.func):
                    total += 1
                    if "tfe" not in {k.arg for k in call.keywords}:
                        missing.append(f"{fn.name}:{call.lineno}")
        assert total >= 8, f"found only {total} `_run_json` call sites; the gate has drifted"
        assert not missing, f"these serialize a run without saying which surface asked: {missing}"

    def test_the_two_native_callers_are_the_runner_protocol_ones(self):
        """`tfe=False` is correct for exactly two callers, and naming them is
        the point: a route handler that hardcoded False would be a silent leak
        of `configuring` onto the frozen surface."""
        tree = ast.parse(pathlib.Path(runs_router.__file__).read_text())
        hardcoded_false = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) or fn.name == "_run_json":
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and "_run_json" in ast.unparse(call.func)):
                    continue
                for k in call.keywords:
                    if k.arg == "tfe" and isinstance(k.value, ast.Constant):
                        hardcoded_false.add(fn.name)
        assert hardcoded_false == {"next_run", "update_run_status"}, (
            "only the runner-protocol endpoints may hardcode the surface; a route handler "
            f"must derive it from the request. Found: {sorted(hardcoded_false)}"
        )


# ────────────────────────────── the state machine ──────────────────────────


class TestConfiguringSitsBetweenApplyingAndApplied:
    def test_the_composite_path_exists_and_the_run_does_not_go_terminal_first(self):
        assert "configuring" in VALID_TRANSITIONS["applying"]
        assert "applied" in VALID_TRANSITIONS["configuring"]
        # The whole mechanism: `_serve_log` closes the apply stream when the
        # RUN is terminal, so a non-terminal `configuring` keeps the CLI
        # attached through the configure and delivers the composite result.
        assert "configuring" not in TERMINAL_STATES

    def test_a_standalone_configure_enters_from_queued(self):
        assert "configuring" in VALID_TRANSITIONS["queued"]

    def test_a_failed_apply_is_not_followed_by_a_configure(self):
        """#1407 point 1a, as a property of the table rather than a comment."""
        assert "configuring" not in VALID_TRANSITIONS.get("errored", set())
        assert "configuring" not in VALID_TRANSITIONS["planned"]

    def test_a_configure_in_flight_cancels_through_canceling(self):
        """Not straight to `canceled`: a configure is mutating hosts, so the
        terminal has to come from the observed Job outcome."""
        assert "canceling" in VALID_TRANSITIONS["configuring"]
        assert "canceled" not in VALID_TRANSITIONS["configuring"]
        assert "configuring" not in run_service.PRE_EXECUTION_STATES

    def test_a_configure_run_ends_in_the_runs_own_terminal_states(self):
        """#1973 decided `succeeded`/`failed`/`canceled` under the superseded
        independent-operation shape; a Run has its own vocabulary (#1974)."""
        assert VALID_TRANSITIONS["configuring"] <= (TERMINAL_STATES | {"canceling"})
        for invented in ("succeeded", "failed"):
            assert invented not in VALID_TRANSITIONS["configuring"]
            assert invented not in TERMINAL_STATES


class TestTheConfigureTimestamps:
    async def test_entering_configuring_stamps_the_start(self):
        db = AsyncMock(spec=AsyncSession)
        run = _run("applying", apply_started_at=NOW)
        await run_service.transition_run(db, run, "configuring")
        assert run.configure_started_at is not None, (
            "the start stamp is what lets `_apply_status` tell a failed apply from a "
            "failed configure, so it must be set BEFORE anything can fail"
        )
        assert run.configure_finished_at is None

    async def test_the_terminal_stamps_the_finish(self):
        db = AsyncMock(spec=AsyncSession)
        run = _run("configuring", apply_started_at=NOW, configure_started_at=NOW)
        await run_service.transition_run(db, run, "applied")
        assert run.configure_finished_at is not None

    async def test_a_run_with_no_configure_gets_neither_stamp(self):
        """The contrast: a terraform/tofu run is untouched by any of this."""
        db = AsyncMock(spec=AsyncSession)
        run = _run("applying", apply_started_at=NOW)
        await run_service.transition_run(db, run, "applied")
        assert run.configure_started_at is None
        assert run.configure_finished_at is None


# ───────────────────────── one definition of a phase ───────────────────────


class TestEveryPlaceThatDecidesWhatAPhaseIsAgrees:
    """The drift this feature invites, gated rather than hoped for.

    Four places independently enumerated the phases and a fifth derived one
    from two statuses. Every one was correct while there were two.
    """

    def test_the_token_phases_and_the_live_states_are_the_same_set(self):
        assert set(RUNNER_PHASES) == set(LIVE_STATES_FOR_PHASE), (
            "a phase a token can claim but that has no live state fails every check that "
            "asks whether the token is still valid; the reverse mints nothing"
        )

    def test_the_reconciler_derives_the_phase_rather_than_restating_it(self):
        for phase, states in LIVE_STATES_FOR_PHASE.items():
            for status in states:
                if status == "confirmed":
                    continue  # pre-claim; no Job exists to reconcile
                assert _phase_of(status) == phase, (
                    f"the reconciler maps {status!r} to {_phase_of(status)!r}, not {phase!r} -- "
                    "a configure Job read as an apply fetches the wrong log and revokes "
                    "leases under the wrong record"
                )

    def test_the_reconciler_derives_no_phase_from_an_inline_conditional(self):
        """An AST check, not a text match.

        A text match on the old expression finds `_phase_of`'s own docstring,
        which quotes it as history -- the recorded trap where the comment
        explaining a removal defeats the gate against it coming back. So this
        looks for the SHAPE: a conditional whose two arms are phase names.
        """
        from terrapod.services import run_reconciler

        tree = ast.parse(pathlib.Path(run_reconciler.__file__).read_text())
        phases = set(LIVE_STATES_FOR_PHASE)
        offenders = [
            f"line {n.lineno}: {ast.unparse(n)}"
            for n in ast.walk(tree)
            if isinstance(n, ast.IfExp)
            and {
                getattr(n.body, "value", None),
                getattr(n.orelse, "value", None),
            }
            <= phases
        ]
        assert not offenders, (
            "a phase is being derived from an inline conditional rather than from "
            f"`LIVE_STATES_FOR_PHASE`, so a third phase is invisible to it: {offenders}"
        )

    def test_the_vault_lease_service_knows_every_phase(self):
        from terrapod.services.vault_lease_service import _PHASES

        assert set(_PHASES) == set(LIVE_STATES_FOR_PHASE), (
            "a phase missing here is not refused -- its leases are simply never revoked "
            "when the Job ends, which is the quiet direction"
        )

    def test_the_runner_config_accepts_every_phase(self):
        import dataclasses
        from typing import get_args, get_type_hints

        from terrapod.runner.runner_config import RunnerConfig

        # A frozen dataclass, so the Literal comes off the resolved type hints
        # rather than pydantic's `model_fields`.
        field = next(f for f in dataclasses.fields(RunnerConfig) if f.name == "phase")
        declared = set(get_args(get_type_hints(RunnerConfig)[field.name]))
        assert declared == set(LIVE_STATES_FOR_PHASE), (
            f"the runner accepts {sorted(declared)} but the state machine has "
            f"{sorted(LIVE_STATES_FOR_PHASE)}"
        )

    def test_there_is_a_log_key_per_phase(self):
        keys = {
            "plan": plan_log_key("ws", "run"),
            "apply": apply_log_key("ws", "run"),
            "configure": configure_log_key("ws", "run"),
        }
        assert set(keys) == set(LIVE_STATES_FOR_PHASE)
        assert len(set(keys.values())) == 3, f"two phases share a log key: {keys}"


# ─────────────────────────── plan_only is refused ──────────────────────────


class TestPlanOnlyIsRefusedOnAConfigureRun:
    @pytest.mark.parametrize(
        ("plan_only", "is_configure", "refused"),
        [
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (True, True, True),
        ],
    )
    def test_only_the_combination_is_refused(self, plan_only, is_configure, refused):
        """A contrast across all four states, because refusing too much is the
        other failure: `plan_only` alone is the ordinary speculative plan every
        pull request makes, and refusing it would break every one of them."""
        if refused:
            with pytest.raises(PlanOnlyConfigureConflict):
                refuse_plan_only_configure(plan_only=plan_only, is_configure=is_configure)
        else:
            refuse_plan_only_configure(plan_only=plan_only, is_configure=is_configure)

    def test_it_subclasses_value_error(self):
        """So every HTTP surface that already turns a ValueError into a 4xx
        keeps working without learning a new type."""
        assert issubclass(PlanOnlyConfigureConflict, ValueError)

    def test_the_chokepoint_calls_it(self):
        """`create_run` is the only `Run` constructor on the live path, so one
        check there cannot be routed around -- the #2116 shape."""
        src = pathlib.Path(run_service.__file__).read_text()
        fn = next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_run"
        )
        calls = [
            c
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and "refuse_plan_only_configure" in ast.unparse(c.func)
        ]
        assert len(calls) == 1, "create_run does not refuse the combination exactly once"
        passed = {k.arg for k in calls[0].keywords}
        assert passed == {"plan_only", "is_configure"}, (
            f"the rule is handed {sorted(passed)}; it needs both options to decide"
        )
