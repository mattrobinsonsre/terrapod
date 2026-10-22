"""A listener reports run outcomes; it does not decide them (GHSA-3423).

`PATCH /listeners/{listener_id}/runs/{run_id}` took a free `status` from the
body. Only `planned` on an apply-capable run was routed through the guarded
`complete_plan`; everything else went to the generic `transition_run`, where
`planned -> confirmed` is a perfectly valid transition.

So whoever held a listener certificate could confirm its own pool's planned
runs, and confirming is what skips:

* the `run:apply` / `run:apply-destroy` capability check;
* the human confirmation itself;
* the staleness and plan-expiry guards;
* the cloud-identity staleness check;
* the CLI-configuration-version-on-a-VCS-workspace block;
* the mergeability gate.

On the next poll the run is claimed as apply and -- since per-workspace cloud
identity -- mints the workspace's apply-phase cloud credentials. The post-plan
gates (policy, run tasks, scans) still held, because a held run stays in
`planning`.

**The allowlist is keyed on the (from, to) PAIR, and that is the whole
subtlety.** `confirmed` is the vulnerability from `planned` and the legitimate
apply-phase UNCLAIM from `applying` -- the twin of `planning -> queued`, undoing
a claim the API could not serve. A target-only rule either reopens the bypass or
breaks the unclaim, and a broken unclaim means a run the API could not serve is
stuck instead of returned to the pool. There was no test for this endpoint at
all before these.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers import runs as router


def _identity(lid: uuid.UUID):
    ident = MagicMock()
    ident.listener_id = lid
    return ident


async def _patch(*, status: str, target: str, plan_only: bool = False):
    """Drive the listener PATCH, returning what the service layer was asked to do.

    Both service entry points are captured rather than mocked away silently, so a
    test can tell `complete_plan` (the guarded path) from `transition_run` (the
    generic one) -- which is the distinction the vulnerability lived in.
    """
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.listener_id = lid
    run.plan_only = plan_only
    run.job_name = "job-x"
    run.workspace_id = uuid.uuid4()

    db = MagicMock()
    db.commit = AsyncMock()
    # `update_run_status` resolves the run's engine through `_engine_of`, which
    # awaits `db.get(Workspace, ...)` (#1536). A bare MagicMock `get` is not
    # awaitable, so without this every test here dies in the engine lookup
    # before reaching the allowlist it is about.
    db.get = AsyncMock(return_value=MagicMock(engine="terraform"))
    seen: dict = {}

    # `**_` on both: the service signatures carry arguments this test has no
    # opinion about -- `engine` (#1536) among them -- and a stub that enumerates
    # them breaks every time one is added, for a test about which entry point
    # was called rather than with what.
    async def _complete_plan(_db, r, has_changes=None, **_):
        seen["via"] = "complete_plan"
        return r

    async def _transition(_db, r, to, error_message="", **_):
        seen["via"] = "transition_run"
        seen["to"] = to
        return r

    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch.object(router.run_service, "complete_plan", _complete_plan),
        patch.object(router.run_service, "transition_run", _transition),
        patch.object(router.run_service, "blocked_by", AsyncMock(return_value=None)),
        # `**_` for the same reason as the stubs above: `_run_json` on this line
        # also takes `engine` (#1536), and this test has no opinion about the
        # serialised body at all.
        patch.object(router, "_run_json", lambda r, blocked_by=None, **_: {"ok": True}),
    ):
        await router.update_run_status(
            listener_id=f"listener-{lid}",
            run_id=f"run-{run.id}",
            body={"status": target},
            identity=_identity(lid),
            db=db,
        )
    return seen


class TestAListenerCannotConfirmARun:
    async def test_planned_to_confirmed_is_refused(self):
        """The finding. `planned -> confirmed` is valid in the state machine and
        is exactly what a listener must not be able to ask for."""
        with pytest.raises(HTTPException) as exc:
            await _patch(status="planned", target="confirmed")
        assert exc.value.status_code == 403
        assert "may not move a run" in exc.value.detail

    async def test_the_refusal_says_why_rather_than_just_refusing(self):
        """An operator reading a listener's logs needs to know this is a boundary
        rather than a transient fault it should retry."""
        with pytest.raises(HTTPException) as exc:
            await _patch(status="planned", target="confirmed")
        assert "reports what its Job did" in exc.value.detail

    @pytest.mark.parametrize("target", ["discarded", "canceled", "canceling", "applied"])
    async def test_no_other_user_act_is_reportable_from_planned(self, target):
        """`planning -> discarded` exists for a human discarding a run held at a
        post-plan gate, and `planned -> applied` is the no-op apply the
        reconciler short-circuits. Neither is something a runner observed."""
        with pytest.raises(HTTPException) as exc:
            await _patch(status="planned", target=target)
        assert exc.value.status_code == 403


class TestTheUNCLAIMSStillWork:
    """The half a target-only allowlist would have broken.

    Both exist so a claim the API cannot serve -- today an unreachable Vault --
    is undone rather than erroring the run. Erroring instead would destroy every
    queued run in the estate over a restart.
    """

    async def test_applying_to_confirmed_is_the_apply_phase_unclaim(self):
        seen = await _patch(status="applying", target="confirmed")
        assert seen["via"] == "transition_run"
        assert seen["to"] == "confirmed"

    async def test_planning_to_queued_is_the_plan_phase_unclaim(self):
        seen = await _patch(status="planning", target="queued")
        assert seen["via"] == "transition_run"
        assert seen["to"] == "queued"


class TestTheLegitimateReportsAreUntouched:
    """A refusal that caught these would stop every run in the estate, which is
    a worse failure than the one being fixed."""

    async def test_planning_to_planned_goes_through_the_guarded_path(self):
        """Apply-capable, so it must reach `complete_plan` -- the shared path that
        applies the post-plan gates and the auto-apply staleness guards. Reaching
        `transition_run` instead is the #665 defect."""
        seen = await _patch(status="planning", target="planned", plan_only=False)
        assert seen["via"] == "complete_plan"

    async def test_a_plan_only_run_reports_planned_through_the_generic_path(self):
        seen = await _patch(status="planning", target="planned", plan_only=True)
        assert seen["via"] == "transition_run"

    @pytest.mark.parametrize(
        ("status", "target"),
        [
            ("planning", "errored"),
            ("applying", "applied"),
            ("applying", "errored"),
            ("canceling", "canceled"),
            ("canceling", "applied"),
            ("canceling", "errored"),
        ],
    )
    async def test_every_other_reportable_outcome_passes(self, status, target):
        seen = await _patch(status=status, target=target)
        assert seen["via"] == "transition_run"


class TestARetryIsNotAnAttempt:
    """Gated on the status actually CHANGING.

    The listener retries with bounded backoff, and `_complete_plan` is idempotent
    -- it returns early once the run has left `planning`, so a re-report of
    `planned` on an already-planned run answers 200 today. Turning that into a
    403 would make a listener read its own successful work as a failure, and the
    retry is the mechanism that makes a dropped response survivable.
    """

    async def test_reporting_the_status_a_run_already_has_is_allowed_through(self):
        seen = await _patch(status="planned", target="planned", plan_only=False)
        assert seen["via"] == "complete_plan"

    async def test_an_applying_rereport_is_allowed_through(self):
        seen = await _patch(status="applying", target="applying")
        assert seen["via"] == "transition_run"


class TestTheAllowlistIsDerivedFromTheStateMachine:
    """Every pair must be a transition the state machine actually permits.

    An entry that `VALID_TRANSITIONS` does not allow would be dead -- it reads as
    a permission being granted while `transition_run` refuses it anyway, which is
    the ledger-that-nothing-checks shape.
    """

    def test_every_reportable_pair_is_a_valid_transition(self):
        from terrapod.services.run_service import VALID_TRANSITIONS

        for current, target in router._LISTENER_REPORTABLE:
            assert target in VALID_TRANSITIONS.get(current, set()), (
                f"{current} -> {target} is listed as listener-reportable but the "
                f"state machine does not permit it"
            )

    def test_confirmed_is_reachable_ONLY_from_applying(self):
        """The pair-keyed property, stated directly. If `confirmed` ever becomes
        reportable from anything else, the bypass is back."""
        froms = {c for c, t in router._LISTENER_REPORTABLE if t == "confirmed"}
        assert froms == {"applying"}

    def test_a_configure_launch_failure_is_reportable(self):
        """`_report_launch_failed` PATCHes `errored` from whatever status the
        run is in, and a configure is in `configuring` (#1974). Without the pair
        the report is refused and a Job the listener could not create is only
        noticed minutes later by the reconciler's launch timeout -- where a plan
        and an apply both have it recorded immediately."""
        assert ("configuring", "errored") in router._LISTENER_REPORTABLE

    def test_a_configure_SUCCESS_is_not_reportable(self):
        """The asymmetry, stated so nobody "completes the pair". A listener
        never observes a configure succeeding -- the reconciler drives that from
        the Job's outcome -- and granting it would let whoever holds a listener
        certificate declare a playbook complete."""
        assert ("configuring", "applied") not in router._LISTENER_REPORTABLE

    def test_no_user_act_is_reportable(self):
        """`discarded` is a human discarding a held run; a listener never
        observes it."""
        targets = {t for _c, t in router._LISTENER_REPORTABLE}
        assert "discarded" not in targets
