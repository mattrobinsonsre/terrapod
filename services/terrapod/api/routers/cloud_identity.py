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
        GET  /runs/{run_id}/cloud-identity-targets  which providers this run mints for
        POST /runs/{run_id}/cloud-identity-token    mint this run's identity token
    Deployment configuration (any authenticated user):
        GET  /oidc/audience-defaults                the deployment-wide audience catalogue
    Signing keys (platform admin):
        GET  /oidc/signing-keys                     what is published, and when it signs
        POST /oidc/signing-keys/actions/rotate      add a key, retire the current one
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
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
    target: str = Query(
        default="",
        description=(
            "The provider configuration this token is for, as the runner "
            "discovered it: `aws`, or `provider.alias` for one aliased "
            "configuration. Resolved specific-then-general."
        ),
    ),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mint one target's cloud identity token for this run.

    **One token per target, carrying only that target's audiences.** A token
    audienced for several targets is replayable between them, and AWS refuses a
    multi-valued `aud` outright, so the runner asks once per provider
    configuration it discovered and writes each answer to its own path.

    **204 means "nothing to deliver", and the runner must not treat it as an
    error.** Three distinct cases answer 204, deliberately:

    * the deployment publishes no issuer — not opted in at all;
    * nothing maps to this target — most providers in most workspaces;
    * **the request named no target at all**, which is a runner image older than
      per-target minting. That one matters: 400 would be the obvious answer and
      it would make every run on a lagging runner FAIL, when the designed
      behaviour is that it falls through to the agent pool's own identity
      exactly as before this feature existed. The fall-through is permanent and
      supported, so a request we cannot serve must look like "nothing here"
      rather than like a fault.

    **The phase comes from the presented token, never from the request.** A
    plan-phase runner asking for the apply identity is the whole thing this
    guards: put write permissions behind a trust condition on `phase: apply` and
    a speculative pull-request plan structurally cannot assume that role,
    because every PR-driven run is plan-only.

    **The run's snapshot is checked, not merely used.** `Run.oidc_audiences` is
    the mapping resolved when the run was created — what the plan was reviewed
    under — and this re-resolves the requested target from live configuration
    and refuses when the two disagree. Minting from the snapshot alone would
    hand an apply a token that matches the reviewed plan while the cloud has
    moved on, and it would then be rejected at the cloud's token exchange, deep
    inside the engine and possibly after a partial apply. Refusing here is the
    same shape as a saved plan refused because the state serial moved: fail
    before anything executes, and say what changed.

    The check is deliberately one lookup on each side. The runner asks about one
    target at a time, so there is no map to diff and no need to know which
    targets the plan used — the richer, named comparison belongs on the confirm
    path, where it costs nothing and a human is reading it.

    A token minted before the phase claim existed carries no phase. That is read
    as "makes no claim" and the minted JWT carries no `phase` either, so an
    operator's trust policy conditioning on it simply will not match — refusing
    the credential rather than quietly widening it.
    """
    from terrapod.api.routers.oidc_issuer import issuer_url
    from terrapod.auth.oidc_signing import sign_identity_token
    from terrapod.config import settings
    from terrapod.services import cloud_identity_resolver

    require_runner_for_run(user, run_id)

    if not settings.auth.oidc_issuer.enabled:
        # Not an error the runner should fail on: an operator who has not
        # published an issuer has not opted this deployment in at all.
        return Response(status_code=204)

    if not target:
        # A lagging runner image. See the docstring — 204, never 400.
        logger.info("cloud identity mint with no target — lagging runner image", run_id=run_id)
        return Response(status_code=204)

    run = await db.get(Run, parse_id(run_id, "run-", detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    snapshot = run.oidc_audiences or {}
    audiences = cloud_identity_resolver.audiences_for_target(snapshot, target)
    if audiences is None:
        # Nothing maps to this provider for this run. The common answer.
        return Response(status_code=204)

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    live = cloud_identity_resolver.resolve_for_workspace(ws, settings=settings)
    if cloud_identity_resolver.target_changed(snapshot, live, target):
        # 409, not 404 or 422: the request is well formed and the caller is
        # entitled to it — the world moved underneath the run. The operator's
        # action is to re-plan, so say that rather than describing a mismatch.
        raise HTTPException(
            status_code=409,
            detail=(
                f"The cloud identity configuration for {target!r} has changed since this "
                f"run was created, so the identity this run would present is no longer the "
                f"one its plan was reviewed under. Queue a new run to pick up the current "
                f"configuration."
            ),
        )

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

    # Record what was actually minted, so the confirm-time staleness check can
    # be scoped to the identities this run PRESENTED rather than to the ones it
    # was configured for. The configured snapshot is the merged map and carries
    # deployment-wide targets a workspace may never use, so checking against it
    # would let one catalogue edit refuse every pending apply in the fleet.
    #
    # Appended after signing, never before: a recorded target that was never
    # served would make the confirm check refuse an apply over an identity the
    # plan never presented.
    minted = list(run.oidc_minted_targets or [])
    if target not in minted:
        minted.append(target)
        run.oidc_minted_targets = minted
        await db.commit()

    logger.info(
        "minted cloud identity token",
        run_id=str(run.id),
        workspace=ws.name,
        phase=phase,
        target=target,
        audiences=audiences,
    )
    return JSONResponse(
        content={
            "token": token,
            "expires_in": settings.auth.oidc_issuer.token_ttl_seconds,
            "phase": phase,
            # Echoed so the runner writes the file under the name it asked for
            # rather than re-deriving it, and so a log line names which target a
            # token was for without the token being in it.
            "target": target,
            "audiences": audiences,
        }
    )


@router.get("/runs/{run_id}/cloud-identity-targets")
async def list_cloud_identity_targets(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Which provider configurations this run mints a token for.

    **This exists so the runner does not have to run `tofu graph` on every run
    in the fleet.** Discovering which provider configurations a root module uses
    means asking the engine, and the overwhelming majority of workspaces
    configure no cloud identity at all. Answering "nothing" here costs one
    indexed row read and lets those runs skip discovery entirely -- so the
    feature adds no cost, and no new failure mode, to a run that does not use
    it. That is also what makes failing closed on a discovery error correct
    rather than reckless: by the time the runner reaches for the engine, the
    operator has asked for federation.

    **Target names only -- never the audiences.** An audience is the value a
    cloud trust policy matches on, so the set of them is worth more to an
    attacker than any one token: it names the roles this deployment can ask to
    assume. The runner does not need them (it writes a file and the engine reads
    it), so they stay in the per-target mint response and are never enumerable.

    **The run's snapshot, not live configuration.** A target added to the
    workspace after this run was created is deliberately absent: the plan was
    reviewed without it, and the mint endpoint would refuse it anyway. Reading
    live configuration here would have the runner discover a target it then
    could not mint.

    204 when there is nothing -- the issuer is not published, or this run maps
    no providers. The runner treats that exactly as it treats a 204 from the
    mint: fall through to the agent pool's own identity, as before this feature
    existed.
    """
    from terrapod.config import settings

    require_runner_for_run(user, run_id)

    if not settings.auth.oidc_issuer.enabled:
        return Response(status_code=204)

    run = await db.get(Run, parse_id(run_id, "run-", detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    targets = sorted(run.oidc_audiences or {})
    if not targets:
        return Response(status_code=204)

    return JSONResponse(content={"targets": targets})


@router.get("/oidc/audience-defaults")
async def get_oidc_audience_defaults(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """The deployment-wide audience catalogue a workspace's own map merges over.

    Exists so a practitioner composing `oidc_audiences` can see what they would
    INHERIT. Without it the two-level merge is only observable through its
    result: a workspace read returns the merged map with no indication of which
    entries the workspace owns, so an operator cannot tell an inherited entry
    from one of their own, and a consumer that writes the merged value back
    promotes every inherited entry into an override.

    **Any authenticated user, not platform admin.** The gate is deliberate
    rather than lax. A workspace read already returns the merged map to anyone
    who can read the workspace, so the effective audiences for a workspace are
    disclosed at that tier today; this adds only the entries a workspace does
    not override. Requiring admin would put it out of reach of exactly the
    person it is for -- a workspace owner deciding whether to override a key --
    while disclosing nothing that tier cannot already see. Knowing an audience
    grants nothing on its own: the cloud's own trust policy is the gate, and
    minting needs a phase-bound runner token scoped to a run on that workspace.

    Contrast the runner-facing targets route above, which returns NAMES only.
    The asymmetry is the point: a runner writes a file and the engine reads it,
    so it has no use for the values, and the set of audiences names the roles
    this deployment can ask to assume.

    Empty when the deployment configures no catalogue, which is the default --
    not an error, and not the same as the issuer being disabled.
    """
    from terrapod.config import settings

    cfg = settings.auth.oidc_issuer
    return JSONResponse(
        content={
            "data": {
                "type": "oidc-audience-defaults",
                "id": "default",
                "attributes": {
                    # Copied, not handed out: this is a live config object and a
                    # serializer must never be the thing that lets a caller
                    # mutate process-wide settings.
                    "audiences": {k: list(v) for k, v in (cfg.audiences or {}).items()},
                    "issuer-enabled": bool(cfg.enabled),
                },
            }
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
