"""workspaces/rules/runs: drop the auto_apply boolean, leaving auto_apply_mode

Revision ID: c6ca51805d3d
Revises: f9bb1c338a1c
Create Date: 2026-11-11

One setting was described by two columns: `auto_apply_mode` (never | always |
create | create_update) and an `auto_apply` boolean that was its projection,
written as `mode != "never"` at five separate sites. The boolean was kept "so
an un-upgraded client keeps working" (#1274); 2.0 is where that window closes.

Two columns for one setting is what let `auto_apply = false` arrive together
with `auto_apply_mode = "always"`, be resolved towards MORE automation than the
configuration asked for, and then fail with "Provider produced inconsistent
result after apply" — after the workspace had already been changed (#2503). With
one column the contradiction cannot be expressed.

The TFE-compatible surface still speaks `auto-apply`, because that is `go-tfe`'s
own field name rather than ours to drop. It now INFERS the boolean from this
column on the way out (`mode != "never"`) and MAPS one onto a mode on the way in
(`true -> always`, `false -> never`), so a `tofu` cloud backend and `tfci` see
no change. Sending both attributes in one request is refused with a 422, which
that surface already did and which is what keeps the mapping unambiguous.

A CONTRACTION, acknowledged in `tests/db/migration_contractions.json` and
recorded at RUNTIME in `schema_contractions` (#2500). The ledger is a review
gate and an old pod cannot read it; the table is how one finds out. Since 2.0 an
API pod stays READY when the schema is merely AHEAD of its code -- that is the
normal state of a rolling upgrade and refusing it emptied the load balancer for
the length of every rollout -- so the only thing that tells such a pod this
schema is no longer safe for it is the row inserted below.

It is parented on the migration that CREATES that table, which is not an
ordering accident: a contraction cannot record itself before the table exists.

The row does not help the upgrade INTO 2.0, and nothing can: `schema_version.py`
is byte-identical on release/v1.11 and release/v1.10 and neither carries any
ahead-handling, so a 1.x pod reports NOT READY against this schema and leaves
the load balancer. For this drop that is the correct outcome -- it reads a
column 2.0 removes -- and it means the roll into 2.0 is briefly unavailable.
`docs/upgrading-to-2.0.md` says so rather than leaving it to be discovered.

`downgrade()` restores the column and backfills it from the mode, so a
downgraded database is self-consistent rather than carrying a column of
defaults that contradicts the setting.
"""

import sqlalchemy as sa
from alembic import op

revision = "c6ca51805d3d"
down_revision = "f9bb1c338a1c"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "autodiscovery_rules", "runs")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "auto_apply")

    # Tell an API pod that predates this release that the schema is no longer
    # safe for it (#2500). A pod refuses readiness when this table holds a
    # revision its own graph does not contain, which needs no ordering between
    # revisions: never heard of it means it is newer than the pod, and already
    # in the graph means the pod is at or after it.
    op.execute(
        "INSERT INTO schema_contractions (revision, note) "
        "VALUES ('c6ca51805d3d', 'drops workspaces/autodiscovery_rules/runs.auto_apply') "
        "ON CONFLICT (revision) DO NOTHING"
    )


def downgrade() -> None:
    # Removed first: a downgraded database no longer holds this contraction, so
    # leaving the row would hold every pre-2.0 pod out of the load balancer for
    # a schema that is once again safe for it.
    op.execute("DELETE FROM schema_contractions WHERE revision = 'c6ca51805d3d'")

    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        # Backfill from the surviving column rather than leaving every row at
        # the default: a downgraded deployment would otherwise read every
        # auto-applying workspace as manual, which is the safe direction but
        # silently wrong about what the operator configured.
        op.execute(
            sa.text(
                f"UPDATE {table} SET auto_apply = (auto_apply_mode IS NOT NULL "  # noqa: S608
                "AND auto_apply_mode <> 'never')"
            )
        )
