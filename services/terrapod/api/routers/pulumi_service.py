"""The Pulumi service surface — what `pulumi login <terrapod>` talks to (#1522).

Implements the subset of Pulumi's service API that the CLI actually consumes, as
catalogued in `docs/pulumi-cli-surface.md`. That document is not a reading of
Pulumi's docs: #1502 drove a real `login → stack init → preview → up → refresh →
export → import → destroy → stack rm` cycle against a request-logging stub and
recorded the 19 endpoints and 101 requests it produced. This serves what the
capture observed and nothing beyond it.

**Scope.** Only what the CLI needs, exactly as the TFE surface is scoped. Pulumi
Cloud's management surface — Deployments, Policy Packs, Insights, ESC, Webhooks,
Registry, org management — is out; a full cycle completed without touching any
of it.

**Nothing here executes anything.** The CLI runs the language host, the engine
and the providers locally. This is a state store, a secrets oracle and an event
sink; `events/batch` is the CLI pushing what it already did.

Four things the capture found that shape the code below, each of which would
otherwise have been built wrong:

1. **Auth is `token <value>`, not `Bearer` — and it changes mid-run.** Calls
   addressed at a stack carry the user's API token; the three made *during* an
   update (`checkpoint`, `events/batch`, `complete`) carry
   `Authorization: update-token <lease>` instead. Two schemes on one surface, and
   the second is invisible to any test that injects its own authenticated client.
2. **The service is the stack's secrets provider.** `encrypt` is called during an
   ordinary `up`, not only when someone writes a secret. Terrapod holds the key
   that makes stack state readable — which is why the operator can decline that
   role and keep a passphrase or KMS provider instead.
3. **A preview is an update.** It is created, then started through the same
   `POST .../update/{id}` an `up` uses, and that call **must** return a lease or
   the CLI aborts with "persisted actions require a token".
4. **Concurrency is refuse-to-start.** There is no lock endpoint: a 409 on the
   begin call ends the CLI immediately and prints the service's `message`
   verbatim. Terrapod's existing per-workspace serialisation maps straight onto
   it.

**Authorization is the workspace's (#1550).** Authenticating a caller says who
they are, not what they may do to a given stack, and this surface first shipped
doing only the former. Every route addressed at a stack now resolves the caller's
capabilities on the workspace behind it — the same resolution the Terraform routes
use — through exactly two doors:

- `_authorized_stack` for calls made with the user's token. A caller who cannot
  read the workspace gets the same 404 as for a stack that does not exist, so
  names cannot be probed; one who can read it but lacks the capability the call
  needs gets a 403 saying which.
- `_require_lease` for the three in-update calls, which carry a lease instead of
  a user. A lease authorizes one update on one stack: it is checked before the
  stack is even looked up (so an unauthenticated caller learns nothing about which
  stacks exist) and then bound to the stack in the URL.

`_find_stack` is the lookup both are built on and is never called by a route
directly; `tests/api/test_pulumi_authz.py` enforces that, and pins which
capability each route requires.

**Mounted natively, not at the root.** The CLI appends `/api/...` to whatever
base URL it is given, path prefix included, so this lives under the Terrapod
prefix. Root space is reserved for the two surfaces genuinely forced there — the
OCI registry and the terraform discovery document.

**Engine-gated (#1429).** The router is not mounted at all when the Pulumi engine
is off: absent, not present-and-404ing. A surface that refuses every request is
still in the schema, still carries its dependencies, and still reads to an
auditor as something this deployment does.
"""

from __future__ import annotations

import gzip
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for


class PulumiError(HTTPException):
    """An error in the shape the Pulumi CLI actually reads.

    Terrapod's house envelope is `{"errors": [...], "detail": ...}`. The CLI reads
    `message` and prints it verbatim — so a plain HTTPException here surfaces to
    the user as `error: [0] ` with nothing after it, which is what a real run
    produced before this existed.

    That matters most for the 409: refuse-to-start is the whole concurrency
    model, and the message is the entire explanation the operator gets.
    """


router = APIRouter(prefix="/pulumi", tags=["pulumi-service"])
logger = get_logger(__name__)


async def pulumi_user(request: Request, db: AsyncSession = Depends(get_db)) -> AuthenticatedUser:
    """Authenticate a Pulumi CLI request.

    The scheme is `Authorization: token <api-token>` — NOT `Bearer`. That is the
    capture's first finding and it is easy to document and then not implement:
    reaching for the standard dependency gives a surface that 401s every request
    the CLI makes, while every test that injects its own client passes.

    So the header is normalised and handed to the ordinary dependency, which
    keeps one place resolving API tokens, sessions and roles. Bearer is accepted
    too, because `curl` against this surface is a reasonable thing for an
    operator to do while debugging and refusing it buys nothing.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    from terrapod.api.dependencies import get_current_user as _get_current_user

    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() not in ("token", "bearer") or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pulumi requests authenticate with `Authorization: token <api-token>`",
        )
    return await _get_current_user(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=value),
        db,
    )


#: Terrapod is single-organization; the CLI still addresses one by name.
DEFAULT_ORG = "default"

#: The engine discriminator a Pulumi-backed workspace carries (#1487).
PULUMI_ENGINE = "pulumi"

#: What each kind of update requires, the same verbs a Terraform run needs. Used
#: both where an update is begun and where it is started, so the two can never
#: disagree about what, say, a destroy costs.
_KIND_CAPABILITY: dict[str, str] = {
    "preview": cap.RUN_PLAN,
    "update": cap.RUN_APPLY,
    "refresh": cap.RUN_APPLY,
    "destroy": cap.RUN_APPLY_DESTROY,
}

_STACK_NOT_FOUND = "Stack not found"


def _stack_workspace_name(project: str, stack: str) -> str:
    """The workspace name backing a stack.

    A Pulumi stack is identified by `{org}/{project}/{stack}` while a Terrapod
    workspace has one flat name, so the two halves are joined. `Workspace.name`
    is globally unique, which is what makes this addressable — and the separator
    is a character neither Pulumi projects nor stacks admit, so the mapping
    cannot collide with a name a user could otherwise choose.

    **Do not "fix" this by adding `pulumi_project`/`pulumi_stack` columns.**
    Terrapod has no projects, deliberately — the same call as single-org, for the
    same reason (see the architecture docs). Pulumi requires one, because it
    lives in `Pulumi.yaml` and the CLI will not address a stack without it, so
    the platform has to accommodate the concept without adopting it. Folding it
    into the name is that accommodation: it confines "project" to a string
    convention inside this one engine's adapter, where columns would make it a
    thing the core model knows about — letting a minority engine reintroduce
    exactly the concept the model excludes, for every workspace of every engine.

    The cost is real and is paid deliberately: the name validator has to know
    this shape (`workspace_name.validate_workspace_name`), and so does restore.
    Both are small and contained, which is the trade.
    """
    return f"{project}::{stack}"


def _split_stack_id(stack_id: str) -> tuple[str, str, str]:
    """Split the CLI's `{org}/{project}/{stack}` path segment.

    Raises 400 rather than 404 on a malformed id: the client sent something this
    API cannot address at all, which is different from asking for a stack that
    does not exist — and the CLI treats 404 as "create it".
    """
    parts = stack_id.split("/")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed stack identifier: {stack_id!r}; expected org/project/stack",
        )
    return parts[0], parts[1], parts[2]


async def _find_stack(db: AsyncSession, stack_id: str) -> Workspace:
    """The workspace backing a stack, or 404. A lookup and NOTHING more.

    It authorizes no one, so no route calls it directly: user calls go through
    `_authorized_stack` and lease calls through `_require_lease`, both of which
    call this and then decide whether the caller may proceed. A guard test fails
    if a route reaches for it on its own — which is exactly how every handler here
    once ended up authenticated but unauthorized (#1550).

    404 is load-bearing: `stack init` probes with a GET first and reads a 404 as
    "does not exist, safe to create".
    """
    _, project, stack = _split_stack_id(stack_id)
    name = _stack_workspace_name(project, stack)
    result = await db.execute(
        select(Workspace).where(Workspace.name == name, Workspace.engine == PULUMI_ENGINE)
    )
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_STACK_NOT_FOUND)
    return ws


async def _runner_caps_on(
    db: AsyncSession, user: AuthenticatedUser, ws: Workspace
) -> frozenset[str]:
    """What a run's own runner token may do on a stack.

    Agent-mode Pulumi runs call this API from the runner Job with the run's
    runner token, which carries only the `everyone` role — so ordinary
    resolution grants it nothing, and the gates below would 404 the run's own
    `pulumi preview`. That is not hypothetical: it is what a live run did the
    first time these gates existed, with every test green.

    Mirrors what the Terraform surface allows a runner
    (`tfe_v2._runner_state_read_allowed`), derived from the run rather than from
    roles:

    - On its **own** run's stack: read, state read and preview always; apply for
      an apply run; destroy for a destroy run. Never `state:write` (wholesale
      import) or `workspace:delete` — no run does either, and a runner token
      that could would be a far wider credential than the run it was minted for.
      A run writes state through checkpoints, which its update's lease governs.
    - On **another** stack: read only, and only where #344's consumer allowlist
      names the run's workspace — a StackReference, governed exactly as
      `terraform_remote_state` is.

    Fails safe: no run, a malformed id, or a run that has gone yields nothing.
    """
    if not user.run_id:
        return frozenset()
    try:
        run_uuid = uuid.UUID(user.run_id)
    except (ValueError, TypeError):
        return frozenset()
    row = (
        await db.execute(
            select(Run.workspace_id, Run.plan_only, Run.is_destroy).where(Run.id == run_uuid)
        )
    ).first()
    if row is None:
        return frozenset()
    if row.workspace_id != ws.id:
        from terrapod.api.routers.tfe_v2 import _runner_state_read_allowed

        if await _runner_state_read_allowed(db, user, ws):
            return frozenset({cap.WORKSPACE_READ, cap.STATE_READ})
        return frozenset()
    caps = {cap.WORKSPACE_READ, cap.RUN_READ, cap.STATE_READ, cap.RUN_PLAN}
    if not row.plan_only:
        caps.add(cap.RUN_APPLY)
        if row.is_destroy:
            caps.add(cap.RUN_APPLY_DESTROY)
    return frozenset(caps)


async def _caps_on(db: AsyncSession, user: AuthenticatedUser, ws: Workspace) -> frozenset[str]:
    """The caller's capabilities on a stack's workspace.

    One place for both kinds of caller: a run's runner token is authorized from
    its run (`_runner_caps_on`); everyone else through the same RBAC resolution
    the Terraform routes use. Every gate on this surface asks this, so the two
    can never be decided differently in different handlers.
    """
    if user.auth_method == "runner_token":
        return await _runner_caps_on(db, user, ws)
    return await resolve_workspace_capabilities_for(db, user, ws)


async def _authorized_stack(
    db: AsyncSession, user: AuthenticatedUser, stack_id: str, required: str
) -> Workspace:
    """The workspace backing a stack, provided the caller holds `required` on it.

    The same capability resolution the Terraform routes use, so a Pulumi
    workspace is governed exactly as a Terraform one is — owner, label RBAC,
    platform roles and the `everyone` floor all apply unchanged.

    Two refusals, deliberately different. A caller who cannot even read the
    workspace gets the same 404, with the same message, as for a stack that does
    not exist: a 403 would confirm the name to someone with no access to it, and
    the CLI already reads 404 as "no such stack". A caller who can read it but
    lacks the capability this call needs gets a 403 naming it — they can see the
    stack, so saying what they are missing gives nothing away and tells them what
    to ask for. (The Terraform surface answers 403 in both cases; this one differs
    because of what a 404 means to the Pulumi CLI.)
    """
    ws = await _find_stack(db, stack_id)
    caps = await _caps_on(db, user, ws)
    if not has_capability(caps, cap.WORKSPACE_READ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_STACK_NOT_FOUND)
    if not has_capability(caps, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires the {required} capability on stack {ws.name}",
        )
    return ws


async def read_body(request: Request) -> dict[str, Any]:
    """Parse a request body, decompressing when the CLI gzipped it.

    Checkpoint and event bodies arrive with `Content-Encoding: gzip` — the
    capture found this, and a handler that parses the raw body sees binary and
    fails on what looks like malformed JSON. Starlette does not decompress
    request bodies, so it is done here.

    Falls back to the raw bytes when the header is absent, so the same helper
    serves every endpoint rather than each one guessing.
    """
    raw = await request.body()
    if not raw:
        return {}
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:  # a truncated or mislabelled body
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Body declared Content-Encoding: gzip but could not be decompressed: {exc}",
            ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Malformed JSON body: {exc}"
        ) from exc


# ── identity ─────────────────────────────────────────────────────────────────
# `pulumi login` calls /api/user and stops if it does not answer. The org list it
# returns is what the CLI offers as the default backend organisation.


@router.get("/api/user")
async def whoami(user: AuthenticatedUser = Depends(pulumi_user)) -> dict[str, Any]:
    """`pulumi login` and `pulumi whoami`."""
    return {
        "id": user.email,
        "githubLogin": user.email,
        "name": user.display_name or user.email,
        "email": user.email,
        "organizations": [{"name": DEFAULT_ORG, "githubLogin": DEFAULT_ORG}],
        # The CLI reads this to decide whether to offer org-scoped features.
        "identities": [DEFAULT_ORG],
    }


@router.get("/api/capabilities")
async def capabilities(
    user: AuthenticatedUser = Depends(pulumi_user),
) -> dict[str, Any]:
    """Feature negotiation. An empty list is accepted and means "nothing extra".

    Declaring capabilities Terrapod does not implement is how the CLI is led into
    calling an endpoint that is not there, so this stays empty until a capability
    is actually served.
    """
    return {"capabilities": []}


@router.get("/api/user/organizations/{org}")
async def get_organization(
    org: str, user: AuthenticatedUser = Depends(pulumi_user)
) -> dict[str, Any]:
    """Org lookup. Called constantly, so it stays cheap."""
    if org != DEFAULT_ORG:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return {"name": DEFAULT_ORG, "githubLogin": DEFAULT_ORG, "defaultStackName": ""}


# ── stacks ───────────────────────────────────────────────────────────────────
# A stack is a Terrapod workspace carrying the `pulumi` engine discriminator
# (#1487), so it inherits state versioning, RBAC and run serialisation rather
# than growing a parallel set of each.


@router.get("/api/user/stacks")
async def list_stacks(
    project: str = "",
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """`pulumi stack ls` — the stacks this caller can read, not every stack.

    RBAC-filtered the same way the workspace list is (#1550): a stack the caller
    cannot read is absent, not listed-then-refused. The project filter runs first
    so capabilities are only resolved for rows that would be shown.
    """
    query = select(Workspace).where(Workspace.engine == PULUMI_ENGINE).order_by(Workspace.name)
    rows = (await db.execute(query)).scalars().all()

    stacks = []
    for ws in rows:
        proj, _, stack = ws.name.partition("::")
        if project and proj != project:
            continue
        caps = await _caps_on(db, user, ws)
        if not has_capability(caps, cap.WORKSPACE_READ):
            continue
        stacks.append(
            {
                "orgName": DEFAULT_ORG,
                "projectName": proj,
                "stackName": stack,
                "lastUpdate": int(ws.updated_at.timestamp()) if ws.updated_at else 0,
                "resourceCount": 0,
            }
        )
    return {"stacks": stacks}


@router.post("/api/stacks/{org}/{project}", status_code=status.HTTP_200_OK)
async def create_stack(
    org: str,
    project: str,
    request: Request,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """`pulumi stack init` — body `{"stackName", "tags"}`.

    Always refuses. Kept as a route so the refusal carries a message naming where
    workspaces come from; unmounting it would leave the CLI reporting a bare 404
    from its own router, which tells the operator nothing about the fix.
    """
    if org != DEFAULT_ORG:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    body = await read_body(request)
    stack = (body.get("stackName") or "").strip()
    if not stack:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="stackName is required")

    name = _stack_workspace_name(project, stack)
    existing = (
        await db.execute(select(Workspace).where(Workspace.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        # "Already exists" is only said to someone who can read it (#1550).
        # Saying it to anyone would make this an oracle for which workspace names
        # are taken — the caller could probe names they have no access to. A
        # caller who cannot read it gets the ordinary refusal below instead.
        caps = await _caps_on(db, user, existing)
        if has_capability(caps, cap.WORKSPACE_READ):
            # The CLI probes with GET first, so reaching here means a genuine
            # race or a name already taken by another engine's workspace.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Stack {project}/{stack} already exists",
            )

    # This endpoint will not create the stack (#1535).
    #
    # Terraform's CLI has never created a workspace — `init` looks one up and
    # fails if it is absent, and the operator creates it in Terrapod first. Doing
    # otherwise for Pulumi would let a CLI bring a platform resource into being
    # with no RBAC review and no record of where it came from, and would leave
    # the two engines governed differently for no reason a user could see.
    #
    # 404 rather than 403: the CLI already treats a 404 from the stack lookup as
    # "this stack is not here", so the shape is one it understands, and the
    # message carries the part it cannot infer — where stacks come from instead.
    logger.info("pulumi_stack_init_refused", stack=name, actor=user.email)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"Stack {project}/{stack} does not exist or is not visible to you, and "
            f"`pulumi stack init` cannot create it. Terrapod workspaces are created in "
            f"the UI, with the Terrapod Terraform provider, or via the API — then "
            f"select the stack with `pulumi stack select {org}/{project}/{stack}`."
        ),
    )


@router.get("/api/stacks/{org}/{project}/{stack}")
async def get_stack(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Stack lookup. A 404 here is how the CLI decides a stack needs creating."""
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.WORKSPACE_READ)
    return {
        "orgName": org,
        "projectName": project,
        "stackName": stack,
        "tags": ws.labels or {},
    }


@router.delete("/api/stacks/{org}/{project}/{stack}")
async def delete_stack(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """`pulumi stack rm`."""
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.WORKSPACE_DELETE)
    await db.delete(ws)
    await db.commit()
    logger.info("pulumi_stack_deleted", stack=ws.name, actor=user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── state ────────────────────────────────────────────────────────────────────
# State is a whole document in both directions, so the existing state-version
# storage fits with no merge model. An empty stack's deployment is `null`: a
# synthetic empty one (`{}`, or a manifest with no resources) fails the CLI's
# snapshot integrity check, which is the kind of thing only a real run finds.

#: The deployment-schema version the CLI expects alongside a deployment body.
DEPLOYMENT_VERSION = 3


async def _read_deployment(ws: Workspace, db: AsyncSession) -> dict[str, Any] | None:
    """The stack's current deployment, or None when it has never been written."""
    from terrapod.crypto.state import decrypt_state_bytes
    from terrapod.db.models import StateVersion
    from terrapod.storage import get_storage
    from terrapod.storage.keys import state_key

    sv = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sv is None:
        return None

    storage = get_storage()
    try:
        raw = await storage.get(state_key(str(ws.id), str(sv.id)))
    except Exception:
        return None
    raw = await decrypt_state_bytes(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _write_deployment(
    ws: Workspace, db: AsyncSession, deployment: dict[str, Any] | None
) -> None:
    """Persist a deployment as the stack's next state version."""
    from terrapod.crypto.state import encrypt_state_bytes
    from terrapod.db.models import StateVersion
    from terrapod.storage import get_storage
    from terrapod.storage.keys import state_key

    latest = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    serial = (latest.serial + 1) if latest else 1

    sv = StateVersion(id=uuid.uuid4(), workspace_id=ws.id, serial=serial)
    db.add(sv)
    await db.flush()

    payload = json.dumps(deployment).encode()
    storage = get_storage()
    await storage.put(state_key(str(ws.id), str(sv.id)), await encrypt_state_bytes(payload))

    # State moved underneath any plan that was already made against this
    # workspace, so those plans are now stale (#647). Every site that writes a
    # state version owes this call — a guard test enforces it, and it caught
    # this one being missed. A Pulumi checkpoint is exactly the "state moved"
    # case the hook was written for: the CLI applies locally and pushes the
    # result, the same shape as a terraform CLI apply.
    from terrapod.services.run_service import discard_stale_plans_for_state_change

    await discard_stale_plans_for_state_change(db, ws.id, serial)
    await db.commit()


@router.get("/api/stacks/{org}/{project}/{stack}/export")
async def export_stack(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """`pulumi stack export`, and how the CLI reads state before an update.

    A stack with no state answers `deployment: null` — NOT an empty object. The
    capture found the CLI's snapshot integrity check rejects a synthetic empty
    deployment, so "nothing yet" has to be expressed as null rather than as an
    empty shape that looks tidier.
    """
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.STATE_READ)
    return {"version": DEPLOYMENT_VERSION, "deployment": await _read_deployment(ws, db)}


@router.post("/api/stacks/{org}/{project}/{stack}/import")
async def import_stack(
    org: str,
    project: str,
    stack: str,
    request: Request,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """`pulumi stack import` — writes state wholesale. Body may be gzipped."""
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.STATE_WRITE)
    body = await read_body(request)
    await _write_deployment(ws, db, body.get("deployment"))
    logger.info("pulumi_state_imported", stack=ws.name, actor=user.email)
    # `stack import` is asynchronous: the CLI takes this id and polls
    # GET .../update/{id} until it reports a terminal status. The id must be
    # non-empty — an empty one makes the poll URL `.../update/`, which matches no
    # route and answers 405, and the CLI reports "waiting for import: [405]".
    #
    # The write above already happened synchronously, so any id works: the poll
    # finds no record and reads that as succeeded, which is the honest answer.
    return {"updateID": str(uuid.uuid4())}


# ── secrets ──────────────────────────────────────────────────────────────────
# The capture's most consequential finding: in `httpstate` mode the CLI delegates
# secret encryption to the backend and calls `encrypt` during an ordinary `up` —
# six times in the captured run, with no secret config set. So this is not an
# optional convenience; standing up the surface means becoming the thing that
# holds the key making stack state readable.
#
# An operator who would rather Terrapod did not hold that key keeps a passphrase
# or KMS provider instead (`pulumi stack init --secrets-provider=...`), in which
# case the CLI encrypts locally and never calls these.
#
# All three require `state:read` (#1550). Decrypt returns secret values, which is
# exactly what reading raw state would reveal, so it costs the same. Encrypt
# reveals nothing, but it is called during `up` and `preview`, and `state:read`
# is held by every preset that can run either — so asking for it refuses nobody
# who needs it while keeping the oracle closed to someone with no grant at all.


@router.post("/api/stacks/{org}/{project}/{stack}/encrypt")
async def encrypt_secret(
    org: str,
    project: str,
    stack: str,
    request: Request,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Encrypt one value for a stack — body `{"plaintext": "<base64>"}`.

    Rides Terrapod's existing envelope encryption rather than introducing a
    second scheme: the same DEK, the same rotation story, the same at-rest
    guarantees the rest of the platform already has.
    """
    import base64

    from terrapod.crypto.service import get_encryption

    await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.STATE_READ)
    body = await read_body(request)
    plaintext = body.get("plaintext")
    if plaintext is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="plaintext is required")

    # The CLI sends and expects base64 on this surface.
    raw = base64.b64decode(plaintext)
    sealed = get_encryption().encrypt(raw.decode("utf-8", errors="surrogateescape"))
    return {"ciphertext": base64.b64encode(sealed.encode()).decode()}


@router.post("/api/stacks/{org}/{project}/{stack}/decrypt")
async def decrypt_secret(
    org: str,
    project: str,
    stack: str,
    request: Request,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Decrypt one value.

    Not exercised by the capture — the captured program had no secret *config* to
    read back — so the shape here follows the CLI's own expectations and is
    covered by the round-trip test rather than by observed traffic.
    """
    import base64

    from terrapod.crypto.service import get_encryption

    await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.STATE_READ)
    body = await read_body(request)
    ciphertext = body.get("ciphertext")
    if ciphertext is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="ciphertext is required"
        )
    sealed = base64.b64decode(ciphertext).decode()
    plaintext = get_encryption().decrypt(sealed)
    return {"plaintext": base64.b64encode(plaintext.encode()).decode()}


@router.post("/api/stacks/{org}/{project}/{stack}/batch-decrypt")
async def batch_decrypt(
    org: str,
    project: str,
    stack: str,
    request: Request,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Decrypt many at once — the shape `pulumi config` uses on a stack with
    several secrets, so it is one round trip rather than N."""
    import base64

    from terrapod.crypto.service import get_encryption

    await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.STATE_READ)
    body = await read_body(request)
    svc = get_encryption()
    out: dict[str, str] = {}
    for ciphertext in body.get("ciphertexts") or []:
        sealed = base64.b64decode(ciphertext).decode()
        out[ciphertext] = base64.b64encode(svc.decrypt(sealed).encode()).decode()
    return {"plaintexts": out}


# ── the update lifecycle ─────────────────────────────────────────────────────
# An update has an explicit begin and end. `preview`, `update`, `refresh` and
# `destroy` all CREATE one; it is then STARTED through the same
# `POST .../update/{id}`, which must hand back a lease token — without one the
# CLI aborts with "persisted actions require a token".
#
# Leases live in Redis with a TTL rather than in a table, because expiry is the
# point: a run that dies mid-update leaves a started-but-never-completed update,
# and the lease timing out is what releases the stack. A row would need a sweeper
# to do what a TTL does for free.

#: How long a lease is good for before the update is considered abandoned.
LEASE_TTL_SECONDS = 30 * 60


#: Redis key holding one update's record.
def _update_key(update_id: str) -> str:
    return f"tp:pulumi:update:{update_id}"


#: Redis key marking the stack as having an update in flight. Its presence is
#: what makes a second begin a 409, and its TTL is what stops a dead run holding
#: the stack forever.
def _stack_lock_key(workspace_id: str) -> str:
    return f"tp:pulumi:stack_active:{workspace_id}"


def _decode_record(raw: dict | None) -> dict[str, str]:
    """A Redis hash as plain strings, whichever way the client returned it."""
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in (raw or {}).items()
    }


async def _begin_update(ws: Workspace, kind: str, user: AuthenticatedUser) -> dict[str, Any]:
    """Create an update, refusing if one is already in flight.

    Concurrency on this surface is refuse-to-start: there is no lock endpoint,
    and a 409 here ends the CLI immediately, printing `message` verbatim. So the
    message is the whole of the user's explanation — it is worth writing for a
    person rather than a log.
    """
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    update_id = str(uuid.uuid4())

    # SET NX is the whole serialisation: the first begin wins, the rest are told
    # why. Same pattern the scheduler uses for its periodic-task mutex.
    acquired = await redis.set(
        _stack_lock_key(str(ws.id)), update_id, nx=True, ex=LEASE_TTL_SECONDS
    )
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another update is currently in progress",
        )

    await redis.hset(
        _update_key(update_id),
        mapping={
            "workspace_id": str(ws.id),
            "kind": kind,
            "status": "not-started",
            "actor": user.email,
        },
    )
    await redis.expire(_update_key(update_id), LEASE_TTL_SECONDS)
    logger.info("pulumi_update_begun", stack=ws.name, kind=kind, update_id=update_id)
    return {"updateID": update_id}


async def _require_lease(
    request: Request, update_id: str, db: AsyncSession, stack_id: str
) -> tuple[dict[str, str], Workspace]:
    """Authenticate an in-update call by its lease, bound to the stack it names.

    The second auth scheme, and the reason it needs its own dependency: these
    calls are made with `Authorization: update-token <lease>` rather than the
    user's API token. A test that injects an authenticated client never exercises
    this path, so the scheme would look fine and be wrong.

    Two properties matter as much as the token check itself (#1550):

    - **Order.** The lease is checked BEFORE the stack is looked up. These calls
      carry no user, so answering 404 for a missing stack but 401 for a bad lease
      on an existing one would let anyone, unauthenticated, test which stacks
      exist. With the lease first, every bad-lease request gets the same 401.
    - **Binding.** A lease authorizes one update on one stack. It is refused when
      the stack in the URL is not the one the update was begun on, so a lease
      obtained for a stack the caller may write cannot be pointed at one they may
      not. The lookup and the comparison live here, in one function, so no route
      can do the first without the second.
    """
    from terrapod.redis.client import get_redis_client

    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "update-token" or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This endpoint requires an update-token lease",
        )

    record = _decode_record(await get_redis_client().hgetall(_update_key(update_id)))
    if not record:
        # Expired or never existed — the same answer either way, because a lease
        # that has timed out is exactly as invalid as one that was invented.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown or expired update"
        )
    if record.get("lease") != value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid lease token")

    ws = await _find_stack(db, stack_id)
    if record.get("workspace_id") != str(ws.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This lease was issued for a different stack",
        )
    return record, ws


@router.post("/api/stacks/{org}/{project}/{stack}/preview")
async def begin_preview(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A preview IS an update — same creation, same start path, same lease."""
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", _KIND_CAPABILITY["preview"])
    return await _begin_update(ws, "preview", user)


@router.post("/api/stacks/{org}/{project}/{stack}/update")
async def begin_up(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", _KIND_CAPABILITY["update"])
    return await _begin_update(ws, "update", user)


@router.post("/api/stacks/{org}/{project}/{stack}/refresh")
async def begin_refresh(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", _KIND_CAPABILITY["refresh"])
    return await _begin_update(ws, "refresh", user)


@router.post("/api/stacks/{org}/{project}/{stack}/destroy")
async def begin_destroy(
    org: str,
    project: str,
    stack: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", _KIND_CAPABILITY["destroy"])
    return await _begin_update(ws, "destroy", user)


@router.post("/api/stacks/{org}/{project}/{stack}/update/{update_id}")
async def start_update(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Start a created update, and hand back the lease.

    The token is not optional decoration: without it the CLI aborts with
    `fatal: An assertion has failed: persisted actions require a token` before
    doing any work. That failure mode is why this is asserted in its own test.

    Starting costs what beginning cost — the capability the update's kind
    requires, looked up from the record rather than trusted from the URL — and
    the update must belong to the stack addressed (#1550). Otherwise the lease,
    which is what authorizes everything after this, could be minted for a stack
    other than the one the update was begun on.
    """
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    record = _decode_record(await redis.hgetall(_update_key(update_id)))
    required = _KIND_CAPABILITY.get(record.get("kind", "")) if record else None
    if required is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown update")
    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", required)
    if record.get("workspace_id") != str(ws.id):
        # Same answer as an update that does not exist: from this stack's point
        # of view, it doesn't.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown update")

    lease = str(uuid.uuid4())
    await redis.hset(_update_key(update_id), mapping={"lease": lease, "status": "running"})
    await redis.expire(_update_key(update_id), LEASE_TTL_SECONDS)
    return {"token": lease}


@router.get("/api/stacks/{org}/{project}/{stack}/update/{update_id}")
async def get_update_status(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    user: AuthenticatedUser = Depends(pulumi_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll an update. `stack import` waits on this."""
    from terrapod.redis.client import get_redis_client

    ws = await _authorized_stack(db, user, f"{org}/{project}/{stack}", cap.RUN_READ)
    record = _decode_record(await get_redis_client().hgetall(_update_key(update_id)))
    if not record:
        # A completed update's record is gone, and the CLI reads "succeeded" as
        # done rather than erroring — which is the right answer for anything it
        # is still polling after the fact.
        return {"status": "succeeded"}
    if record.get("workspace_id") != str(ws.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown update")
    return {"status": record.get("status") or "running"}


@router.patch("/api/stacks/{org}/{project}/{stack}/update/{update_id}/checkpoint")
async def write_checkpoint(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Write state mid-update. Lease-authenticated, and gzipped.

    A preview's lease may not write state (#1550). A preview never persists a
    checkpoint — on a live stack, previews leave no state version behind and
    every one that exists was written by an update — so refusing costs nothing.
    Accepting would let `run:plan`, the capability that begins a preview, buy a
    `state:write` its holder was never granted.
    """
    record, ws = await _require_lease(request, update_id, db, f"{org}/{project}/{stack}")
    if record.get("kind") == "preview":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A preview does not write state; this lease cannot checkpoint",
        )
    body = await read_body(request)
    await _write_deployment(ws, db, body.get("deployment"))
    return {}


@router.post("/api/stacks/{org}/{project}/{stack}/update/{update_id}/events/batch")
async def post_events(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Engine events. Lease-authenticated, gzipped, and accepted-and-dropped.

    The CLI pushes what it already did; nothing downstream consumes these yet, so
    they are acknowledged rather than stored. Refusing them would fail the run
    for no gain, and storing them without a reader would be storage nobody asked
    for — worth revisiting when there is a run view to feed.
    """
    await _require_lease(request, update_id, db, f"{org}/{project}/{stack}")
    await read_body(request)
    return {}


@router.post("/api/stacks/{org}/{project}/{stack}/update/{update_id}/complete")
async def complete_update(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """End the update and release the stack."""
    from terrapod.redis.client import get_redis_client

    record, ws = await _require_lease(request, update_id, db, f"{org}/{project}/{stack}")
    body = await read_body(request)

    redis = get_redis_client()
    await redis.delete(_update_key(update_id))
    # Release only if this update still holds it: a lease that expired may have
    # been replaced by a newer update, and deleting that one's lock would let a
    # third start alongside it.
    held = await redis.get(_stack_lock_key(str(ws.id)))
    if held and (held.decode() if isinstance(held, bytes) else held) == update_id:
        await redis.delete(_stack_lock_key(str(ws.id)))

    logger.info(
        "pulumi_update_completed",
        stack=ws.name,
        update_id=update_id,
        status=body.get("status"),
        kind=record.get("kind"),
    )
    return {}


@router.post("/api/stacks/{org}/{project}/{stack}/update/{update_id}/renew_lease")
async def renew_lease(
    org: str,
    project: str,
    stack: str,
    update_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Extend a lease. Not exercised by the capture — the runs were too short to
    need it — so the shape follows the CLI's expectations and is covered by test
    rather than by observed traffic."""
    from terrapod.redis.client import get_redis_client

    await _require_lease(request, update_id, db, f"{org}/{project}/{stack}")
    redis = get_redis_client()
    await redis.expire(_update_key(update_id), LEASE_TTL_SECONDS)
    return {}
