"""Who may read another workspace's state (#344), for every engine.

A producer workspace shares its state by naming the consumers allowed to read
it. No rows means not shared, so the default is closed.

This lives in its own module because **two engines now ask the same question by
two different routes**, and the answer must not depend on which one asked:

- Terraform reads another workspace's state through `terraform_remote_state`,
  which the provider fetches over HTTP. The TFE-compatible state routes check
  this before serving (`routers/tfe_v2._runner_state_read_allowed`).
- Pulumi reads it through `StackReference`, which is served by whatever backend
  the CLI is pointed at. Agent runs point at Terrapod (#1879), so the Pulumi
  service surface checks this before resolving a stack that is not the run's own
  (`routers/pulumi_service._runner_caps_on`).

Held in one place so the two cannot drift: a grant added for a Terraform
consumer must mean exactly the same thing for a Pulumi one, and an operator
configuring the allowlist should never have to know which engine will read it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import WorkspaceRemoteStateConsumer


async def consumer_grant_id(
    db: AsyncSession,
    *,
    producer_workspace_id: uuid.UUID,
    consumer_workspace_id: uuid.UUID,
) -> uuid.UUID | None:
    """The grant letting `consumer` read `producer`'s state, or None.

    Returns the row's id rather than a bare bool because the Terraform path
    records it in the audit trail — `#344 Phase 2` writes a
    `workspace.remote_state_read` entry naming the grant, which is the thing a
    forensic reader follows back to who authorized the read. A predicate that
    answered only yes/no would quietly degrade that record, so the id is the
    return value and truthiness is left to the caller.

    A pure lookup: it decides nothing about self-reads, which are the caller's
    business, because "my own run's workspace" means something different on each
    surface. Both callers handle that case before reaching here.
    """
    row = await db.execute(
        select(WorkspaceRemoteStateConsumer.id).where(
            WorkspaceRemoteStateConsumer.producer_workspace_id == producer_workspace_id,
            WorkspaceRemoteStateConsumer.consumer_workspace_id == consumer_workspace_id,
        )
    )
    return row.scalar_one_or_none()
