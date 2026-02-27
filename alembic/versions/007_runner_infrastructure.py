"""Runner infrastructure: variables, config versions, runs, CA.

Revision ID: 007
Revises: 006
Create Date: 2026-02-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Certificate Authority ---
    op.create_table(
        "certificate_authority",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ca_cert", sa.Text(), nullable=False),
        sa.Column("ca_key_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- Variables ---
    op.create_table(
        "variables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "category", sa.String(20), nullable=False, server_default="terraform"
        ),
        sa.Column("hcl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "key", name="uq_variables_workspace_key"),
    )
    op.create_index("ix_variables_workspace_id", "variables", ["workspace_id"])

    # --- Variable Sets ---
    op.create_table(
        "variable_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "global_set", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "priority", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("org_name", "name", name="uq_variable_sets"),
    )

    # --- Variable Set Variables ---
    op.create_table(
        "variable_set_variables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variable_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "category", sa.String(20), nullable=False, server_default="terraform"
        ),
        sa.Column("hcl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("variable_set_id", "key", name="uq_variable_set_variables"),
    )
    op.create_index(
        "ix_variable_set_variables_set_id",
        "variable_set_variables",
        ["variable_set_id"],
    )

    # --- Variable Set Workspaces (junction) ---
    op.create_table(
        "variable_set_workspaces",
        sa.Column(
            "variable_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # --- Configuration Versions ---
    op.create_table(
        "configuration_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(20), nullable=False, server_default="tfe-api"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "auto_queue_runs",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "speculative", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_configuration_versions_workspace_id",
        "configuration_versions",
        ["workspace_id"],
    )

    # --- Runs ---
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "configuration_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("configuration_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "is_destroy", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "auto_apply", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "plan_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("source", sa.String(30), nullable=False, server_default="tfe-api"),
        sa.Column(
            "terraform_version", sa.String(20), nullable=False, server_default=""
        ),
        sa.Column(
            "runner_definition",
            sa.String(63),
            nullable=False,
            server_default="standard",
        ),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "listener_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runner_listeners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"])
    op.create_index("ix_runs_status", "runs", ["status"])

    # --- Alter agent_pools: add service_account_name ---
    op.add_column(
        "agent_pools",
        sa.Column(
            "service_account_name",
            sa.String(63),
            nullable=False,
            server_default="",
        ),
    )

    # --- Alter workspaces: add agent_pool_id and runner_definition ---
    op.add_column(
        "workspaces",
        sa.Column(
            "agent_pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "runner_definition",
            sa.String(63),
            nullable=False,
            server_default="standard",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "runner_definition")
    op.drop_column("workspaces", "agent_pool_id")
    op.drop_column("agent_pools", "service_account_name")
    op.drop_table("runs")
    op.drop_table("configuration_versions")
    op.drop_table("variable_set_workspaces")
    op.drop_table("variable_set_variables")
    op.drop_table("variable_sets")
    op.drop_table("variables")
    op.drop_table("certificate_authority")
