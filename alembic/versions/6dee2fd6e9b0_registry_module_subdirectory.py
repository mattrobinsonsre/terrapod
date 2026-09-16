"""registry_modules: subdirectory, so a module can live below a repository's root

A module in a subdirectory of a repository — what Terraform and OpenTofu
address with the `//subdir` source syntax — could not be registered: a module
was only ever a repository's root (#1583). This records the path within the
repository. Empty means the root, which is what every existing module is.

A partial unique index stops one subdirectory of one repository being
registered twice under different names. It covers only rows that have a
subdirectory, so it cannot conflict with any existing registration, and root
modules stay exactly as unconstrained as they were.

Release line 1.7. Written on release/v1.7 on top of 1.6's head, and carried to
main at the same point with main's next migration re-parented onto it — see
"A release line's migrations are a prefix of main's" in AGENTS.md.

Additive: NOT NULL with server_default '', so no existing row changes.

Revision ID: 6dee2fd6e9b0
Revises: 8c02c0a6b39b
"""

import sqlalchemy as sa
from alembic import op

revision: str = "6dee2fd6e9b0"
down_revision: str | None = "8c02c0a6b39b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "registry_modules",
        sa.Column("subdirectory", sa.String(500), nullable=False, server_default=""),
    )
    op.create_index(
        "uq_registry_modules_repo_subdirectory",
        "registry_modules",
        ["vcs_repo_url", "subdirectory"],
        unique=True,
        postgresql_where=sa.text("subdirectory <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_registry_modules_repo_subdirectory", table_name="registry_modules")
    op.drop_column("registry_modules", "subdirectory")
