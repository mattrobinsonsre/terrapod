"""the run spine for a configure: the flag, its timestamps (#1988, #1973)

Three columns on `runs`, nothing dropped or retyped. Expand-only in the
strongest sense: a replica running the previous release neither reads nor
writes any of them, so a rolling upgrade is unaffected in both directions.

**A configure is a `Run` carrying a flag, not its own object type** (#1974,
settled 2026-10-16). The deciding fact was measured rather than argued: a
configure Job cannot be dispatched under either shape without touching the same
five places -- `RUNNER_PHASES` is a closed `{plan, apply}` frozenset that treats
a third phase as *invalid* rather than refused, `LIVE_STATES_FOR_PHASE` has no
third key, the claim loop is a literal two-tuple list, the reconciler derives
its phase from two statuses, and there are only two log keys. With that cost
paid either way, a separate object type bought nothing and cost a second
lifecycle, claim path, reconciler concern, RBAC surface, notification path and
UI, plus a second thing for the provider, MCP and the SDK to carry. The
composite case -- apply then configure -- already had to be one `Run`, because
two would mean the run goes terminal at the end of the apply, the CLI
disconnects, and the operator never sees the configure they asked for.

So `is_configure` sits beside `is_destroy`, `plan_only`, `refresh_only` and
`is_drift_detection`: one more thing a run can be, in a column of the shape the
other run options already use.

**`configure_started_at` and `configure_finished_at` mirror the plan and apply
pair, and the first is load-bearing rather than symmetric.** `_apply_status`
derives the TFE apply sub-resource's status entirely from the run, and a run
whose apply SUCCEEDED and whose configure then FAILED is `errored` with the
apply genuinely finished and a state version committed. Reporting that as a
failed apply would be the opposite of what happened, on the one surface an
operator reads to decide whether to re-run -- so the discrimination needs a
signal, and `configure_started_at` is it. (The adjacent bug, where every failed
apply reported `unreachable` because the branch keyed on `apply_finished_at`
being absent, shipped separately in #2128 so the supported lines could have it;
this builds on a branch that is already correct.)

**Defaults keep every existing run exactly as it is.** `is_configure` is FALSE
and the timestamps are NULL, so no data step is needed and nothing reads them
on a run that predates the column. A deployment running only terraform/tofu or
Pulumi carries the three columns and never sets them, which is how "those users
pay nothing" is delivered now that the engine on/off switch is withdrawn
(#1986): by data rather than by a flag an operator could set wrongly.

**Nothing can create a configure run yet, deliberately.** The API exposes no
`is-configure` attribute: #1974 owns triggering and #1972 owns executing. A
column an operator could set while nothing executed it would be the
"declaration without its reader" defect that #2116 was the fourth instance of,
and a run that could be created but never dispatched would sit `queued` for
ever. The vocabulary lands here; the things that set and consume it follow.

Revision ID: f08ad84dd25b
Revises: 7f1b648caf08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f08ad84dd25b"
down_revision: str | None = "7f1b648caf08"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        # One more thing a run can be, beside `is_destroy` / `plan_only` /
        # `refresh_only` / `is_drift_detection`. FALSE so every existing run is
        # unchanged and a configure is opt-in.
        sa.Column("is_configure", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "runs",
        # NOT merely symmetric with the plan/apply pair: this is what lets
        # `_apply_status` tell "the apply failed" from "the apply succeeded and
        # the configure failed", which are the same `errored` status with the
        # same two apply timestamps set.
        sa.Column("configure_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("configure_finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "configure_finished_at")
    op.drop_column("runs", "configure_started_at")
    op.drop_column("runs", "is_configure")
