"""`latest-run` carries the three phase STARTS, so a badge can deep-link (#2413 F16).

The review's named case was *"when you click errored on a workspace you get taken
to the run which errored"*, and the workspace document was what stood in the way:
it carried `{id, status, plan-only, created-at}`, so a client could not tell WHICH
phase to open. `status` alone cannot answer it -- `errored` does not name a phase,
and the phase that failed recorded no finish time, so the last phase to have
STARTED is the only honest signal.

Deliberately not called `status-timestamps`, though these are the key names the run
document uses for the same three moments: that attribute carries queue and finish
times too, and a subset under the same name would read as the whole of it. The key
names match so that ONE rule reads both --
`web/src/lib/run-phases.ts::explainingPhase` stays the single producer of
"which phase explains this", and `runHref` beside it the single producer of the URL.

The fixtures are the REAL ORM models rather than hand-built stubs. The serializer
reads 71 workspace attributes, and a stub enumerating them is the fixture that
breaks the next time a column is added -- which this repository has already paid
for once, as 23 failures across five files whose message named neither the new
attribute nor the fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from terrapod.api.routers import tfe_v2
from terrapod.db.models import Run, Workspace

T = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime.now(UTC)


def _ws() -> Workspace:
    return Workspace(id=uuid.uuid4(), name="ws", created_at=NOW, updated_at=NOW)


def _run(status: str = "errored", **starts) -> Run:
    return Run(
        id=uuid.uuid4(),
        status=status,
        plan_only=False,
        created_at=NOW,
        plan_started_at=starts.get("plan"),
        apply_started_at=starts.get("apply"),
        configure_started_at=starts.get("configure"),
    )


def _latest(run: Run | None) -> dict | None:
    doc = tfe_v2._workspace_json(_ws(), latest_run=run, tfe=False)
    return doc["data"]["attributes"]["latest-run"]


class TestPhaseStarts:
    def test_the_three_starts_are_emitted_under_their_run_document_names(self):
        assert _latest(_run(plan=T, apply=T, configure=T))["phase-started-at"] == {
            "planning-at": tfe_v2._rfc3339(T),
            "applying-at": tfe_v2._rfc3339(T),
            "configuring-at": tfe_v2._rfc3339(T),
        }

    def test_an_unstarted_phase_is_DROPPED_not_null(self):
        # `explainingPhase` takes the last key PRESENT, so a null would read as
        # a phase that started -- and would send an errored plan to the apply
        # view. The run document drops nulls for the same reason; matching it is
        # what lets one rule read both.
        assert _latest(_run(plan=T))["phase-started-at"] == {"planning-at": tfe_v2._rfc3339(T)}

    def test_a_composite_reports_the_configure_as_the_last_to_start(self):
        # The case the attribute exists for: an apply-then-configure that
        # errored. Both phases ran, and the one that explains the outcome is the
        # later one.
        starts = _latest(_run(plan=T, apply=T, configure=T))["phase-started-at"]
        assert list(starts)[-1] == "configuring-at"

    def test_a_run_that_never_started_a_phase_carries_an_empty_map(self):
        # Not absent: `queued` is a real state, and the client falls back on the
        # DERIVED phase being null rather than on the key being missing.
        assert _latest(_run(status="queued"))["phase-started-at"] == {}

    def test_the_pre_existing_keys_are_untouched(self):
        # Additive, so an un-upgraded client reads exactly what it read before.
        assert {"id", "status", "plan-only", "created-at"} <= set(_latest(_run(plan=T)))

    def test_no_latest_run_stays_null_rather_than_an_empty_object(self):
        assert _latest(None) is None
