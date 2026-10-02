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

    # The connection's owner. Case-folded on both sides: the write path lower-cases
    # from this release, but a row written before it — or by a migration, or by hand —
    # may carry mixed case, and an owner grant that silently never matches is worse
    # than no grant at all because nothing reports it.
    if conn.owner_email and conn.owner_email.strip().lower() == actor_email.strip().lower():
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


def _canonical_repo(conn: VCSConnection | None, repo_url: str) -> str | None:
    """The ONE spelling a pattern is matched against: `owner/repo`, or None.

    **Derived from the same parser the fetch uses**, and from nothing else. That is
    the whole correctness argument here, and it has now been got wrong twice in two
    different ways, so it is worth stating as a rule: the allowlist must compare
    against *the repository the clone will actually use*, and against no other
    string.

    1. The first version matched `urlparse(url).path`, which DROPS the query string
       and fragment while `vcs_provider.parse_repo_url` splits on the first `://`
       anywhere in the string. So `myorg/safe?x=a://host/evil/evil` matched the
       pattern `myorg/safe` and cloned `evil/evil`. Fixed by deriving from
       `parse_repo_url`.
    2. The second version derived the canonical form correctly and then ALSO offered
       the raw URL as a form a pattern could match, for the convenience of an
       operator writing a pattern against a full address. Because `fnmatch`'s `*`
       crosses `/`, the ordinary pattern `myorg/*` matched the whole crafted string
       — so `myorg/safe?x=a://github.com/othercorp/private` was allowed while
       `othercorp/private` was cloned. Reproduced by execution; the allowlist was
       defeated for any pattern containing `/` and `*`, which is the spelling the
       documentation recommends.

    Both failures are the same shape: a second string, derived differently from the
    one the fetch uses, offered to the matcher. There is now exactly one, so a
    pattern cannot be satisfied by a spelling the clone will not use.

    A pattern written against a full address still works — see
    `_pattern_repo_form`, which reduces the PATTERN to the same shape instead of
    widening what the URL may match.

    `None` means "this URL does not name a repository", which callers must treat as
    refusal rather than as "no constraint".
    """
    parsed = parse_repo_url(conn, repo_url) if conn is not None else None
    if not parsed:
        return None
    owner, repo = parsed
    return f"{owner}/{repo}"


def _pattern_repo_form(pattern: str) -> str:
    """A pattern reduced to the `owner/repo` shape the canonical form has.

    An operator may reasonably write `https://github.com/platform-team/*`. Rather
    than letting the URL match in more shapes — which is what defeated the control
    — the PATTERN is brought to the URL's shape: drop the scheme, then drop the host
    segment. `https://gitlab.example.com/group/sub/*` becomes `group/sub/*`.

    Deliberately NOT `parse_repo_url`: a pattern is a glob, not a URL, and
    `platform-team/*` must survive untouched. A pattern with no `://` is already in
    the right shape and is returned as given.
    """
    if "://" not in pattern:
        return pattern
    after = pattern.split("://", 1)[1]
    host, slash, rest = after.partition("/")
    return rest if slash else after


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
    raw = list(getattr(conn, "allowed_repositories", None) or [])
    patterns = [p.strip() for p in raw if isinstance(p, str) and p.strip()]
    if not patterns:
        # An empty list means "any repository", which is what every existing
        # deployment has after the migration — the allowlist is opt-in.
        #
        # But a list that was NON-empty and left nothing after stripping is a
        # different thing: somebody intended a restriction. The API refuses that
        # shape with a 422, so reaching here means the row was written another way,
        # and the two options are to allow everything or to allow nothing. For a
        # security control the second is right — a mangled restriction should fail
        # loudly rather than silently become the widest possible setting.
        return not raw

    canonical = _canonical_repo(conn, repo_url)
    if not canonical:
        # A narrowed connection must not accept a target nobody can resolve. The
        # fetch would fail anyway, but failing here means it fails as "out of
        # scope" rather than somewhere deeper as a parse error.
        return False

    owner = canonical.split("/", 1)[0]
    for raw_pattern in patterns:
        pattern = _pattern_repo_form(raw_pattern)
        if not pattern:
            # A pattern that reduced to nothing (`https://host/`) names no
            # repository. Skipping it rather than matching everything keeps a
            # malformed entry from widening the connection.
            continue
        if fnmatch.fnmatchcase(canonical, pattern):
            return True
        # `myorg` on its own means the whole owner.
        if "/" not in pattern and fnmatch.fnmatchcase(owner, pattern):
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
