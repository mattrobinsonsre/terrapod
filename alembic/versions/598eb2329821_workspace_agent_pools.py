"""workspace→agent-pool mapping table (#1087 / #960 phase 0)

Replaces the two-column pool set on `workspaces` (`agent_pool_id` holding
element 0, `agent_pool_extra_ids` JSONB holding the rest) with a mapping table
carrying a real foreign key on each side.

The columns did not earn their keep and the split left an integrity hole:
`delete_pool` is a bare DELETE that relies on `agent_pool_id`'s
`ON DELETE SET NULL` to detach workspaces, so the JSONB half — which can carry
no foreign key — left a dangling id in every set that named a deleted pool.
Both sides of the mapping table CASCADE, so a pool deletion now detaches
cleanly everywhere with no application-side sweeping.

**`workspaces.agent_pool_id` is deliberately NOT dropped here.** Migrations run
as a Helm `pre-upgrade` hook, so the new schema lands *before* the new pods: for
the length of the rollout every serving replica is still on the previous
release, and that release maps `agent_pool_id` with `lazy="joined"` — a LEFT
JOIN naming the column on *every* Workspace load, not just pool-aware queries.
Dropping it here would make workspace lookup, lock/unlock and **state-version
upload** raise `UndefinedColumn` on those replicas, so an in-flight apply could
fail to upload its state.

That is exactly what expand/contract exists to prevent (see
docs/versioning-and-support.md). This release expands and stops reading the
column; a later minor contracts it, once no supported release still reads it.

`agent_pool_extra_ids` IS dropped, and that is safe: it was added earlier in
this same release cycle and has never appeared in a published version, so no
running replica knows about it.

Revision ID: 598eb2329821
Revises: 9b7161d25c9e
Create Date: 2026-07-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "598eb2329821"
down_revision: str | None = "9b7161d25c9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_agent_pools",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_pool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_pool_id"], ["agent_pools.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "agent_pool_id"),
    )
    # Claim-side lookups go workspace → pools; the pool → workspaces direction
    # is what "which workspaces would this pool have to run?" needs.
    op.create_index(
        "ix_workspace_agent_pools_pool",
        "workspace_agent_pools",
        ["agent_pool_id"],
    )

    # Backfill: element 0 from the scalar column, the remainder from the JSONB
    # list, preserving order. `WITH ORDINALITY` numbers the JSONB entries from
    # 1, which is exactly the ordinal they need given element 0 takes 0.
    op.execute(
        """
        INSERT INTO workspace_agent_pools (workspace_id, agent_pool_id, ordinal)
        SELECT id, agent_pool_id, 0
        FROM workspaces
        WHERE agent_pool_id IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO workspace_agent_pools (workspace_id, agent_pool_id, ordinal)
        SELECT w.id, extra.value::uuid, extra.ordinality
        FROM workspaces w
        CROSS JOIN LATERAL jsonb_array_elements_text(w.agent_pool_extra_ids)
             WITH ORDINALITY AS extra(value, ordinality)
        WHERE jsonb_typeof(w.agent_pool_extra_ids) = 'array'
          AND EXISTS (SELECT 1 FROM agent_pools p WHERE p.id = extra.value::uuid)
        ON CONFLICT DO NOTHING
        """
    )

    op.drop_column("workspaces", "agent_pool_extra_ids")
    # NOT dropped: workspaces.agent_pool_id. The previous release still reads it
    # on every Workspace load and is still serving during the rollout — see the
    # module docstring. It is left in place, unread by this release, for a later
    # minor to contract.


def downgrade() -> None:
    # agent_pool_id was never dropped, so there is nothing to re-add — only the
    # JSONB half and the foreign key come back.
    op.add_column(
        "workspaces",
        sa.Column(
            "agent_pool_extra_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # The foreign key came back with the column — neither was dropped — so it
    # is not recreated here. Only the values need restoring.
    op.execute(
        """
        UPDATE workspaces w
        SET agent_pool_id = link.agent_pool_id
        FROM workspace_agent_pools link
        WHERE link.workspace_id = w.id AND link.ordinal = 0
        """
    )
    op.execute(
        """
        UPDATE workspaces w
        SET agent_pool_extra_ids = COALESCE(rest.ids, '[]'::jsonb)
        FROM (
            SELECT workspace_id,
                   jsonb_agg(agent_pool_id::text ORDER BY ordinal) AS ids
            FROM workspace_agent_pools
            WHERE ordinal > 0
            GROUP BY workspace_id
        ) AS rest
        WHERE rest.workspace_id = w.id
        """
    )
    op.drop_index("ix_workspace_agent_pools_pool", table_name="workspace_agent_pools")
    op.drop_table("workspace_agent_pools")
