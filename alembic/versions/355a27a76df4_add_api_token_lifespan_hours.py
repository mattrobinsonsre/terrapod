"""Add api_tokens.lifespan_hours column.

Revision ID: 355a27a76df4
Revises: 1f15ac58564c
"""

from alembic import op
import sqlalchemy as sa


revision = "355a27a76df4"
down_revision = "1f15ac58564c"


def upgrade() -> None:
    op.add_column("api_tokens", sa.Column("lifespan_hours", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_tokens", "lifespan_hours")
