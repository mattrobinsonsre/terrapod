"""OPA policy-as-code enforcement (#343)

Native policy-as-code: policy_sets hold named collections of Rego v1
policies, scoped to workspaces via the label-RBAC allow/deny model (or
global_scope for org-wide). policy_evaluations records the outcome of
evaluating one policy set against one run; a mandatory failure gates the
run at the post_plan boundary. Empty (no policy sets) => no behaviour
change for existing deployments.

Revision ID: 5a173d4b4e20
Revises: 42266b856e8e
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "5a173d4b4e20"
down_revision: str | None = "42266b856e8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_sets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "enforcement_level",
            sa.String(20),
            server_default="advisory",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("global_scope", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("allow_labels", JSONB(), server_default="{}", nullable=False),
        sa.Column("allow_names", JSONB(), server_default="[]", nullable=False),
        sa.Column("deny_labels", JSONB(), server_default="{}", nullable=False),
        sa.Column("deny_names", JSONB(), server_default="[]", nullable=False),
        sa.Column("created_by", sa.String(255), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_policy_sets_name", "policy_sets", ["name"])

    op.create_table(
        "policies",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_set_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("policy_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("rego", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("policy_set_id", "name", name="uq_policies_set_name"),
    )
    op.create_index("ix_policies_policy_set_id", "policies", ["policy_set_id"])

    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_set_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("policy_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("policy_set_name", sa.String(255), server_default="", nullable=False),
        sa.Column("enforcement_level", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("result", JSONB(), server_default="{}", nullable=False),
        sa.Column("overridden_by", sa.String(255), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "policy_set_id", name="uq_policy_evaluations_run_set"),
    )
    op.create_index("ix_policy_evaluations_run_id", "policy_evaluations", ["run_id"])
    op.create_index("ix_policy_evaluations_policy_set_id", "policy_evaluations", ["policy_set_id"])


def downgrade() -> None:
    op.drop_index("ix_policy_evaluations_policy_set_id", table_name="policy_evaluations")
    op.drop_index("ix_policy_evaluations_run_id", table_name="policy_evaluations")
    op.drop_table("policy_evaluations")
    op.drop_index("ix_policies_policy_set_id", table_name="policies")
    op.drop_table("policies")
    op.drop_index("ix_policy_sets_name", table_name="policy_sets")
    op.drop_table("policy_sets")
