"""A deployment may hold more than one GitLab connection

The unique constraint `uq_vcs_connections_install` has existed since the initial
schema as a blanket `UNIQUE (provider, github_installation_id)`. That is right for
GitHub, where the installation id identifies the credential: connecting the same
installation twice would put two credentials over the same repositories with no way
to tell which one a workspace is using.

It is meaningless for GitLab. `github_installation_id` is `NOT NULL DEFAULT 0` and
the GitLab create path never sets it, so **every** GitLab row carries `0` and the
pair `('gitlab', 0)` collides with itself. A deployment could therefore never hold
more than one GitLab connection, and the second attempt surfaced as a bare
`409 Resource already exists or violates a constraint` naming neither the column
nor the reason.

Nobody met it because nothing in the tree created two GitLab connections — a new
end-to-end test for the repository allowlist was the first, and it seeds one open
and one restricted connection because the behaviour under test is a contrast.

The practical cost was not theoretical: the documented remedy for a saturated
GitLab token is to give a busy repository its own connection, because a GitLab
token's rate allowance is per token. That remedy was impossible to follow.

Replaced with a partial unique index over the same columns, `WHERE provider =
'github'`, which enforces exactly what the constraint was for and nothing else.

Expand/contract: dropping the constraint cannot break a replica running older
code. Nothing reads it, and the GitHub duplicate case is *also* checked in the
create handler before the insert — the constraint is the backstop, not the check.
An older replica goes on refusing a duplicate GitHub installation for that reason,
and gains the ability to create a second GitLab connection, which is the fix.

Revision ID: 15a8b636ea54
Revises: d0bdcaa15b45
"""

import sqlalchemy as sa
from alembic import op

revision = "15a8b636ea54"
down_revision = "d0bdcaa15b45"
branch_labels = None
depends_on = None

_NAME = "uq_vcs_connections_install"


def upgrade() -> None:
    # The constraint and the index share a name on purpose: the name is what
    # anybody looking for this rule will grep for, and having both would leave
    # two objects claiming to express the same rule with different scopes.
    op.drop_constraint(_NAME, "vcs_connections", type_="unique")
    op.create_index(
        _NAME,
        "vcs_connections",
        ["provider", "github_installation_id"],
        unique=True,
        postgresql_where=sa.text("provider = 'github'"),
    )


def downgrade() -> None:
    # Restoring the blanket constraint fails if the deployment has taken
    # advantage of the fix, which is the honest outcome: there is no way to hold
    # two GitLab connections AND the constraint that forbids them. Keeping the
    # extra rows and silently dropping the rule would be worse, because the
    # next upgrade would then find duplicates it cannot explain.
    op.drop_index(_NAME, table_name="vcs_connections")
    op.create_unique_constraint(_NAME, "vcs_connections", ["provider", "github_installation_id"])
