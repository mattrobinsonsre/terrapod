"""Auto-configure on an inventory change that did not arrive through git (#2224).

#1974 fires a configure from a PUSH. This fires one from an inventory change
written any other way -- the API, the MCP tools, the UI, or a Terraform apply
declaring `terrapod_inventory_*` rows. The user's framing, which is the design:

    any inventory change calls for a run on the small minority of workspaces
    which have auto configure

## The signal is the equality token, not a hook on the write paths

`inventory_resolve.platform_rev` already exists for the resolution cache, and it
is exactly "what the declared rows currently say": a COUNT and a maximum
`updated_at` per contributing table. So "any inventory change" is "the token
differs", and three things follow for free:

* **A DELETE fires it.** The count is in the token precisely because removing a
  row changes the resolution and leaves the maximum where it was. Removing a host
  from a group changes what every pattern naming that group resolves to, so it
  must fire -- and it does, with no logic of ours.
* **Nothing has to be added to the twenty-odd inventory write routes**, so there
  is no call site to forget and no ledger to police. The alternative -- a service
  call in each handler, with a derived gate asserting every handler makes it --
  is the shape this codebase keeps paying for.
* **It survives a restart and a Redis flush**, because the comparison basis is a
  column rather than an in-flight timer.

## The debounce is a settle, and it is the whole of the design

A single `for_each` apply over fifty instances writes fifty hosts, fifty host
variables and fifty memberships. Comparing the token on a bare tick would fire
once per tick while that apply is still writing. So a run is created only when
the token has been **stable since the previous tick**: a burst coalesces into one
run once it stops moving.

The previous tick's observation lives in Redis rather than in a second column,
because it is transient and losing it costs one extra tick of delay rather than a
spurious run -- the column still bounds what can fire.

## Three directions this deliberately does not take

* **It stamps at run CREATION, not on success.** One change produces one run. If
  it stamped on success a configure that keeps failing would re-fire for ever
  against an inventory that has not moved; retrying a failure is #1930's.
* **The first observation ADOPTS rather than fires.** A NULL stamp means "never
  auto-configured from an inventory change", which is true of every workspace the
  moment this ships -- including ones that already have rows and already have the
  flag on. Firing on that would configure a fleet for a change nobody made, on
  upgrade. So the first cycle records the token and the second one onwards acts.
* **It passes the workspace's `default-limit`**, not a narrowed one. Narrowing a
  derived configure to the hosts the change actually affected is #2229, and it
  needs a diff of two resolved inventories: this trigger's signal is an equality
  token, which says *something* changed and never *what*.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import inventory_resolve, run_service, workspace_settings

logger = get_logger(__name__)

#: Where the previous tick's observation lives. Transient by design -- see the
#: module docstring on why this is not a second column.
_SETTLE_PREFIX = "tp:inv_settle:"

#: Long enough that the key outlives the gap between two ticks comfortably, short
#: enough that a workspace which stops changing stops costing a key.
_SETTLE_TTL_SECONDS = 3600

#: A configure already on its way. NOT "any non-terminal run", which is the
#: recorded drift-detection incident: a `planned` run awaiting an operator's
#: confirm can sit indefinitely, and keying on it froze drift for ever. A
#: configure has no confirm step -- a standalone one goes `queued` ->
#: `configuring` -> terminal -- so this cannot deadlock.
_CONFIGURE_IN_FLIGHT = ("pending", "queued", "configuring")


async def _configure_in_flight(db: AsyncSession, workspace_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(Run.id)
        .where(
            Run.workspace_id == workspace_id,
            Run.is_configure.is_(True),
            Run.status.in_(_CONFIGURE_IN_FLIGHT),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _create_run(db: AsyncSession, ws: Workspace) -> Run | None:
    """One configure for this workspace, against its current code.

    The LATEST UPLOADED configuration version, not the latest applied one: the
    inventory moved and the playbook did not, so the operator's current code is
    what should run. (Drift detection deliberately picks the latest *applied* CV
    instead, because it diffs against the state those bytes produced -- a
    different question.)
    """
    cv = await run_service.get_latest_uploaded_cv(db, ws.id)
    if cv is None:
        logger.debug(
            "Skipping auto-configure: no configuration version to take a playbook from",
            workspace=ws.name,
        )
        return None
    try:
        return await run_service.create_run(
            db,
            workspace=ws,
            message="Auto-configure: the inventory changed",
            source="auto-configure",
            configuration_version_id=cv.id,
            is_configure=True,
        )
    except (run_service.PlanApplyNotEnabled, workspace_settings.ConfigureNotEnabled):
        # A run the PLATFORM decided on, so the refusal is caught and skipped
        # rather than raised: erroring once per cycle about a workspace behaving
        # exactly as it declares is noise, which is the #2116 contract's own
        # reasoning for this half of the choice.
        #
        # Belt and braces rather than a live path -- the cycle's query already
        # requires `configure_enabled`, and a standalone configure is the only
        # shape this creates, so `require_run_operations` asks about nothing
        # else. It is caught anyway because the filter and the rule are two
        # places, and the gate that made me write this exists because they can
        # drift. DEBUG, so a workspace stuck in that state is not a warning per
        # cycle for ever.
        logger.debug(
            "Skipping auto-configure: the workspace does not declare the operation",
            workspace=ws.name,
        )
        return None


async def auto_configure_cycle() -> None:
    """One pass over the workspaces that asked for this.

    Registered with the distributed scheduler, so exactly one replica runs it per
    interval. The `auto_configure_enabled` filter is what keeps it cheap: it is
    the small minority of workspaces by design.
    """
    from terrapod.redis.client import get_redis_client

    async with get_db_session() as db:
        result = await db.execute(
            select(Workspace).where(
                Workspace.auto_configure_enabled.is_(True),
                # A workspace that declares no configure operation cannot run
                # one; `create_run` would refuse it anyway, so this is about not
                # logging a refusal once per tick for ever.
                Workspace.configure_enabled.is_(True),
            )
        )
        workspaces = list(result.scalars().all())
        if not workspaces:
            return

        redis = get_redis_client()
        for ws in workspaces:
            try:
                if ws.locked:
                    continue

                current = await inventory_resolve.platform_rev(db, ws.id)
                key = f"{_SETTLE_PREFIX}{ws.id}"

                seen = await redis.get(key)
                if isinstance(seen, bytes):
                    seen = seen.decode()
                if seen != current:
                    # Still moving. Record and wait a tick, so a fifty-host apply
                    # coalesces into one run rather than one per tick.
                    await redis.setex(key, _SETTLE_TTL_SECONDS, current)
                    continue

                if ws.auto_configure_inventory_stamp is None:
                    # First observation. Adopt rather than fire -- see the module
                    # docstring: firing here would configure every opted-in
                    # workspace on upgrade for a change nobody made.
                    ws.auto_configure_inventory_stamp = current
                    await db.commit()
                    logger.info(
                        "Adopted the current inventory as the auto-configure baseline",
                        workspace=ws.name,
                    )
                    continue

                if ws.auto_configure_inventory_stamp == current:
                    continue

                if await _configure_in_flight(db, ws.id):
                    # Deliberately NOT stamped: the change has not been acted on,
                    # so a later tick must still see it.
                    logger.debug(
                        "Skipping auto-configure: one is already on its way",
                        workspace=ws.name,
                    )
                    continue

                run = await _create_run(db, ws)
                if run is None:
                    continue
                ws.auto_configure_inventory_stamp = current
                await db.commit()
                logger.info(
                    "Auto-configure run created from an inventory change",
                    workspace=ws.name,
                    run_id=str(run.id),
                )
            except Exception as exc:  # noqa: BLE001
                # One workspace per cycle, never the cycle: the same containment
                # the VCS pollers have, for the same reason.
                logger.warning(
                    "Auto-configure failed for one workspace",
                    workspace=ws.name,
                    err=str(exc),
                    exc_info=True,
                )
