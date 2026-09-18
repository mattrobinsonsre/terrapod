"""Rename terraform_version to engine_version (#1559).

The column holds the version of whichever engine the workspace runs, and now
that a Pulumi workspace pins its own version the old name says something untrue
about two thirds of the engines Terrapod supports. `execution_backend` names the
engine; `engine_version` names its version.

**This is a rename, not an expand/contract pair, and that is a deliberate
exception.** The usual rule is to add the new column, dual-write, and drop the
old one a release later, so that a rolling upgrade's old replicas keep working.
A rename instead means that between the migration hook finishing and the last
old API pod being replaced -- a window of seconds to a couple of minutes on a
`maxSurge: 1, maxUnavailable: 0` rollout -- an old replica selecting
`terraform_version` gets an error from Postgres, and the requests it is serving
fail.

It is taken because the alternative is worse in proportion: three tables would
carry two columns each, every write path would have to set both, and the
dual-write would have to stay correct across a release boundary for a column
nobody reads. Nothing outside the API touches the database, and the API and the
schema ship together, so the exposure is that one rollout window and nothing
else. The 2.0 release notes say so, and
`docs/upgrading-to-2.0.md` tells operators who cannot take it to scale the API
to one replica for the upgrade.

The API surface is NOT renamed by this: `terraform-version` keeps working as an
input and keeps being returned, alongside the canonical `engine-version`.

Revision ID: e6f8c4c1d698
Revises: c008b3b0787a
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6f8c4c1d698"
down_revision: str | Sequence[str] | None = "c008b3b0787a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table that snapshots or configures an engine version.
_TABLES = ("workspaces", "autodiscovery_rules", "runs")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "terraform_version", new_column_name="engine_version")


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "engine_version", new_column_name="terraform_version")
