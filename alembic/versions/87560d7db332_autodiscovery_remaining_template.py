"""Autodiscovery rules template the remaining workspace settings (#1763).

`a6751186f8b7` gave the rule the two feature sets the issue named. Auditing the
rest column by column found ten more ordinary settings a rule could not set, so
every workspace it created had to be corrected by hand afterwards — the same
gap, just on settings nobody had got round to noticing.

Ten is the whole remainder. What a rule deliberately does NOT template is now
recorded with its reason in
`tests/services/test_workspace_autodiscovery_service.py::
TestEveryWorkspaceSettingIsTemplatedOrLedgered`, so the next setting cannot go
missing here silently.

Purely additive and defaulted to today's behaviour: every column takes the value
the workspace-creation path already defaults to, so an existing rule materialises
exactly the workspace it materialised before this ran.

`drift_detection_enabled` is the one to look at twice. A workspace created by the
ordinary API defaults it to `true` when the workspace is VCS-connected, and every
autodiscovered workspace is. The rule therefore defaults to `true` as well —
`false` would have quietly turned drift detection off across every monorepo
directory on upgrade.

Revision ID: 87560d7db332
Revises: a6751186f8b7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "87560d7db332"
down_revision: str | Sequence[str] | None = "a6751186f8b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("terragrunt_enabled", sa.Boolean(), "false"),
    ("terragrunt_version", sa.String(50), "1.0"),
    ("vcs_workflow", sa.String(20), "merge_then_apply"),
    ("auto_merge", sa.Boolean(), "false"),
    ("auto_merge_strategy", sa.String(20), "merge"),
    ("drift_detection_enabled", sa.Boolean(), "true"),
    ("drift_detection_interval_seconds", sa.Integer(), "86400"),
    ("slack_channel", sa.String(128), ""),
)


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column(
            "autodiscovery_rules",
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    # Nullable: NULL means "no expiry", which is what the workspace column
    # means too. A zero default would read as an expiry of zero seconds.
    op.add_column(
        "autodiscovery_rules",
        sa.Column("plan_expiry_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "drift_ignore_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "drift_ignore_rules")
    op.drop_column("autodiscovery_rules", "plan_expiry_seconds")
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column("autodiscovery_rules", name)
