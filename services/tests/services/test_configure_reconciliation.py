"""The reconciler supervises a configure, and its clock reads the right column.

A pure-function tier deliberately: the property is which timestamp the stale
check measures from, and that is decided by a mapping rather than by any
database behaviour. The wrong answer here does not raise -- it silently either
never times a run out or times a healthy one out early -- so it needs asserting
rather than exercising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from terrapod.services.run_reconciler import _phase_of, _phase_start_of

PLAN_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
APPLY_AT = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
CONFIGURE_AT = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def _run(status: str, *, plan=PLAN_AT, apply=APPLY_AT, configure=CONFIGURE_AT):
    run = MagicMock()
    run.status = status
    # Set explicitly: an absent attribute on a MagicMock answers with a distinct
    # auto-attribute, so a mapping that returned the WRONG column would still
    # compare unequal to the right one and the test would pass for no reason.
    run.plan_started_at = plan
    run.apply_started_at = apply
    run.configure_started_at = configure
    return run


class TestThePhaseIsDerivedNotGuessed:
    def test_configuring_is_the_configure_phase(self):
        """Already true via `LIVE_STATES_FOR_PHASE` (#2131); pinned because the
        reconciler's phase derivation is what names the runner token's phase."""
        assert _phase_of("configuring") == "configure"

    def test_the_other_two_are_unchanged(self):
        assert _phase_of("planning") == "plan"
        assert _phase_of("applying") == "apply"


class TestTheStaleClockReadsTheInFlightPhase:
    def test_a_configure_is_measured_from_its_own_start(self):
        assert _phase_start_of(_run("configuring")) == CONFIGURE_AT

    def test_a_standalone_configure_has_no_apply_to_borrow_from(self):
        """The defect the producer exists for. Before it, `configuring` fell to
        the `else` branch and read `apply_started_at` -- NULL on a standalone
        configure -- so the run had no timestamp to time out against and would
        sit live for ever.
        """
        assert _phase_start_of(_run("configuring", apply=None)) == CONFIGURE_AT

    def test_a_composite_configure_does_not_measure_from_its_apply(self):
        """Worse than the standalone case rather than better.

        A composite run HAS an `apply_started_at`, so the old expression would
        silently measure the configure's age from when its apply began and time
        out a healthy configure early -- a failure that looks like a slow
        playbook rather than like a clock bug.
        """
        assert _phase_start_of(_run("configuring")) != APPLY_AT

    def test_plan_and_apply_are_unchanged(self):
        assert _phase_start_of(_run("planning")) == PLAN_AT
        assert _phase_start_of(_run("applying")) == APPLY_AT

    def test_a_status_with_no_live_phase_keeps_the_previous_behaviour(self):
        """`canceling` and the terminals belong to whichever phase was in
        flight, which a status alone cannot say -- so the fallback is the apply's
        start, as it was before, rather than a new answer invented here."""
        assert _phase_start_of(_run("canceling")) == APPLY_AT
