"""A failed apply must not be reported as an apply that never ran (#1988).

`_apply_status` derives the TFE apply sub-resource's status entirely from the
run -- there is no independent apply status column -- and its `errored` branch
read:

    if run.apply_started_at and not run.apply_finished_at:
        return "errored"
    return "unreachable"

That condition cannot be true for any run the state machine produces.
`transition_run` sets `apply_finished_at` on the very transition to `errored`,
so by the time this function reads the run the timestamp is always set, the
branch falls through, and **every failed apply reported `unreachable`** --
which in go-tfe's vocabulary means the apply never ran. The CLI was told the
apply had not happened, on the one surface an operator reads to decide whether
to re-run it, while the apply had in fact run and failed. The `errored` return
was effectively dead code.

Nothing covered this function at all before, which is how it shipped.

**The second class here is the one worth keeping.** The bug was not a typo: two
functions disagreed about what `apply_finished_at` means, and nothing coupled
them. So `TestThePremiseTheFixRestsOn` drives `transition_run` itself and pins
the timestamp behaviour that makes `apply_started_at` the right discriminator.
If that ever changes, this file fails and names the function to re-read, rather
than `_apply_status` quietly becoming wrong a second time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.routers.runs import _apply_json, _apply_status
from terrapod.services.run_service import transition_run

NOW = datetime.now(UTC)


def _run(status: str, *, started=None, finished=None, configured=None):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.apply_started_at = started
    run.apply_finished_at = finished
    # Explicit, and None by default, because an UNSET MagicMock attribute is
    # TRUTHY: without this every errored run reads as "the apply succeeded and
    # the configure failed" once `_apply_status` learned to check it (#1988),
    # and these tests silently stopped being about the branch they name.
    run.configure_started_at = configured
    return run


class TestAFailedApplyIsReportedAsFailed:
    """A contrast: the two `errored` runs differ only in whether an apply ran.

    Written as a pair deliberately. The property is a *relationship* between
    "errored with an apply" and "errored without one", and a single-state
    fixture passes whichever way the branch is written -- including the way
    that shipped, which answered `unreachable` for both.
    """

    def test_an_errored_run_whose_apply_ran_differs_from_one_whose_apply_did_not(self):
        # Exactly the state `transition_run` leaves behind: it sets
        # `apply_finished_at` on the transition to `errored`.
        apply_failed = _apply_status(_run("errored", started=NOW, finished=NOW))
        # A plan-phase failure never reaches the apply, so it has no start
        # time -- and the apply-phase unclaim (#1646) deliberately clears it.
        never_applied = _apply_status(_run("errored"))

        assert apply_failed == "errored", (
            "a failed apply reported as `unreachable` tells the CLI the apply never ran "
            "while the apply ran and failed (#1988)"
        )
        assert never_applied == "unreachable"

    def test_the_timestamp_that_used_to_decide_no_longer_does(self):
        """`apply_finished_at` must not change the answer on its own.

        The old branch keyed on its absence, so pinning that the two agree is
        what stops the condition being reintroduced.
        """
        with_finish = _apply_status(_run("errored", started=NOW, finished=NOW))
        without_finish = _apply_status(_run("errored", started=NOW, finished=None))
        assert with_finish == without_finish == "errored"

    def test_the_unaffected_statuses_are_unchanged(self):
        # The fix touches one branch; everything a Terraform user sees on a
        # normal run must be byte-identical.
        assert _apply_status(_run("applied", started=NOW, finished=NOW)) == "finished"
        assert _apply_status(_run("applying", started=NOW)) == "running"
        assert _apply_status(_run("canceling", started=NOW)) == "running"
        assert _apply_status(_run("confirmed")) == "pending"
        assert _apply_status(_run("planned")) == "unreachable"
        assert _apply_status(_run("canceled")) == "canceled"
        assert _apply_status(_run("discarded")) == "canceled"


class TestItReachesTheSerialisedPayload:
    """One layer up, because a correct helper nobody serialises is no fix.

    Not driven through the route: the wiring from `_apply_json` to both
    `/applies/{id}` handlers did not change, and the behaviour that did is a
    pure function. Asserting the payload is the honest boundary.
    """

    def test_the_apply_sub_resource_reports_errored_for_a_failed_apply(self):
        payload = _apply_json(_run("errored", started=NOW, finished=NOW))
        assert payload["data"]["attributes"]["status"] == "errored"

    def test_and_unreachable_when_no_apply_ran(self):
        payload = _apply_json(_run("errored"))
        assert payload["data"]["attributes"]["status"] == "unreachable"


class TestThePremiseTheFixRestsOn:
    """`transition_run` sets `apply_finished_at` on the errored transition.

    This is why `apply_finished_at` is useless as a discriminator and
    `apply_started_at` is the right one. It is asserted here rather than
    assumed, because the previous branch was a correct reading of a premise
    nobody had checked.
    """

    def _run_for_transition(self, status: str, **kw):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = status
        run.workspace_id = uuid.uuid4()
        run.plan_started_at = kw.get("plan_started_at")
        run.plan_finished_at = kw.get("plan_finished_at")
        run.apply_started_at = kw.get("apply_started_at")
        run.apply_finished_at = kw.get("apply_finished_at")
        # Same reason as `_run` above: truthy when unset.
        run.configure_started_at = kw.get("configure_started_at")
        run.configure_finished_at = kw.get("configure_finished_at")
        run.error_message = ""
        run.source = "tfe-api"
        return run

    async def test_erroring_an_in_flight_apply_stamps_apply_finished_at(self):
        db = AsyncMock(spec=AsyncSession)
        run = self._run_for_transition("applying", apply_started_at=NOW)
        assert run.apply_finished_at is None

        await transition_run(db, run, "errored", error_message="Error: apply failed")

        assert run.apply_finished_at is not None, (
            "the fix assumes this stamp is always set by the time `_apply_status` reads "
            "the run; if that is no longer true, re-read `_apply_status`'s errored branch"
        )
        # And the whole point: the run it just produced reports `errored`.
        assert _apply_status(run) == "errored"

    async def test_a_plan_failure_leaves_no_apply_start_so_it_stays_unreachable(self):
        db = AsyncMock(spec=AsyncSession)
        run = self._run_for_transition("planning", plan_started_at=NOW)

        await transition_run(db, run, "errored", error_message="Error: plan failed")

        assert run.apply_started_at is None
        assert _apply_status(run) == "unreachable"
