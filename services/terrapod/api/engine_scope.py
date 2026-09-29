"""Which engine's rows a surface may serve, in one place (#1407 §2, #1487, #1572).

**The TFE-compatible surface never learns about another engine.** That was always
the rule, and `tfe_v2._engine_filter` enforced it — but only in `tfe_v2.py`, while
eight other router mounts serve workspace sub-resources on that same surface. The
result was one workspace answering two ways:

    GET /api/tfe/v2/workspaces/{id}                   404   (filtered)
    GET /api/tfe/v2/workspaces/{id}/runs              200   (not filtered)
    GET /api/tfe/v2/workspaces/{id}/configuration-versions
                                                      200   (not filtered)

A `go-tfe` client told the workspace does not exist, and then handed its runs. The
filter's own docstring predicted exactly this failure — *"the moment a second
appears a missing filter hands a Pulumi workspace to a `terraform` CLI that cannot
parse it"* — it simply lived in one file while the property spanned four.

**Why a router cannot just always filter.** Some of these routers are mounted on
BOTH surfaces (#1898 did it for variables), because Terrapod's own API has to
serve every engine while the compatibility surface serves one. The same handler
must therefore answer differently depending on the door the request came through,
which is what `load_workspace_scoped` does and why it needs the request.

Read `is_tfe_path` for the door; read this for what the door implies.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.prefixes import is_tfe_path
from terrapod.db.models import Run, Workspace
from terrapod.engines import TERRAFORM


def engine_filter(model):
    """Restrict a query to Terraform rows.

    Canonical implementation. `tfe_v2._engine_filter` delegates here rather than
    holding a second copy: two implementations of "which engine may this surface
    see" is how one of them later gets relaxed on its own.

    Name-uniqueness guards must NOT use this. `workspaces.name` is unique
    globally, across engines, so a guard that filtered by engine would decide a
    name was free and then hit an IntegrityError — turning a clean 422 into a 500.
    """
    if model is Run:
        # Runs store no engine (#1536); a run's is its workspace's.
        return Run.workspace_id.in_(select(Workspace.id).where(Workspace.engine == TERRAFORM))
    return model.engine == TERRAFORM


async def load_workspace_scoped(
    workspace_id: str,
    db: AsyncSession,
    *,
    request: Request | None,
) -> Workspace:
    """Load a workspace, 404ing on the TFE surface if it belongs to another engine.

    The scoping is on the REQUEST's prefix, not on a caller's flag, because the
    same router object is mounted on both surfaces and the handler cannot know
    which one it is serving any other way.

    ``request`` of None reads as the TFE surface — the conservative direction. A
    caller that forgets to thread it can then only be too strict, never leak.

    404 rather than 403, deliberately: on the compatibility surface a workspace
    belonging to another engine does not exist as far as that client is concerned,
    and saying "forbidden" would confirm it exists to a client that has no
    business knowing.
    """
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == ws_uuid,
            # One statement so the source-introspection guard in
            # tests/api/test_v2_engine_filter.py can see the filter. It reads
            # the statement a `select()` sits in, so splitting this across two
            # lines would make the guard blind to it.
            engine_filter(Workspace) if request is None or is_tfe_path(request.url.path) else True,
        )
    )
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws
