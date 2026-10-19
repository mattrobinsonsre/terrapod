"""Per-workspace Ansible execution-environment resources (#2154)

The EE runs as a native sidecar beside the orchestrator, so the Job now has two
containers doing different work: the orchestrator runs tofu, and the EE runs
ansible against however many hosts the inventory resolves to. One request pair
cannot size both, so the EE gets its own -- with the same 2x limit rule, the
same per-workspace override, and the same snapshot onto the run.

Expand-only: six nullable=False columns with a `''` server default, which is
the "inherit the deployment default" value rather than a concrete size. A
replica that predates this release writes neither and reads neither, and
`build_job_spec` carries its own default for a run created by one.

Revision ID: fe8c74c89dd5
Revises: aecc6a77aad2
Create Date: 2026-10-19

"""

import sqlalchemy as sa
from alembic import op

revision = "fe8c74c89dd5"
down_revision = "aecc6a77aad2"
branch_labels = None
depends_on = None

#: Every table that carries the orchestrator's own `resource_cpu` pair, because
#: the EE pair follows it everywhere: the workspace owns the setting, the rule
#: templates it onto the workspaces it materialises, and the run snapshots it.
_TABLES = ("workspaces", "autodiscovery_rules", "runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("ee_resource_cpu", sa.String(20), nullable=False, server_default=""),
        )
        op.add_column(
            table,
            sa.Column("ee_resource_memory", sa.String(20), nullable=False, server_default=""),
        )
    # The run alone, because this records WHICH environment a run used rather
    # than how big it was. The built-in tags move, so a run that does not
    # record its own reference cannot be identified afterwards -- and it is
    # what the reconciler names when a sidecar never becomes ready.
    op.add_column(
        "runs",
        sa.Column("ee_image", sa.String(500), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("runs", "ee_image")
    for table in _TABLES:
        op.drop_column(table, "ee_resource_memory")
        op.drop_column(table, "ee_resource_cpu")
