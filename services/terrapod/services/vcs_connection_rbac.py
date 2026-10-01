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

**Four ways to hold a claim, any one of which is enough.** A platform admin may
name any connection. The connection's `owner_email` may name it. A principal whose
roles reach the connection's `labels` may name it — the same allow/deny evaluation
every other labelled resource gets, including the `access: everyone` floor. And,
kept from v1.8.2, anyone who already owns a workspace using the connection may name
it again, because that grant is one they already hold.

That last one existed because `VCSConnection` carried neither `labels` nor
`owner_email`, so there was no dimension to match on without a schema change — it
was the version that fitted a patch, and its deliberate consequence was that the
**first** workspace on a connection had to be created by an admin. The owner and
label columns remove that consequence: a connection can now be delegated to a team
up front. The workspace-ownership path stays because removing it would break
deployments that upgraded on it.

**The repository allowlist is the residual hole**, and it is separate from all of
the above. Even a fully entitled caller could point the connection at ANY repository
its credential can read, because the gate authorises the *connection* and the repo
URL is just a string on the workspace. `allowed_repositories` is empty by default,
which keeps exactly today's behaviour, and when non-empty restricts the connection
to those patterns. It is checked wherever a repository URL is accepted, not only at
the one obvious site: the refs endpoint is a private-repository oracle at
workspace-read, and the config fetch is where the source actually arrives.
"""

from __future__ import annotations

import fnmatch
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import VCSConnection, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.vcs_provider import parse_repo_url

logger = get_logger(__name__)


async def may_reference_connection(
    db: AsyncSession,
    *,
    conn_id: uuid.UUID,
    actor_email: str,
    is_platform_admin: bool,
    actor_roles: list[str] | None = None,
) -> bool:
    """True if `actor_email` may point a workspace at `conn_id`.

    `is_platform_admin` is passed in rather than resolved here so the one caller
    that has no live principal — the run-time credential mint, which authorises
    against the workspace's owner — cannot accidentally grant itself admin.

    `actor_roles` is optional for the same reason: a caller with no session has no
    roles to evaluate, and omitting it must mean "no label claim", never "all
    labels". A missing argument that widened access would be the whole finding
    again, in the fix for it.

    Pass **`user.roles`**, not `effective_platform_roles(user)`. That derived set is
    documented as being for platform gates only and never a substitute here — for a
    `service_detached` token it is the PINNED roles, so using it would let a
    detached token keep label reach to a connection through a role its user no
    longer holds. This is a per-resource decision and wants the un-attenuated set.
    """
    if not settings.vcs.require_connection_authorization:
        return True
    if is_platform_admin:
        return True
    if not actor_email:
        return False

    # `db.get`, not a select: this is a primary-key load, so it goes through the
    # identity map and often costs no round trip at all — the callers that reach
    # here have usually just loaded the connection for something else.
    conn = await db.get(VCSConnection, conn_id)
    if conn is None:
        # A connection that does not exist is not a claim. The caller's own
        # existence check reports it; this must not report True.
        return False

    # The connection's owner.
    if conn.owner_email and conn.owner_email == actor_email:
        return True

    # Label RBAC, the same allow/deny evaluation every labelled resource gets —
    # with two deliberate narrowings, because a VCS connection is not like the
    # other labelled resources: reaching one grants read on every repository its
    # credential can reach, so the usual conveniences are too blunt here.
    if actor_roles:
        from terrapod.services.rbac_service import check_access

        labels = dict(conn.labels or {})

        # 1. The `access: everyone` floor is NOT honoured. `check_access` seeds it
        #    unconditionally, so a connection carrying that label — a habitual one —
        #    would be nameable by every authenticated principal, which is a one-label
        #    revert of this entire finding. Delegate a connection with a role's
        #    `allow_labels` or with `owner_email`, both of which name someone.
        labels.pop("access", None)

        # 2. No name-based matching. `allow_names` is a flat namespace shared across
        #    every resource type, so a role written as `allow_names: ["prod-net"]`
        #    for a workspace would otherwise also authorise the CONNECTION called
        #    `prod-net`. Passing an empty name leaves label matching as the only
        #    path, which is the one an operator writing connection delegation means.
        if labels and await check_access(db, actor_email, "", labels, actor_roles):
            return True

    # Kept from v1.8.2: already owns a workspace using it, so the grant is one they
    # already hold and naming it again gains them nothing.
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


def _repo_forms(conn: VCSConnection | None, repo_url: str) -> list[str] | None:
    """The spellings a pattern may legitimately be written against, or None.

    **Derived from the same parser the fetch uses**, not from a second URL parse.
    That is the whole correctness argument here, and the first version of this
    function got it wrong in a way that voided the allowlist entirely: it matched on
    `urlparse(url).path`, which DROPS the query string and the fragment, while
    `vcs_provider.parse_repo_url` splits on the first `://` anywhere in the string.
    So `myorg/safe?x=a://host/evilorg/evil` matched the pattern `myorg/safe` and
    cloned `evilorg/evil`. Two parsers disagreeing about what the repository is, is
    the same failure as two matchers disagreeing about who receives a credential.

    `None` means "this URL does not name a repository", which callers must treat as
    refusal rather than as "no constraint".
    """
    parsed = parse_repo_url(conn, repo_url) if conn is not None else None
    if not parsed:
        return None
    owner, repo = parsed
    canonical = f"{owner}/{repo}"
    forms = [canonical]
    # Operators write `myorg/*`, so the canonical form is the one that matters. The
    # raw URL is offered as well for a pattern written against a full address, but
    # ONLY when it parsed — an unparseable URL has no forms at all.
    raw = (repo_url or "").strip()
    if raw and raw != canonical:
        forms.append(raw)
    return forms


def repository_allowed(conn: VCSConnection | None, repo_url: str) -> bool:
    """Whether this connection may be pointed at this repository.

    Empty list means any, which is what every existing deployment has after the
    migration — the allowlist is opt-in, so upgrading changes nothing until an
    operator narrows it.

    A pattern containing no `/` is matched against the owner alone as well as the
    `owner/repo` form, so `myorg` and `myorg/*` both mean what an operator expects.

    **`fnmatch`'s `*` crosses `/`, and the canonical form is NOT always two
    segments.** GitHub's parser returns exactly owner and repo, but GitLab's keeps
    the nested group path — `https://gitlab.com/group/sub/proj` parses as
    `('group/sub', 'proj')`, so the canonical form is `group/sub/proj`. On GitLab
    that makes `group/*` match everything at any depth under `group`, which is what
    an operator almost certainly wants from a group-wide pattern but is wider than
    the pattern reads. Someone who means only the group's direct projects should
    write the projects out, or a pattern per subgroup. Stated here rather than
    silently relied on, because the earlier version of this function claimed the
    opposite and was wrong.
    """
    if conn is None:
        return False
    patterns = [
        p.strip()
        for p in (getattr(conn, "allowed_repositories", None) or [])
        if isinstance(p, str) and p.strip()
    ]
    if not patterns:
        return True

    forms = _repo_forms(conn, repo_url)
    if not forms:
        # A narrowed connection must not accept a target nobody can resolve. The
        # fetch would fail anyway, but failing here means it fails as "out of
        # scope" rather than somewhere deeper as a parse error.
        return False

    owner = forms[0].split("/", 1)[0]
    for pattern in patterns:
        for form in forms:
            if fnmatch.fnmatch(form, pattern):
                return True
        # `myorg` on its own means the whole owner.
        if "/" not in pattern and fnmatch.fnmatch(owner, pattern):
            return True
    return False


def refusal_detail(conn_id: uuid.UUID) -> str:
    """What the caller is told. Names the id, the reason and the way out."""
    return (
        f"Not authorized to use VCS connection vcs-{conn_id}. A VCS connection "
        "covers every repository its credential can reach, so naming one grants "
        "that access. You may name a connection you own, one your roles reach by "
        "label, or one you already own a workspace on; a platform admin can set "
        "the connection's owner or labels, or set "
        "`api.config.vcs.require_connection_authorization: false` to restore the "
        "previous behaviour of accepting any connection id."
    )


def repository_refusal_detail(conn_id: uuid.UUID, repo_url: str, patterns: list) -> str:
    """Separate from `refusal_detail` on purpose.

    "You may not use this connection" and "this connection may not be pointed
    there" are different problems with different remedies, and collapsing them
    sends the operator to ask for the wrong thing.
    """
    shown = ", ".join(repr(p) for p in patterns[:5] if isinstance(p, str))
    more = "" if len(patterns) <= 5 else f" (and {len(patterns) - 5} more)"
    return (
        f"VCS connection vcs-{conn_id} is restricted to specific repositories and "
        f"{repo_url!r} is not one of them. Allowed: {shown}{more}. A platform admin "
        "can widen `allowed-repositories` on the connection, or clear it to allow "
        "any repository the credential can reach."
    )
