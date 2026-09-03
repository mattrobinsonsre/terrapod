"""Dynamic workspace assignment for variable sets (#1440).

Pure expand: one nullable column. NULL preserves the two modes that existed
before — explicit assignment and `global_set` — so no existing variable set
changes behaviour.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9dff757b0a4"
# Ordered so that release/v1.6's chain is a PREFIX of main's: a v1.6
# deployment upgrading to a main-based release must find its own head
# partway down this chain with main's extra work still ahead of it. If
# these two lines are "tidied" back, `alembic upgrade head` on such a
# database runs NOTHING and silently skips main's OCI/package-cache work.
down_revision = "e1bf96919d09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "variable_sets",
        sa.Column("assignment_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("variable_sets", "assignment_rule")
