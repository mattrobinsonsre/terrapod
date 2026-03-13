"""Add job_name and job_namespace to runs table.

Revision ID: fe70f352330a
Revises: 355a27a76df4
"""

from alembic import op
import sqlalchemy as sa


revision = "fe70f352330a"
down_revision = "355a27a76df4"


def upgrade() -> None:
    op.add_column("runs", sa.Column("job_name", sa.String(255), nullable=True))
    op.add_column("runs", sa.Column("job_namespace", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "job_namespace")
    op.drop_column("runs", "job_name")
