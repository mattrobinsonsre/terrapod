"""audit dual-actor model (#282 phase 7)

Adds `actor_type` / `origin` / `actor_login` / `actor_id` to the audit
log so we can distinguish HTTP/UI/API actions (`actor_type=terrapod_user`,
`origin=api|terrapod_ui`) from PR-comment-driven actions
(`actor_type=vcs_user`, `origin=pr_comment`) from background-task work
(`actor_type=system`). Lets a security review isolate VCS-driven changes
from Terrapod-user changes.

Also widens `action` from 20 → 40 chars to accommodate verb-based VCS
audit entries (e.g. "plan", "apply", "merge") in addition to the HTTP
method captured for API events.

Revision ID: a0b6c95a281d
Revises: c17aecf92ac8
Create Date: 2026-05-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a0b6c95a281d"
down_revision: str | None = "c17aecf92ac8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column(
            "actor_type",
            sa.String(20),
            nullable=False,
            server_default="terrapod_user",
        ),
    )
    op.add_column(
        "audit_logs",
        sa.Column("origin", sa.String(20), nullable=False, server_default="api"),
    )
    op.add_column(
        "audit_logs",
        sa.Column("actor_login", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "audit_logs",
        sa.Column("actor_id", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_audit_logs_actor_type", "audit_logs", ["actor_type"])
    op.alter_column("audit_logs", "action", type_=sa.String(40))


def downgrade() -> None:
    op.alter_column("audit_logs", "action", type_=sa.String(20))
    op.drop_index("ix_audit_logs_actor_type", table_name="audit_logs")
    op.drop_column("audit_logs", "actor_id")
    op.drop_column("audit_logs", "actor_login")
    op.drop_column("audit_logs", "origin")
    op.drop_column("audit_logs", "actor_type")
