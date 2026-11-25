"""Drop pr_sessions.last_processed_review_id (#2704)

Declared with the `pr_sessions` table in #282 and read by nothing since: no
service writes it, no service reads it, no test asserts it, and it has no
spelling in any consumer -- the column name is the only form of it that has
ever existed in the tree.

It was meant as the review half of a pair of poll cursors, so a poll fallback
would not re-dispatch an already-handled review. Its sibling
`last_processed_comment_id` is genuinely load-bearing, and the contrast is what
settles this one: a comment IS an event, because it can carry `terrapod apply`,
so `_poll_pr_comments` filters on the cursor, dispatches per comment under a
comment-scoped dedup key, and advances it. Dispatching one twice would queue
two applies.

A review is never processed as an individual item anywhere, so there is no
second dispatch to guard:

* nothing enumerates reviews at all. `list_pr_reviews` is declared on the
  provider Protocol and implemented for both providers, and has no caller in
  any language -- so there is no loop for a cursor to advance through.
* no review triggers an action. The GitHub `pull_request_review` webhook takes
  no per-review action: it enqueues `vcs_immediate_poll` under a REPO-scoped
  dedup key, which any number of reviews collapse into by construction, and it
  deliberately does not resume a blocked apply. GitLab has no review branch at
  all.
* approval reaches the apply gate as a SNAPSHOT rather than an event.
  `run_service._check_mergeability_or_block`, on the confirm path, asks the
  provider for the pull request's current mergeability. Re-asking yields the
  same answer, so the idempotency a cursor would buy is already structural --
  and that call reads no review id and keeps no cursor.

It could not have worked on GitLab even if something had wanted it. The comment
cursor compares monotonic provider ids; GitLab has no review ids, only a
snapshot of current approvers keyed by user id, with no submission time. A
"last processed review id" has nothing there to order.

A CONTRACTION, so it records itself in `schema_contractions` (#2500). "Nothing
reads it" is true of the attribute and NOT of the query: SQLAlchemy names every
mapped column explicitly, so a pod whose code still maps this one emits it in
every `SELECT ... FROM pr_sessions` and would fail the whole VCS poll cycle
after the drop. The marker is what holds such a pod out of the load balancer
instead of leaving it READY against a schema it cannot serve.

No compatibility window is needed beyond that marker, and the reason is the
absence of readers rather than an expand/contract step: there is no value to
dual-write, nothing to backfill, and no code path to switch over first.

Not operator-visible in any form -- no API attribute, no Helm value, no config
key, no documentation mention -- so it earns no `docs/upgrading-to-2.0.md`
entry.

Revision ID: a0735f81e9d2
Revises: b56cc921fd47
"""

import sqlalchemy as sa
from alembic import op

revision = "a0735f81e9d2"
down_revision = "b56cc921fd47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("pr_sessions", "last_processed_review_id")

    op.execute(
        "INSERT INTO schema_contractions (revision, note) "
        "VALUES ('a0735f81e9d2', "
        "'Drops pr_sessions.last_processed_review_id (#2704), a poll cursor nothing "
        "ever read. Review idempotency comes from the mergeability snapshot taken at "
        "confirm time, not from a cursor. An older pod still maps the column, so its "
        "PRSession SELECT names it and must not serve.') "
        "ON CONFLICT (revision) DO NOTHING"
    )


def downgrade() -> None:
    # Nullable with no default, exactly as #282 created it. Nothing read the
    # column, so there is no value to reconstruct -- every row comes back NULL,
    # which is what every row held.
    op.add_column(
        "pr_sessions",
        sa.Column("last_processed_review_id", sa.String(64), nullable=True),
    )

    op.execute("DELETE FROM schema_contractions WHERE revision = 'a0735f81e9d2'")
