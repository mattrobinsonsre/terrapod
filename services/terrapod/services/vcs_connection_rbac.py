"""May this principal point something at this VCS connection? (GHSA-v8g7-pqrj-8mcm)

A VCS connection is an admin-managed resource. It holds a GitHub App installation
or a GitLab access token, and it covers **every repository** that credential can
reach — so naming one is a grant, not a reference. Only admins can create, list or
view connections.

But a workspace serialises `vcs-connection-id`, and that attribute is returned to
anyone with **read** on the workspace. So the id is discoverable by design, and
until this check existed nothing stopped any authenticated user from creating their
own workspace, pointing it at a connection they had no claim to, and obtaining
repository access through someone else's installation.

`8prq` was the same bug from the other end: the token minted for that workspace
carried every permission the App held. Narrowing it to `contents: read` reduced the
blast radius to a cross-installation *read*; this closes the access itself.

**The rule: you may name a connection you already have.** A platform admin may name
any. Anyone else may name a connection only where they already own a workspace using
it — so the grant is one they already hold, and naming it again gains them nothing.
The case this refuses is the one the finding describes: a principal with no claim to
a connection attaching it to a workspace of their own.

The consequence to be aware of, and it is deliberate: the **first** workspace for a
connection must be created by a platform admin, because until one exists there is no
workspace to own. Afterwards an ordinary user can create as many as they like against
it. An operator who needs the old behaviour can set
`vcs.require_connection_authorization: false`, which is there for exactly that and is
the setting to reach for rather than granting someone admin.

Deliberately NOT keyed on labels. Every other RBAC'd entity here carries `labels` and
`owner_email`; `VCSConnection` carries neither, so there is no dimension to match on
without a schema change. Giving it one, and gating it the way agent pools and the
registry are gated, is the 2.0 answer — this is the version that fits a patch.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import Workspace


async def may_reference_connection(
    db: AsyncSession,
    *,
    conn_id: uuid.UUID,
    actor_email: str,
    is_platform_admin: bool,
) -> bool:
    """True if `actor_email` may point a workspace at `conn_id`.

    `is_platform_admin` is passed in rather than resolved here so the one caller
    that has no live principal — the run-time credential mint, which authorises
    against the workspace's owner — cannot accidentally grant itself admin.
    """
    if not settings.vcs.require_connection_authorization:
        return True
    if is_platform_admin:
        return True
    if not actor_email:
        return False
    # Owns a workspace already using it, so the grant is one they already hold.
    row = (
        await db.execute(
            select(Workspace.id)
            .where(
                Workspace.vcs_connection_id == conn_id,
                Workspace.owner_email == actor_email,
            )
            .limit(1)
        )
    ).first()
    return row is not None


def refusal_detail(conn_id: uuid.UUID) -> str:
    """What the caller is told. Names the id, the reason and the way out."""
    return (
        f"Not authorized to use VCS connection vcs-{conn_id}. A VCS connection "
        "covers every repository its credential can reach, so naming one grants "
        "that access. You may name a connection you already own a workspace on; a "
        "platform admin can create the first workspace for a connection, or set "
        "`api.config.vcs.require_connection_authorization: false` to restore the "
        "previous behaviour of accepting any connection id."
    )
