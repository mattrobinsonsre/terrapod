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
    # GHSA-mjhj-g43q-w6m9 (D3). These four columns exist so a security review
    # can tell a VCS-driven change from a Terrapod-user one; dropping them
    # leaves a PR-comment-driven apply by an external VCS user with NO
    # attribution at all, because such a row has an empty `actor_email` —
    # precisely the case the dual-actor columns were added to cover.
    #
    # Downgrades are an operator action rather than an attacker one, so this is
    # a forensic-integrity defect rather than an exploitable one. It is worth
    # guarding because it fires during incident response or a botched upgrade,
    # which is exactly when the attribution matters most.
    #
    # The whole run is wrapped in one transaction (alembic/env.py does not set
    # transaction_per_migration), so this either aborts cleanly and changes
    # nothing, or proceeds.
    bind = op.get_bind()
    attributed = bind.execute(
        # Must test for attribution that is NOT reconstructible from the
        # columns that survive. `upgrade()` backfills every existing row with
        # server_default 'terrapod_user' / 'api', and every new row gets the
        # same — so `actor_type <> ''` was true for EVERY row ever written and
        # the guard blocked the downgrade on every deployment, including the
        # ones its own comment calls safe.
        sa.text(
            "SELECT count(*) FROM audit_logs WHERE "
            "actor_type <> 'terrapod_user' OR origin <> 'api' "
            "OR actor_login <> '' OR actor_id <> ''"
        )
    ).scalar()
    if attributed:
        raise RuntimeError(
            f"refusing to drop the dual-actor audit columns: {attributed} audit row(s) "
            "carry non-default attribution (a VCS user, a PR comment, or a background "
            "task) that exists nowhere else — a VCS-driven action has no actor_email "
            "to fall back on. Export audit_logs first if you need this downgrade."
        )
    op.alter_column("audit_logs", "action", type_=sa.String(20))
    op.drop_index("ix_audit_logs_actor_type", table_name="audit_logs")
    op.drop_column("audit_logs", "actor_id")
    op.drop_column("audit_logs", "actor_login")
    op.drop_column("audit_logs", "origin")
    op.drop_column("audit_logs", "actor_type")
