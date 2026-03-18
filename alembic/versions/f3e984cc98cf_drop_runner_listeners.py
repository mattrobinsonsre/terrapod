"""Drop runner_listeners table — listeners move to Redis.

Revision ID: f3e984cc98cf
Revises: 1b7bc2b4e011
Create Date: 2026-03-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f3e984cc98cf"
down_revision = "1b7bc2b4e011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop FK constraint on runs.listener_id (keep column as bare UUID)
    op.drop_constraint(
        "runs_listener_id_fkey", "runs", type_="foreignkey"
    )

    # Drop runner_listeners table and its index
    op.drop_index("ix_runner_listeners_pool_id", table_name="runner_listeners")
    op.drop_table("runner_listeners")


def downgrade() -> None:
    # Recreate runner_listeners table
    op.create_table(
        "runner_listeners",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), unique=True, nullable=False),
        sa.Column("certificate_fingerprint", sa.String(64), nullable=True),
        sa.Column("certificate_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "runner_definitions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
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
    )
    op.create_index(
        "ix_runner_listeners_pool_id", "runner_listeners", ["pool_id"]
    )

    # Re-add FK constraint on runs.listener_id
    op.create_foreign_key(
        "runs_listener_id_fkey",
        "runs",
        "runner_listeners",
        ["listener_id"],
        ["id"],
        ondelete="SET NULL",
    )
