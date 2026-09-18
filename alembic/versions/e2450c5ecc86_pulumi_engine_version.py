"""Clear the Terraform version a Pulumi workspace was given (#1559).

`engine_version` did not mean anything for a Pulumi workspace until now: the
Pulumi CLI version was one value pinned for the whole deployment, and the column
was filled in by the ordinary workspace-creation path, which defaults it from
`default_terraform_version`. So every Pulumi workspace in every existing
database carries a Terraform version -- typically "1.12" -- that nothing read.

It is read now. Left alone, the first run on each of those workspaces would ask
the binary cache for **Pulumi 1.12**, which does not exist: Pulumi is on 3.x, the
partial would resolve to nothing, and the runner would 404 fetching a binary and
fail the run. That is a fleet-wide break on upgrade, on exactly the workspaces
this feature is for.

Clearing it to "" means "this deployment's default", which is what these
workspaces have effectively been running all along -- so the upgrade preserves
their behaviour rather than changing it. Anyone who wants a specific version
sets it, which is the whole point of the change.

Terraform and OpenTofu workspaces are untouched: their `engine_version` has
always meant what it says.

Revision ID: e2450c5ecc86
Revises: e6f8c4c1d698
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e2450c5ecc86"
down_revision: str | Sequence[str] | None = "e6f8c4c1d698"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only rows whose version is a Terraform one. A workspace that somehow
    # already carries a 3.x is left as it is rather than second-guessed.
    op.execute(
        """
        UPDATE workspaces
           SET engine_version = ''
         WHERE engine = 'pulumi'
           AND engine_version <> ''
           AND engine_version NOT LIKE '3.%'
        """
    )


def downgrade() -> None:
    # The per-row values this cleared cannot come back -- they were a default
    # nothing read, not information. What a downgrade can restore is the shape
    # the older code expects: a non-empty version on every Pulumi workspace,
    # holding the same Terraform default that path would have written. That is
    # exactly as meaningful as what was there before, which is to say not very,
    # and it puts the database back where the older code can work with it.
    op.execute(
        """
        UPDATE workspaces
           SET engine_version = '1.12'
         WHERE engine = 'pulumi'
           AND engine_version = ''
        """
    )
