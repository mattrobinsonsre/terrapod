"""Update default OpenTofu version from 1.9 to 1.11.

Revision ID: 00a2cf7e0345
Revises: 8529adc11ddb
"""

from alembic import op

revision = "00a2cf7e0345"
down_revision = "8529adc11ddb"


def upgrade() -> None:
    op.alter_column(
        "workspaces",
        "terraform_version",
        server_default="1.11",
    )
    op.alter_column(
        "runs",
        "terraform_version",
        server_default="1.11",
    )


def downgrade() -> None:
    op.alter_column(
        "runs",
        "terraform_version",
        server_default="1.9",
    )
    op.alter_column(
        "workspaces",
        "terraform_version",
        server_default="1.9",
    )
