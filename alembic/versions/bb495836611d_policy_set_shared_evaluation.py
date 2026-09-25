"""policy set shared evaluation and support files

Revision ID: bb495836611d
Revises: c4d1e7b90a26
Create Date: 2026-09-25

#1842. Two additive columns on `policy_sets`:

``shared_evaluation`` — opt in to loading a set's files into ONE ``opa eval``
so policies can share helper rules and data. Defaults false, so every existing
set keeps evaluating one policy at a time exactly as before. The flag is the
only switch: nothing about a set changes until an operator sets it.

``support_files`` — the files a set carries that are not themselves policies:
``.yaml`` / ``.yml`` / ``.json`` data, and ``.rego`` helpers that define no
``deny``/``warn``. Keyed by filename WITH its extension, because the extension
is what tells OPA how to load it and what distinguishes a helper from a data
file.

Support files are synced whatever ``shared_evaluation`` says, and only *used*
when it is on. Storing them unconditionally is what makes the flag take effect
immediately: gate the sync on it instead and an operator who flips it on sees
nothing change until the next VCS poll, which reads as the feature not working.

Expand-only, so this is safe under a rolling upgrade: an old replica does not
read either column, and a new one finds the server defaults.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bb495836611d"
down_revision = "c4d1e7b90a26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "policy_sets",
        sa.Column(
            "shared_evaluation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "policy_sets",
        sa.Column(
            "support_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("policy_sets", "support_files")
    op.drop_column("policy_sets", "shared_evaluation")
