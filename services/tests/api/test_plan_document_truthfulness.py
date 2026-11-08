"""The plan resource must not contradict the run it belongs to.

Two defects the TFE-compatibility survey (#2368) found in `_plan_json` /
`_plan_status`, both invisible to every snapshot gate because the attribute
names were right and only the values were wrong.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from terrapod.api.routers.runs import _plan_json, _plan_status


def _run(status: str, has_changes: bool | None) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.has_changes = has_changes
    run.has_json_output = False
    run.resource_additions = None
    return run


class TestHasChangesComesFromTheRun:
    """`has-changes` was derived from the run STATUS, so it lied on a no-change run.

    A run with nothing to do short-circuits `planned -> applied`
    (`run_service`), and the old expression was
    `run.status in ("planned", "confirmed", "applying", "applied")` -- every one
    of which that run passes through. So the plan claimed changes it did not
    have, while `Run.has-changes` next door was correct all along.

    It is read: `internal/cloud/backend_show.go` branches on the PLAN's
    `has-changes` to tag a shown plan `NoChanges`.
    """

    def test_a_no_change_run_reports_no_changes(self) -> None:
        # The case that was wrong: status says applied, the run says nothing changed.
        attrs = _plan_json(_run("applied", False))["data"]["attributes"]
        assert attrs["has-changes"] is False

    def test_a_changed_run_reports_changes(self) -> None:
        attrs = _plan_json(_run("applied", True))["data"]["attributes"]
        assert attrs["has-changes"] is True

    def test_the_two_resources_agree(self) -> None:
        """The pair is the property; either alone passes whichever way it is written."""
        for has_changes in (True, False):
            run = _run("planned", has_changes)
            plan = _plan_json(run)["data"]["attributes"]
            assert plan["has-changes"] is run.has_changes

    def test_a_row_predating_the_column_falls_back_to_the_status(self) -> None:
        """`None` means "written before the column existed", not "no changes"."""
        assert _plan_json(_run("planned", None))["data"]["attributes"]["has-changes"] is True
        assert _plan_json(_run("pending", None))["data"]["attributes"]["has-changes"] is False


class TestEveryStatusMapsToOneGoTfeCanSwitchOn:
    """`_plan_status` ends in `return s`, so an unmapped status leaks verbatim.

    `_apply_status` records this exact trap for this exact value -- "an unlisted
    status is handed to `go-tfe` verbatim, and `configuring` is not a value it
    can switch on" -- and added its own `configuring` case. The plan side had
    the same `return s` and no such case.
    """

    def test_configuring_is_a_finished_plan(self) -> None:
        # `configuring` is the post-apply phase, so the plan is long done.
        assert _plan_status(_run("configuring", True)) == "finished"

    def test_no_status_escapes_as_itself(self) -> None:
        """Derived from the state machine rather than a list kept by hand.

        That sentence was here before #2425 §2.5 and was not true: the set was
        two literal lines and `VALID_TRANSITIONS` was never read, so adding a
        status to the machine without a `_plan_status` case left this green --
        and `_plan_status` ends `return s`, so the new value would reach
        `go-tfe` verbatim, which is the single thing this test exists to stop.

        It is read off the map now, both sides of it: a status can appear only
        as a transition TARGET (`configuring` arrived that way), so taking the
        keys alone would miss exactly the kind of addition this is about. The
        two sets were identical the day this was fixed, which is the point --
        a hand-kept list is correct until it is not, and nothing says when.
        """
        from terrapod.services import run_service

        # Every status the state machine can produce, from its own transition map.
        statuses = set(run_service.VALID_TRANSITIONS)
        statuses |= {
            target for targets in run_service.VALID_TRANSITIONS.values() for target in targets
        }
        statuses |= set(run_service.TERMINAL_STATES)
        # A floor, so a map that stops being readable fails here rather than
        # passing on an empty subject.
        assert len(statuses) >= 10, (
            f"derived only {sorted(statuses)} from VALID_TRANSITIONS; the reader has "
            "stopped reading and this test is asserting nothing"
        )

        # The phase statuses go-tfe declares for a plan.
        allowed = {
            "pending",
            "managed_queued",
            "queued",
            "running",
            "errored",
            "canceled",
            "finished",
            "unreachable",
        }

        leaked = {s: _plan_status(_run(s, True)) for s in sorted(statuses)}
        leaked = {s: got for s, got in leaked.items() if got not in allowed}
        assert not leaked, (
            "these run statuses are handed to `go-tfe` verbatim as a plan status, "
            f"which it cannot switch on: {leaked}"
        )
