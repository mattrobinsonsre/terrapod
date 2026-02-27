"""Agent pools, join tokens, and runner listeners.

Adds tables for the runner infrastructure:
- agent_pools: named groups of runner listeners
- agent_pool_tokens: join tokens for remote listener registration
- runner_listeners: registered listener identities (runtime state in Redis)

Seeds the default local pool.

Revision ID: 003
Revises: 002
Create Date: 2026-02-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_pools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(63), unique=True, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("org_name", sa.String(63), nullable=True),
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

    op.create_table(
        "agent_pool_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.String(255), nullable=False),
    )
    op.create_index("ix_agent_pool_tokens_token_hash", "agent_pool_tokens", ["token_hash"])
    op.create_index("ix_agent_pool_tokens_pool_id", "agent_pool_tokens", ["pool_id"])

    op.create_table(
        "runner_listeners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), unique=True, nullable=False),
        sa.Column("certificate_fingerprint", sa.String(64), nullable=True),
        sa.Column("certificate_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "runner_definitions",
            postgresql.JSONB(),
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
    op.create_index("ix_runner_listeners_pool_id", "runner_listeners", ["pool_id"])

    # Seed the default local pool
    op.execute(
        sa.text(
            "INSERT INTO agent_pools (id, name, description) "
            "VALUES (gen_random_uuid(), 'default', 'Local in-cluster runner pool')"
        )
    )


def downgrade() -> None:
    op.drop_table("runner_listeners")
    op.drop_table("agent_pool_tokens")
    op.drop_table("agent_pools")
