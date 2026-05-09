"""Add autodiscovery_rules table.

Adds connection-scoped rules that drive workspace autodiscovery in
monorepos. When the VCS poller scans a PR (or a default-branch push)
and the changed paths don't match any existing workspace's
trigger_prefixes, autodiscovery rules are consulted: a path that
matches a rule's `pattern` and isn't excluded by `ignore_patterns`
auto-creates a workspace using the rule's template fields and
queues the corresponding run.

Rules belong to a VCS connection, not an organization (Terrapod is
single-org). Every rule names exactly one repo so rule-fanout is
simple and predictable. The `repo_url` mirrors the existing
`workspaces.vcs_repo_url` so the same parser/dispatch logic works
unchanged.

See terrapod #283.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "8bb13bfef824"
down_revision = "ad9e14fa1469"


def upgrade() -> None:
    op.create_table(
        "autodiscovery_rules",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        # Scoping
        sa.Column("vcs_connection_id", sa.UUID(), nullable=False),
        sa.Column("repo_url", sa.String(2048), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False, server_default=""),
        # Match
        sa.Column("pattern", sa.String(1024), nullable=False),
        sa.Column("ignore_patterns", JSONB, nullable=False, server_default="[]"),
        # Identity
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("name_template", sa.String(255), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Workspace template
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="agent"),
        sa.Column("agent_pool_id", sa.UUID(), nullable=True),
        sa.Column("execution_backend", sa.String(20), nullable=False, server_default="tofu"),
        sa.Column("terraform_version", sa.String(50), nullable=False, server_default="1.11"),
        sa.Column("resource_cpu", sa.String(20), nullable=False, server_default="1"),
        sa.Column("resource_memory", sa.String(20), nullable=False, server_default="2Gi"),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("labels", JSONB, nullable=False, server_default="{}"),
        sa.Column("owner_email", sa.String(255), nullable=True),
        # Audit
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["vcs_connection_id"], ["vcs_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_pool_id"], ["agent_pools.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("vcs_connection_id", "name", name="uq_autodiscovery_rule_name"),
    )
    op.create_index(
        "ix_autodiscovery_rules_repo",
        "autodiscovery_rules",
        ["vcs_connection_id", "repo_url"],
    )
    # Track which rule (if any) auto-created a workspace, so we can
    # audit autodiscovered workspaces and avoid re-creating them on
    # subsequent poll cycles.
    op.add_column(
        "workspaces",
        sa.Column("autodiscovery_rule_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workspaces_autodiscovery_rule_id",
        "workspaces",
        "autodiscovery_rules",
        ["autodiscovery_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_autodiscovery_rule_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "autodiscovery_rule_id")
    op.drop_index("ix_autodiscovery_rules_repo", table_name="autodiscovery_rules")
    op.drop_table("autodiscovery_rules")
