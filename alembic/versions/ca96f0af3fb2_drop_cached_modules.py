"""Drop cached_modules table.

Module cache code removed — terraform/tofu has no module mirror protocol.

Revision ID: ca96f0af3fb2
Revises: 8d2c5b0c1edc
Create Date: 2026-03-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "ca96f0af3fb2"
down_revision: Union[str, None] = "8d2c5b0c1edc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Table may not exist on fresh databases where e891915cd9d1 is already a no-op
    op.execute("DROP INDEX IF EXISTS ix_cached_modules_lookup")
    op.execute("DROP TABLE IF EXISTS cached_modules")


def downgrade() -> None:
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    op.create_table(
        "cached_modules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provider", sa.String(63), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "hostname", "namespace", "name", "provider", "version",
            name="uq_cached_modules",
        ),
    )
    op.create_index(
        "ix_cached_modules_lookup",
        "cached_modules",
        ["hostname", "namespace", "name", "provider"],
    )
