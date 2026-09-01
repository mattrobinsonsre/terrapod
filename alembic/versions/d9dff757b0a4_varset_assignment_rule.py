"""Dynamic workspace assignment for variable sets (#1440).

Pure expand: one nullable column. NULL preserves the two modes that existed
before — explicit assignment and `global_set` — so no existing variable set
changes behaviour.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9dff757b0a4"
# Chained to this line's head, not main's: the 1.6 release branch is cut
# from 1.5 and never carries the migrations that landed on main after it.
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
