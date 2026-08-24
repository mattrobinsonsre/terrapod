"""When a mirrored tag was last confirmed against upstream (#1425).

Pure expand: one nullable column, so old and new replicas run against it safely
during a rolling upgrade.

The backfill needs explaining, because NULL is not merely "unset" once this
column exists — it marks a tag as locally pushed, and locally pushed tags are
never revalidated. Leaving every existing row NULL would therefore withhold the
fix from precisely the tags that motivated it: an operator upgrades to stop
`latest` being pinned for ever, and every `latest` they already had stays pinned
for ever.

So existing tags in repositories that mirror are backfilled from `updated_at`,
which starts their freshness window at the last time the row was known to be
current. They revalidate on the first pull after the TTL, which is the intended
behaviour.

Rows predating this column carry no provenance, so one case is genuinely
ambiguous: a tag *pushed* into a mirror repository is indistinguishable from one
fetched into it, and is backfilled alongside. That is the deliberate trade — it
is an unusual thing to have done, and the alternative silently withholds the fix
from every cached tag in the deployment. Tags created from here on record their
provenance at write time and are not guessed about.
"""

import sqlalchemy as sa
from alembic import op

revision = "792641f78049"
down_revision = "c45b8a594980"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oci_tags",
        sa.Column("revalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE oci_tags
           SET revalidated_at = oci_tags.updated_at
          FROM oci_repositories
         WHERE oci_repositories.id = oci_tags.repository_id
           AND oci_repositories.upstream IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("oci_tags", "revalidated_at")
