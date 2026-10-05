"""Per-workspace cloud identity — the mint, and the signing keys (#1901).

A run's cloud identity is a short-lived RS256 JWT whose claims describe the run:
which workspace, which phase. A cloud federates to Terrapod as an OIDC identity
provider and its trust policy conditions on those claims, so two workspaces on
one agent pool can hold different cloud permissions without a second listener.

**Nothing here is cloud-specific, deliberately.** Terrapod mints a token and the
runner writes it to a file; which cloud consumes it, and how, is the operator's
provider configuration and the docs' problem. That is what makes the same token
work for AWS, Azure, GCP, Vault's JWT auth and anything else that federates to an
OIDC issuer — and it is why there is no role ARN, no tenant id and no credential
config anywhere in this file.

Endpoints (all under /api/terrapod/v1):
    Runner protocol (runner token, run_id-scoped):
        POST /runs/{run_id}/cloud-identity-token    mint this run's identity token
    Signing keys (platform admin):
        GET  /oidc/signing-keys                     what is published, and when it signs
        POST /oidc/signing-keys/actions/rotate      add a key, retire the current one
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_runner_for_run,
)
from terrapod.api.ids import parse_id
from terrapod.db.models import OIDCSigningKey, Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(tags=["cloud-identity"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


@router.post("/runs/{run_id}/cloud-identity-token")
async def mint_cloud_identity_token(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mint this run's cloud identity token.

    **204 when the workspace mints nothing.** The runner calls this
    unconditionally — it has no other way to know whether the workspace opted in,
    and giving it one would mean a new wire field and a new way for a lagging
    listener to be wrong. So "not opted in" has to be distinguishable from
    "broken", because the runner's behaviour differs completely: on 204 it takes
    no action and the run authenticates to the cloud with the agent pool's own
    identity, exactly as before; on anything else it fails the run (#1442's rule
    — credentials were asked for and could not be had, and continuing would mean
    silently running under broader permissions than the operator chose).

    **The phase comes from the presented token, never from the request.** A
    plan-phase runner asking for the apply identity is the whole thing this
    guards: put write permissions behind a trust condition on `phase: apply` and
    a speculative pull-request plan structurally cannot assume that role, because
    every PR-driven run is plan-only.

    A token minted before the phase claim existed carries no phase. That is read
    as "makes no claim" and the minted JWT carries no `phase` either, so an
    operator's trust policy conditioning on it simply will not match — refusing
    the credential rather than quietly widening it.
    """
    from terrapod.api.routers.oidc_issuer import issuer_url
    from terrapod.auth.oidc_signing import sign_identity_token
    from terrapod.config import settings

    require_runner_for_run(user, run_id)

    if not settings.auth.oidc_issuer.enabled:
        # Not an error the runner should fail on: an operator who has not
        # published an issuer has not opted this deployment in at all.
        return Response(status_code=204)

    run = await db.get(Run, parse_id(run_id, "run-", detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    audiences = list(run.oidc_audiences or [])
    if not audiences:
        return Response(status_code=204)

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    phase = user.run_phase
    claims: dict = {
        "iss": issuer_url(),
        # Composite `sub`, phase last, in the colon-delimited shape HCP Terraform
        # and GitHub both use. It carries the phase as well as the discrete claim
        # below because Azure federated identity credentials match on issuer,
        # subject and audience ONLY — no arbitrary claims — so `sub` is the one
        # place a phase condition can be expressed there. Clouds that can read
        # arbitrary claims should condition on `workspace` and `phase` instead,
        # which needs no wildcard.
        "sub": f"workspace:{ws.name}" + (f":phase:{phase}" if phase else ""),
        "aud": audiences,
        "workspace": ws.name,
        "workspace_id": str(ws.id),
        "run_id": str(run.id),
        # Single-organization deployment, so this is the literal `default`. Here
        # because a cloud trust policy written against two Terrapod deployments
        # wants something to tell them apart, and the issuer URL already does
        # that — this is for symmetry with the claim set other issuers publish.
        "terrapod_organization": "default",
    }
    if phase:
        claims["phase"] = phase

    token = sign_identity_token(claims, ttl_seconds=settings.auth.oidc_issuer.token_ttl_seconds)
    logger.info(
        "minted cloud identity token",
        run_id=str(run.id),
        workspace=ws.name,
        phase=phase,
        audiences=audiences,
    )
    return JSONResponse(
        content={
            "token": token,
            "expires_in": settings.auth.oidc_issuer.token_ttl_seconds,
            "phase": phase,
            "audiences": audiences,
        }
    )


@router.get("/oidc/signing-keys")
async def list_signing_keys(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What is published, and which key is signing.

    Public key material only — the private half never leaves the API. `kid` is an
    RFC 7638 thumbprint, so it is the same value a cloud sees in a token header.
    """
    from terrapod.auth.oidc_signing import get_signing_key

    rows = (
        (await db.execute(select(OIDCSigningKey).order_by(OIDCSigningKey.created_at.asc())))
        .scalars()
        .all()
    )
    try:
        signing_kid = get_signing_key().kid
    except RuntimeError:
        signing_kid = None

    return JSONResponse(
        content={
            "data": [
                {
                    "type": "oidc-signing-keys",
                    "id": k.kid,
                    "attributes": {
                        "kid": k.kid,
                        "created-at": _rfc3339(k.created_at),
                        "activates-at": _rfc3339(k.activates_at),
                        "retired-at": _rfc3339(k.retired_at),
                        "signing": k.kid == signing_kid,
                    },
                }
                for k in rows
            ],
            "meta": {"signing-kid": signing_kid},
        }
    )


@router.post("/oidc/signing-keys/actions/rotate")
async def rotate_signing_key_route(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Add a key and retire the one currently signing.

    The new key is published immediately and starts signing only after the
    propagation window, because the clouds cache the JWKS and a token signed with
    a key they have not fetched yet cannot be verified. The retired key stays
    published for its grace window, because the tokens it already signed are
    still inside their own lifetime.
    """
    from terrapod.auth.oidc_signing import rotate_signing_key

    try:
        new_key = await rotate_signing_key(db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    row = (
        await db.execute(select(OIDCSigningKey).where(OIDCSigningKey.kid == new_key.kid))
    ).scalar_one()
    return JSONResponse(
        status_code=201,
        content={
            "data": {
                "type": "oidc-signing-keys",
                "id": row.kid,
                "attributes": {
                    "kid": row.kid,
                    "created-at": _rfc3339(row.created_at),
                    "activates-at": _rfc3339(row.activates_at),
                    "retired-at": None,
                    "signing": False,
                },
            },
            "meta": {
                "note": (
                    "Published now; signs from activates-at, once the clouds have had "
                    "a chance to fetch the JWKS."
                )
            },
        },
    )
