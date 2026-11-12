"""Drop the `auto_apply` boolean from workspaces and autodiscovery rules (#2503)

One setting was described by two columns: `auto_apply_mode`
(`never`/`always`/`create`/`create_update`) and an `auto_apply` boolean that was
its projection. Two columns for one setting let a caller state a contradiction
-- `auto-apply: false` with `auto-apply-mode: "always"` -- which resolved
towards MORE automation than was asked for. One column cannot hold one.

`runs.auto_apply` is deliberately NOT dropped. It is not this setting: it
records what a particular run decided, beside `auto_apply_declined_reason`, and
a run's answer must survive the workspace setting changing underneath it.

A CONTRACTION, so it records itself in `schema_contractions` (#2500): an API
pod whose code predates this revision reads `workspaces.auto_apply` and would
fail every workspace query, so it must be held out of the load balancer rather
than left READY against a schema it cannot serve.

Upgrading from 1.11 is briefly unavailable regardless, and that is the correct
outcome rather than something to work around: 1.11's `schema_version` has no
ahead-of-code handling at all, so a 1.11 pod stays READY while reading a column
2.0 has removed. `docs/upgrading-to-2.0.md` states it as an expectation.

The upgrade promotes any row whose boolean says yes while the mode says `never`
before dropping -- there should be none (every write path has written the pair
together since #1274, which backfilled the existing rows), but a row that did
exist would otherwise lose auto-apply silently.

Revision ID: a7197ada2c0e
Revises: 26175f851307
"""

import sqlalchemy as sa
from alembic import op

revision = "a7197ada2c0e"
# Re-parented onto 2f4124a96516 (the run-plan-resources migration, #2488)
# rather than 26175f851307. Both were written against that parent, so the
# chain forked into two heads the moment #2488 landed -- a fork a file-level
# rebase cannot see, because neither file conflicts. The order carries no
# meaning here: that migration adds run plan-resource storage and this one
# drops two unrelated boolean columns.
down_revision = "2f4124a96516"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        # Belt and braces: the pair has been written together since #1274, so
        # this should match nothing. A row it did match would silently lose
        # auto-apply when the boolean went, which is the one direction worth
        # paying a statement to rule out.
        op.execute(
            f"UPDATE {table} SET auto_apply_mode = 'always' "  # noqa: S608
            "WHERE auto_apply = true AND auto_apply_mode = 'never'"
        )
        op.drop_column(table, "auto_apply")

    op.execute(
        "INSERT INTO schema_contractions (revision, note) "
        "VALUES ('a7197ada2c0e', "
        "'Drops workspaces.auto_apply and autodiscovery_rules.auto_apply (#2503). "
        "Safe once every replica resolves auto-apply from auto_apply_mode alone; "
        "an older pod still reads the dropped column and must not serve.') "
        "ON CONFLICT (revision) DO NOTHING"
    )


def downgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "auto_apply",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        # Derive the projection back from the setting, which is exactly what
        # every write path did before the drop.
        op.execute(
            f"UPDATE {table} SET auto_apply = true WHERE auto_apply_mode <> 'never'"  # noqa: S608
        )

    op.execute("DELETE FROM schema_contractions WHERE revision = 'a7197ada2c0e'")
