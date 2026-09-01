"""Dynamic workspace assignment for variable sets (#1440).

Pure expand: one nullable column. NULL preserves the two modes that existed
before — explicit assignment and `global_set` — so no existing variable set
changes behaviour.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9dff757b0a4"
down_revision = "4408d25e7130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "variable_sets",
        sa.Column("assignment_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("variable_sets", "assignment_rule")
