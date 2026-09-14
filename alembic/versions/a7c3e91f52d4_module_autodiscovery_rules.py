"""module_autodiscovery_rules: rules that find and register registry modules

The module registry's counterpart to workspace autodiscovery (#1584). A rule
names a repository on a VCS connection, which directories count as modules
(a glob pattern plus ignore paths) and how each one is named. A scan registers
the matching directories as VCS-sourced modules — the root and any submodules —
and the registry poller registers new ones as they land on the tracked branch.

`registry_modules.module_autodiscovery_rule_id` records which rule registered a
module, the way `workspaces.autodiscovery_rule_id` does. Deleting a rule leaves
its modules in place: the reference is set to NULL.

Release line 1.7. Written on release/v1.7 on top of the line's head, and carried
to main at the same point with main's next migration re-parented onto it — see
"A release line's migrations are a prefix of main's" in AGENTS.md.

Additive: a new table, and a nullable column with no default on an existing one.

Revision ID: a7c3e91f52d4
Revises: 6dee2fd6e9b0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e91f52d4"
down_revision: str | None = "6dee2fd6e9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "module_autodiscovery_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vcs_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo_url", sa.String(2048), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("pattern", sa.String(1024), nullable=False),
        sa.Column(
            "ignore_patterns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("name_template", sa.String(255), nullable=False, server_default=""),
        sa.Column("provider", sa.String(63), nullable=False, server_default=""),
        sa.Column("vcs_tag_pattern", sa.String(255), nullable=False, server_default="v*"),
        sa.Column(
            "labels", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("owner_email", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scanned_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "seen_subdirectories",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("vcs_connection_id", "name", name="uq_module_autodiscovery_rule_name"),
    )
    op.create_index(
        "ix_module_autodiscovery_rules_repo",
        "module_autodiscovery_rules",
        ["vcs_connection_id", "repo_url"],
    )
    op.add_column(
        "registry_modules",
        sa.Column(
            "module_autodiscovery_rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "module_autodiscovery_rules.id",
                ondelete="SET NULL",
                name="fk_registry_modules_module_autodiscovery_rule",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_registry_modules_module_autodiscovery_rule", "registry_modules", type_="foreignkey"
    )
    op.drop_column("registry_modules", "module_autodiscovery_rule_id")
    op.drop_index("ix_module_autodiscovery_rules_repo", table_name="module_autodiscovery_rules")
    op.drop_table("module_autodiscovery_rules")
