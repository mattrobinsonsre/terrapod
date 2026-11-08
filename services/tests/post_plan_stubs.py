"""Stubs for the post-plan side effects that `runs.tfe_post_plan_decisions` arms.

`tfe_post_plan_decisions` defaults to **true** from 2.0 (#2425), which brings
three previously-dead code paths to life on every run:

* `create_run` reserves the run's post-plan task stage, because Terraform
  Enterprise creates a run's stages with the run and the CLI reads them once,
  straight afterwards (#1704);
* `transition_run` cancels a reserved stage that never started, when the run
  reaches a terminal status, so the CLI does not poll a pending stage forever;
* `show_run` builds the TFE post-plan view, which lists the run's policy-check
  and task-stage ids.

All of them issue real `db.execute(...)` queries. A test driving the run
lifecycle with a narrow mocked DB therefore fails inside machinery it does not
model -- `AttributeError: 'coroutine' object has no attribute 'first'` and
friends -- which says nothing about the behaviour under test.

These helpers stub that machinery out rather than widening each DB mock, for the
same reason `test_run_service.TestCompletePlan._stub_policy_gate` stubs the OPA
gate: the behaviour has its own dedicated tests, and widening a mock to satisfy
a side effect teaches it to answer queries the test has no opinion about.

**The stubs are not a substitute for coverage, and must not be used where the
post-plan vocabulary is the subject.** It is covered by:

* `tests/services/test_post_plan_decisions.py` -- the status vocabulary, and
  `TestAFailedMandatoryRunTask`, which pins the hold-versus-error decision in
  both vocabularies and for a plan-only run;
* `tests/integration/test_post_plan_decisions_integration.py` -- against a real
  database: `test_a_new_run_has_its_post_plan_stage_reserved`,
  `test_1_x_reserves_nothing` and `test_a_run_that_ends_first_cancels_the_stage`
  are the reserve/cancel pair end to end.

Note neither helper touches the `tfe_post_plan_decisions` setting. Selecting the
1.x default instead would make these tests dodge the flip rather than tolerate
it, leaving the behaviour every operator now gets exercised by almost nothing.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch


@contextlib.contextmanager
def stub_run_lifecycle_task_stages():
    """Neutralise the task-stage work `create_run` and `transition_run` do.

    `reserve_task_stage` answers `None` (nothing reserved) and
    `cancel_pending_stages` answers `0` (nothing cancelled) -- the shapes each
    returns when there is no stage to act on, so a caller that reads the return
    value sees a coherent answer rather than a `MagicMock`.
    """
    with (
        patch(
            "terrapod.services.run_task_service.reserve_task_stage",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "terrapod.services.run_task_service.cancel_pending_stages",
            new=AsyncMock(return_value=0),
        ),
    ):
        yield


@contextlib.contextmanager
def stub_post_plan_view_reads():
    """Neutralise the two list reads `_post_plan_view` adds to `show_run`.

    Both answer an empty list, which is what the view reports for a run with no
    policy checks and no task stages. `blocked_by` is untouched, so a test that
    cares which gate holds a run still gets the real answer.
    """
    with (
        patch(
            "terrapod.services.policy_check_service.list_checks",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "terrapod.services.run_task_service.list_run_task_stages",
            new=AsyncMock(return_value=[]),
        ),
    ):
        yield
