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
        POST /runs/{run_id}/cloud-identity-tokens   mint a token per provider the run uses
    Deployment configuration (any authenticated user):
        GET  /oidc/audience-defaults                the deployment-wide audience catalogue
    Signing keys (platform admin):
        GET  /oidc/signing-keys                     what is published, and when it signs
        POST /oidc/signing-keys/actions/rotate      add a key, retire the current one
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
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
from terrapod.services import workspace_settings

router = APIRouter(tags=["cloud-identity"])

#: Cap on how many provider configurations one run may ask to mint for. The
#: runner bounds its own list too; this bounds it again, because a runner token
#: is a credential a run holds rather than a reason to trust the body.
MAX_TARGETS = 100

logger = get_logger(__name__)


def _rfc3339(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


class CloudIdentityMintRequest(BaseModel):
    """What the runner discovered, and how much it trusts its own answer.

    The outcome travels because only the API can judge it: a graph that could
    not be read is fatal for a workspace holding identity and irrelevant for one
    that is not, and the runner discovers before it knows which it is in.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    providers: list[str] = Field(
        default_factory=list,
        max_length=MAX_TARGETS,
        description=(
            "The provider configurations this run's root module uses, as "
            "`type` or `type.alias`. Empty is meaningful: a configuration may "
            "declare no provider. Ignored for an engine that cannot discover "
            "them, whose runner sends nothing and is minted the workspace's "
            "whole resolved mapping instead."
        ),
    )
    discovery: Literal["ok", "failed", "unparsed"] = Field(
        default="ok",
        description=(
            "`ok` — the list is authoritative. `failed` — the graph command "
            "errored. `unparsed` — it ran and named provider nodes none of "
            "which could be read."
        ),
    )
    discovery_detail: str = Field(
        default="",
        alias="discovery-detail",
        max_length=400,
        description="Why, for the operator, when discovery is not `ok`.",
    )


@router.post("/runs/{run_id}/cloud-identity-tokens")
async def mint_cloud_identity_tokens(
    payload: CloudIdentityMintRequest,
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mint this run's cloud identity tokens — one per provider configuration.

    **One token per target, carrying only that target's audiences.** A token
    audienced for several targets is replayable between them, and AWS refuses a
    multi-valued `aud` outright, so each provider configuration the runner
    discovered gets its own token and the runner writes each to its own path.

    **One request, not one per target, and not a gate followed by N mints.**
    Which providers a run uses is a property of the configuration and only the
    runner can answer it; which identities a workspace holds is a property of
    the platform and only this can. So the intersection costs one hop whichever
    end sends its half, and the runner sending its list up is the cheaper
    direction — a gate request first would make every federated run pay two
    hops to save an engine invocation on the runs that are not federated. The
    engine's graph is a static walk needing no network, no credentials and no
    state, so that was never a trade worth making.

    **204 means "nothing to deliver", and the runner must not treat it as an
    error.** Four distinct cases answer 204, deliberately:

    * the deployment publishes no issuer — not opted in at all;
    * the workspace holds no identity — the normal posture for most workspaces;
    * nothing it holds is used by this configuration;
    * **the request named no providers and reported `ok`** — a configuration
      that declares none, which cannot reach a cloud and so needs no token.

    The fall-through to the agent pool's own identity is permanent and
    supported, so anything we cannot serve must look like "nothing here" rather
    than like a fault. A runner image predating this route never calls it and
    gets the same outcome by a different path.

    **A discovery the runner could not trust is refused, but only when it
    matters.** `failed` or `unparsed` both arrive as an empty provider list,
    which is indistinguishable from a provider-less configuration — so taking
    them at face value would be a silent fall-through to the pool's broader
    identity for exactly the workspaces that were deliberately moved off it.
    The order below is therefore load-bearing: a workspace holding no identity
    is answered 204 **before** the outcome is examined, so a graph failure
    cannot fail a run that was never using this feature.

    **An engine that cannot discover gets every identity the workspace
    resolves** (#2006). Terraform's runner enumerates its provider
    configurations with a static `graph` walk; Pulumi's cannot, because a Pulumi
    program is arbitrary code whose provider instances are built at runtime and
    the thing that would run it is what needs the credentials. So for a
    non-discovering engine this mints the whole resolved mapping rather than an
    intersection, and `discovers_provider_configurations` on the engine strategy
    is what decides -- read here off the workspace row, because the runner image
    does not ship `terrapod.engines` and a runner's claim about its own engine
    would be the runner's rather than the platform's.

    Two consequences, both deliberate. The discovery outcome is not examined for
    such an engine, because its runner never ran a graph command and an outcome
    describing one carries no information. And `MAX_TARGETS` stops being slack
    and becomes the real bound: it is enforced with a 409 rather than
    truncating, because a silently shortened token set looks complete and is
    not -- the missing one surfaces inside the engine at the cloud's token
    exchange, with an error naming neither the file nor the reason.

    **The phase comes from the presented token, never from the request.** A
    plan-phase runner asking for the apply identity is the whole thing this
    guards: put write permissions behind a trust condition on `phase: apply` and
    a speculative pull-request plan structurally cannot assume that role,
    because every PR-driven run is plan-only.

    **The run's snapshot is checked, not merely used.** `Run.oidc_audiences` is
    the mapping resolved when the run was created — what the plan was reviewed
    under — and this re-resolves each requested target from live configuration
    and refuses when the two disagree. Minting from the snapshot alone would
    hand an apply a token that matches the reviewed plan while the cloud has
    moved on, and it would then be rejected at the cloud's token exchange, deep
    inside the engine and possibly after a partial apply. Refusing here is the
    same shape as a saved plan refused because the state serial moved: fail
    before anything executes, and say what changed.

    The check stays one lookup per side per target. It is over the targets this
    request asks for, never the whole snapshot — the snapshot is the merged map
    and carries deployment-wide targets a workspace may never use, so checking
    all of it would let one catalogue edit refuse runs that could not present
    the changed identity in the first place.

    A token minted before the phase claim existed carries no phase. That is read
    as "makes no claim" and the minted JWT carries no `phase` either, so an
    operator's trust policy conditioning on it simply will not match — refusing
    the credential rather than quietly widening it.
    """
    from terrapod.api.routers.oidc_issuer import issuer_url
    from terrapod.auth.oidc_signing import sign_identity_token
    from terrapod.config import settings
    from terrapod.engines import discovers_provider_configurations
    from terrapod.services import cloud_identity_resolver, run_service

    require_runner_for_run(user, run_id)

    if not settings.auth.oidc_issuer.enabled:
        # Not an error the runner should fail on: an operator who has not
        # published an issuer has not opted this deployment in at all.
        return Response(status_code=204)

    run = await db.get(Run, parse_id(run_id, "run-", detail="Run not found"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # A runner token is good until it expires -- run state is not checked when it
    # is verified -- and the default TTL is an hour. This is the first endpoint
    # that converts one into CLOUD credentials, so the missing liveness check
    # costs more here than elsewhere: code in the plan pod that exfiltrates the
    # token could keep POSTing here after the run finished, drawing fresh
    # short-lived credentials for the workspace's identity from anywhere, for the
    # remainder of the hour. A finished run has no legitimate reason to mint.
    if run.status in run_service.TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=(f"Run is {run.status} — a finished run cannot mint cloud identity tokens."),
        )

    snapshot = run.oidc_audiences or {}
    if not snapshot:
        # This workspace holds no identity, so the discovery outcome cannot
        # matter -- checked BEFORE the outcome, so a graph failure never fails a
        # run that is not using this feature. See the docstring.
        return Response(status_code=204)

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Whether the runner could have discovered anything is a property of the
    # ENGINE, read here off the workspace row rather than taken from the request
    # (#2006). Two reasons it belongs on this side: the runner image does not
    # ship `terrapod.engines` at all, and a claim about which engine a runner is
    # would be the runner's own, where this is the platform's.
    discovers = discovers_provider_configurations(ws.engine)

    if discovers and payload.discovery != "ok":
        # 409, not 422: the request is well formed and the caller is entitled to
        # it. The runner could not determine which identities to present, and
        # this workspace has some, so there is no safe answer -- falling through
        # would hand the run the agent pool's broader identity under the name of
        # a workspace that was moved off it.
        #
        # Gated on `discovers` because for an engine that cannot discover there
        # was nothing to go wrong: its runner never runs a graph command, so an
        # outcome describing one carries no information about this run.
        raise HTTPException(
            status_code=409,
            detail=(
                f"This workspace is configured for cloud identity federation, but the "
                f"run could not determine which provider configurations it uses "
                f"({payload.discovery}), so there is no way to tell which identity to "
                f"present. {payload.discovery_detail}".strip()
            ),
        )

    # Which targets this run gets. For an engine that discovered its own
    # provider configurations this is the intersection of what it uses with what
    # the workspace holds; for one that cannot discover, it is everything the
    # workspace holds.
    resolved: dict[str, list[str]] = {}

    if discovers:
        # Only the intersection, and "in the mapping" means the RESOLVER says so,
        # never raw key membership: `vault.eu` is answered by a `vault` entry,
        # which is what lets an operator alias a provider five times without
        # naming every alias in the catalogue. A set intersection looks
        # equivalent and silently mints nothing for every aliased configuration.
        #
        # A configured target the root module never uses is not an error -- the
        # mapping is per workspace and a configuration need not use every
        # provider in it -- and a used provider nothing maps to is the common
        # case for most providers in most workspaces.
        # A dict keyed on the target, so a provider the graph named twice
        # resolves once. The order is taken from `sorted` below rather than here.
        for t in payload.providers:
            # Refuse before resolving, because a target is echoed back and the
            # runner joins it into `<token dir>/<target>/token`. `max_length`
            # above bounds the LIST, never an item, and the lookup splits on the
            # FIRST dot -- so `aws./../vault` resolves through an ordinary `aws`
            # entry and would land an AWS-audienced token at the path the
            # operator's `vault` block reads. Anything holding this run's token
            # can send it, including the workspace's own configuration.
            #
            # And bound the ITEM, not just the list. A name longer than the
            # write path allows cannot match a catalogue key -- those are capped
            # at the same constant -- but it can still resolve through the
            # GENERAL fallback, because the lookup splits on the first dot and
            # `aws.<64KB>` answers through an ordinary `aws` entry. Without this
            # the name is echoed back, joined into a path, written into
            # `oidc_minted_targets` for ever, and named in a log line.
            #
            # Only on this branch: the `else` below iterates the workspace's own
            # stored snapshot, which `validate_oidc_audiences` already capped at
            # the same constant, and a key predating that guard is handled by
            # its 422. This list is the caller's.
            if len(t) > workspace_settings.MAX_OIDC_TARGET_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"provider configuration name is longer than "
                        f"{workspace_settings.MAX_OIDC_TARGET_LEN} characters"
                    ),
                )
            unsafe = cloud_identity_resolver.unsafe_target_reason(t)
            if unsafe:
                raise HTTPException(
                    status_code=400,
                    detail=f"provider configuration name {t!r} {unsafe}",
                )
            audiences = cloud_identity_resolver.audiences_for_target(snapshot, t)
            if audiences is not None:
                resolved[t] = audiences
    else:
        # Mint everything this workspace resolves (#2006). The engine cannot say
        # which subset its program will use, so narrowing would mean guessing,
        # and a guess that comes out short fails inside the engine at the cloud's
        # token exchange -- after the program has started, with an error naming
        # neither the file nor the reason.
        #
        # Every key is taken straight from the snapshot, so `audiences_for_target`
        # answers all of them and the resolver's alias fallback never has to run.
        #
        # The cap stops being slack here and becomes the actual bound, so it is
        # enforced rather than silently truncating: dropping the tail would
        # deliver a token set that looks complete and is not.
        if len(snapshot) > MAX_TARGETS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This workspace resolves {len(snapshot)} cloud identity targets and "
                    f"its engine cannot determine which of them this run uses, so all of "
                    f"them would be minted -- more than the {MAX_TARGETS} this endpoint "
                    f"issues for one run. Narrow the workspace's `oidc_audiences` to the "
                    f"targets it needs."
                ),
            )
        if payload.providers:
            # Not an error, because a future runner may send a list this engine
            # cannot have discovered. Worth a line: it means one side believes
            # discovery happened.
            logger.info(
                "cloud identity: provider list ignored for a non-discovering engine",
                run_id=run_id,
                engine=ws.engine,
                sent=len(payload.providers),
            )
        for t in sorted(snapshot):
            unsafe = cloud_identity_resolver.unsafe_target_reason(t)
            if unsafe:
                # 422 rather than the 400 above: this names the operator's own
                # stored configuration, not anything in the request. Reachable
                # only for a key written before the write-side guard existed.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"This workspace's cloud identity configuration contains the "
                        f"target name {t!r}, which {unsafe}. Correct the workspace's "
                        f"`oidc_audiences` before this run can mint its tokens."
                    ),
                )
            audiences = cloud_identity_resolver.audiences_for_target(snapshot, t)
            if audiences is not None:
                resolved[t] = audiences

    wanted = sorted(resolved)
    if not wanted:
        return Response(status_code=204)

    live = cloud_identity_resolver.resolve_for_workspace(ws, settings=settings)
    changed = [t for t in wanted if cloud_identity_resolver.target_changed(snapshot, live, t)]
    if changed:
        # The operator's action is to re-plan, so say that rather than
        # describing a mismatch. Named, because the runner asked about several
        # and "something moved" would not tell anyone which.
        raise HTTPException(
            status_code=409,
            detail=(
                f"The cloud identity configuration for {', '.join(repr(t) for t in changed)} "
                f"has changed since this run was created, so the identity this run would "
                f"present is no longer the one its plan was reviewed under. Queue a new run "
                f"to pick up the current configuration."
            ),
        )

    phase = user.run_phase
    ttl = settings.auth.oidc_issuer.token_ttl_seconds
    pending: list[tuple[str, list[str], dict]] = []
    for target in wanted:
        audiences = resolved[target]
        claims: dict = {
            "iss": issuer_url(),
            # Composite `sub`, phase last, in the colon-delimited shape HCP
            # Terraform and GitHub both use. It carries the phase as well as the
            # discrete claim below because Azure federated identity credentials
            # match on issuer, subject and audience ONLY -- no arbitrary claims
            # -- so `sub` is the one place a phase condition can be expressed
            # there. Clouds that can read arbitrary claims should condition on
            # `workspace` and `phase` instead, which needs no wildcard.
            "sub": f"workspace:{ws.name}" + (f":phase:{phase}" if phase else ""),
            "aud": audiences,
            "workspace": ws.name,
            "workspace_id": str(ws.id),
            "run_id": str(run.id),
            # Single-organization deployment, so this is the literal `default`.
            # Here because a cloud trust policy written against two Terrapod
            # deployments wants something to tell them apart, and the issuer URL
            # already does that -- this is for symmetry with the claim set other
            # issuers publish.
            "terrapod_organization": "default",
        }
        if phase:
            claims["phase"] = phase
        pending.append((target, audiences, claims))

    # Rule 13: one RS256 signature is ~1ms and this signs once per target, so a
    # full request is ~100ms of blocking CPU on the event loop -- for every
    # tenant, not just this one, and anything holding this run's token can ask.
    # `rotate_signing_key` already does exactly this for its keygen, citing the
    # same rule; the asymmetry was the tell.
    def _sign_all() -> list[dict]:
        return [
            {
                "token": sign_identity_token(claims, ttl_seconds=ttl),
                # Echoed so the runner writes the file under the name it asked
                # for rather than re-deriving it, and so a log line names which
                # target a token was for without the token being in it.
                "target": target,
                "audiences": audiences,
            }
            for target, audiences, claims in pending
        ]

    tokens = await asyncio.to_thread(_sign_all)

    # Record what was actually minted, so the confirm-time staleness check can
    # be scoped to the identities this run PRESENTED rather than to the ones it
    # was configured for. The configured snapshot is the merged map and carries
    # deployment-wide targets a workspace may never use, so checking against it
    # would let one catalogue edit refuse every pending apply in the fleet.
    #
    # Written after signing, never before: a recorded target that was never
    # served would make the confirm check refuse an apply over an identity the
    # plan never presented.
    # Re-read the row FOR UPDATE before appending. The model comment argued this
    # needed no locking because the runner mints "one at a time per phase" and the
    # list "only ever grows, so a superset is still sound" -- but a lost update
    # does not produce a superset, it produces a SUBSET, and concurrent mints are
    # reachable: the runner retries on 5xx, so a first attempt that timed out
    # after committing can overlap its own retry. A dropped target then falls
    # outside the confirm-time staleness check, and an apply proceeds under an
    # identity its plan was never reviewed against -- the one direction the
    # comment ruled out.
    # `refresh(..., with_for_update=True)` rather than a second `select()`: same
    # re-read under the same row lock, but it does not add a `db.execute` call --
    # and every test on this route scripts `db.execute` as an ordered list, so an
    # extra one desynchronises all of them (#1565's lesson, 39 failures when this
    # was written the other way).
    await db.refresh(run, with_for_update=True)
    minted = list(run.oidc_minted_targets or [])
    added = [t["target"] for t in tokens if t["target"] not in minted]
    if added:
        # Capped, because this grows across requests and the runner may call
        # repeatedly: it retries on 5xx, and a phase asks once per phase. A run
        # cannot legitimately present more distinct identities than it may ask
        # for in one request, so MAX_TARGETS is the ceiling. Truncating rather
        # than refusing: the tokens are already signed and on their way back, so
        # failing here would hand the runner credentials the record disclaims.
        # The record is only ever read to scope the confirm-time staleness check,
        # and a short record narrows that check rather than widening it.
        combined = minted + added
        if len(combined) > MAX_TARGETS:
            logger.warning(
                "cloud identity minted-target record truncated",
                run_id=str(run.id),
                kept=MAX_TARGETS,
                dropped=len(combined) - MAX_TARGETS,
            )
            combined = combined[:MAX_TARGETS]
        run.oidc_minted_targets = combined
    await db.commit()

    logger.info(
        "minted cloud identity tokens",
        run_id=str(run.id),
        workspace=ws.name,
        phase=phase,
        targets=[t["target"] for t in tokens],
    )
    return JSONResponse(
        content={"tokens": tokens, "phase": phase, "expires_in": ttl},
    )


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

    The runner never sees this. It sends the provider configurations it
    discovered and is answered with tokens, so it has no use for the catalogue
    at all -- which is the asymmetry worth keeping: the set of audiences names
    the roles this deployment can ask to assume, and only a person composing an
    override needs to read it.

    Empty when the deployment configures no catalogue, which is the default --
    not an error, and not the same as the issuer being disabled.
    """

    # A runner token must not read this. It is deployment-wide topology -- every
    # federation target every workspace uses -- and the plan Job runs the
    # workspace's own HCL: an `external` data source, a `pre_init` hook or a
    # third-party module can read TP_AUTH_TOKEN out of the environment. The
    # discovery document deliberately publishes no audiences for exactly this
    # reason, and serving them one tier up to anything holding a runner token
    # gives the disclosure back.
    #
    # The runner has no use for it either: the mint already returns the resolved
    # audiences per target, which is all the credential phase writes.
    if user.auth_method == "runner_token":
        raise HTTPException(
            status_code=403,
            detail=(
                "A runner token cannot read the deployment's audience catalogue. "
                "The mint returns this run's own resolved audiences."
            ),
        )
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
