"""Mask sensitive inventory values on the read surface (GHSA-23jf-9q8w-5f37).

The per-variable routes have always masked: `_var_json` returns `***` when
`var.sensitive`. The **resolved** view did not, and it is gated on
`INVENTORY_READ`, which sits in the READ preset -- so any read-tier principal
could recover `ansible_become_password`, an SSH password, or anything else an
operator had marked sensitive, simply by asking for the resolved inventory
rather than the variable.

**This masks on the HTTP surface only, and that distinction is the whole
design.** A configure run gets its inventory from the stored run artifact
(`run_artifacts.download_resolved_inventory`), not from this route, and it
needs the real values to configure anything. So masking belongs *after*
resolution, at the point the document is serialised for a human or an agent --
never inside `resolve_inventory`, which both paths share. Masking there would
turn every configure run into one that sets passwords to `***`.

**Masking is by KEY, across the whole workspace, deliberately.** A resolved
value is merged from several levels, so "is this particular value sensitive"
has no single answer -- the same key can be declared on a host, a group and
globally, with the flag set on only one of them. Masking a key that is
sensitive *anywhere* in the workspace over-masks in the ambiguous case, which
is the direction a disclosure guard should fail in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    InventoryGlobalVar,
    InventoryGroup,
    InventoryGroupVar,
    InventoryHost,
    InventoryHostVar,
)

#: Must match `inventory._MASKED`; `test_the_mask_matches_the_per_variable_route`
#: pins the two together, because two spellings of a mask is how one of them
#: stops being recognised as one.
MASK = "***"


async def sensitive_keys(db: AsyncSession, workspace_id: Any) -> set[str]:
    """Every variable key marked sensitive anywhere in this workspace.

    Three tables, because a key is sensitive if it is sensitive at any level.
    """
    keys: set[str] = set()

    host_q = (
        select(InventoryHostVar.key)
        .join(InventoryHost, InventoryHost.id == InventoryHostVar.host_id)
        .where(InventoryHost.workspace_id == workspace_id, InventoryHostVar.sensitive.is_(True))
    )
    group_q = (
        select(InventoryGroupVar.key)
        .join(InventoryGroup, InventoryGroup.id == InventoryGroupVar.group_id)
        .where(InventoryGroup.workspace_id == workspace_id, InventoryGroupVar.sensitive.is_(True))
    )
    global_q = select(InventoryGlobalVar.key).where(
        InventoryGlobalVar.workspace_id == workspace_id,
        InventoryGlobalVar.sensitive.is_(True),
    )

    for q in (host_q, group_q, global_q):
        keys.update((await db.execute(q)).scalars().all())
    return keys


def mask_hosts(hosts: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    """`{host: {key: value}}` with every sensitive key's value replaced."""
    if not keys:
        return hosts
    return {
        host: {k: (MASK if k in keys else v) for k, v in (vars_ or {}).items()}
        for host, vars_ in hosts.items()
    }


def mask_provenance(prov: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    """The provenance document with sensitive values replaced at every level.

    The chain carries one entry per level that set the key, each with its own
    `value` -- so masking only the effective value would leave the secret in the
    chain beside it, which is the same disclosure one field along.
    """
    if not keys:
        return prov
    out: dict[str, Any] = {}
    for host, per_key in prov.items():
        masked_keys: dict[str, Any] = {}
        for key, detail in (per_key or {}).items():
            if key not in keys or not isinstance(detail, dict):
                masked_keys[key] = detail
                continue
            d = dict(detail)
            if "value" in d:
                d["value"] = MASK
            chain = d.get("chain")
            if isinstance(chain, list):
                d["chain"] = [
                    ({**e, "value": MASK} if isinstance(e, dict) and "value" in e else e)
                    for e in chain
                ]
            masked_keys[key] = d
        out[host] = masked_keys
    return out
