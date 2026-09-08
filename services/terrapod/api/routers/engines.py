"""What each execution engine is, and what its run states mean (#1407 §3, #1521).

Runs and workspaces carry an `engine`. That names the family the work belongs to
— Terraform today, Pulumi and Ansible later — but on its own it leaves a consumer
to *infer* what the run's state means, because the internal status names are the
platform's and do not change: a run is `planning` whatever engine it belongs to.

For Terraform that reads correctly by accident. For Pulumi it does not — the same
state is a *preview*, and for Ansible it is a *check*. Leaving that difference to
be worked out by convention is exactly the "smoothed over" outcome #1407 §3
forbids, and it would be worked out separately (and eventually inconsistently) in
the web UI, the SDK, the provider and the MCP tools.

So the vocabulary is published once, here, and every consumer resolves the same
words from the same place. What is served is the engine's own *tokens*, never
prose: these are identifiers a client maps to its own translated strings, so
nothing user-facing is pinned to English by an API response.

Endpoints (under /api/terrapod/v1):
    GET /engines             the engines this deployment serves
    GET /engines/{name}      one of them

Read-only, and readable by any authenticated user: this is a description of the
deployment's capabilities, not of anybody's resources, so there is nothing here
to scope by RBAC. Which engines appear is decided by the engine registry, so a
gated-off engine is absent rather than advertised and refused (#1429).
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.pagination import paginate
from terrapod.engines import known_engines, strategy_for

router = APIRouter(tags=["engines"])


def _engine_json(name: str) -> dict:
    """One engine, in the house JSON:API shape.

    `status-phases` is the part that earns this endpoint. It maps each internal
    run status onto the phase *this* engine calls it, which is what lets a client
    report a Pulumi run as previewing rather than planning without hard-coding a
    mapping it cannot see.
    """
    strategy = strategy_for(name)
    return {
        "type": "engines",
        "id": strategy.name,
        "attributes": {
            "name": strategy.name,
            # The engine's phases, in the order a run performs them.
            "phases": list(strategy.phases),
            # Internal run status -> this engine's phase.
            "status-phases": dict(strategy.status_phases),
            # Which binary a workspace gets when it does not choose one. For
            # Terraform this is the tofu/terraform split — a choice *within* the
            # engine, not a different engine.
            "default-execution-backend": strategy.default_execution_backend,
            # The message-catalogue namespace holding this engine's display
            # words. A token, not a string: the words themselves are translated
            # per locale and so cannot live on an API response.
            "vocabulary": strategy.vocabulary,
        },
    }


@router.get("/engines")
async def list_engines(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """The engines this deployment serves."""
    items = [_engine_json(name) for name in known_engines()]
    page, meta = paginate(items, request)
    return {"data": page, "meta": meta}


@router.get("/engines/{engine_name}")
async def get_engine(
    engine_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """One engine.

    404 rather than an empty body for an engine this deployment does not serve,
    so a client asking about Pulumi on a Terraform-only install is told plainly
    rather than left to interpret silence.
    """
    if engine_name not in known_engines():
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine_name}")
    return {"data": _engine_json(engine_name)}
