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

    Pass **`dependencies.label_reach_roles(user)`**, not `user.roles` and not
    `effective_platform_roles(user)`. Each of those escapes in one direction: the live
    set includes roles a `service_bound` token was deliberately not pinned to, and the
    derived platform set is pinned-only for a `service_detached` token, so it includes
    roles the principal no longer holds. The helper is the intersection, which escapes
    in neither, and it drops `admin` because `check_access` short-circuits on it and
    would re-grant through the label path the admin that `is_platform_admin` has
    already decided against on the attenuated view.
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

    # The connection's owner, case-folded on BOTH sides — and this fold is now the only
    # thing that makes an owner grant work for any row, not a concession to legacy
    # ones. The write path used to lower-case too; that was removed, because a server
    # that alters a value a Terraform provider sent makes the resource unmanageable
    # (see `_rbac_attrs`). So the tolerance lives here, on the read side, where it
    # costs nothing — do not "tidy" it away, and do not restore the write-side fold.
    if conn.owner_email and conn.owner_email.strip().lower() == actor_email.strip().lower():
        return True

    # Label RBAC, the same allow/deny evaluation every labelled resource gets —
    # with two deliberate narrowings, because a VCS connection is not like the
    # other labelled resources: reaching one grants read on every repository its
    # credential can reach, so the usual conveniences are too blunt here.
    if actor_roles:
        from terrapod.services.rbac_service import check_access

        # Defence in depth over `dependencies.label_reach_roles`, which callers use to
        # narrow this set. `check_access` short-circuits to True on `admin`, so an
        # `admin` arriving here — from a caller that forgot to narrow, or a future one
        # — re-grants through the label path the admin that `is_platform_admin` above
        # has already decided against on the attenuated view. Dropping it costs a
        # genuine admin nothing, because that gate returned True long before here.
        actor_roles = [r for r in actor_roles if r != "admin"]

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
        if actor_roles and labels and await check_access(db, actor_email, "", labels, actor_roles):
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


class RepositoryNotAllowed(PermissionError):
    """A fetch was attempted against a repository outside the connection's allowlist.

    `PermissionError` so that a caller reading the TYPE can tell a refusal from a
    transport failure — `RepositoryNotAllowed` is unambiguous where a bare `Exception`
    would not be.

    **It does NOT escape an `except OSError`.** `PermissionError` subclasses `OSError`,
    so a handler written for the network catches this too; an earlier version of this
    docstring claimed the opposite, which is the inverse of Python's own hierarchy and
    would have had someone "fix" the base class on the strength of it. What contains a
    refusal in practice is the poller's `except Exception`, which logs it per workspace
    and moves on — that is asserted in
    `TestARefusalAtTheCloneCostsOneWorkspaceNotTheCycle`.
    """


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
    patterns, decided = _patterns_or_verdict(conn)
    if decided is not None:
        return decided

    canonical = _canonical_repo(conn, repo_url)
    if not canonical:
        # A narrowed connection must not accept a target nobody can resolve. The
        # fetch would fail anyway, but failing here means it fails as "out of
        # scope" rather than somewhere deeper as a parse error.
        return False
    return _matches_any(canonical, patterns)


def repository_pair_allowed(conn: VCSConnection | None, owner: str, repo: str) -> bool:
    """`repository_allowed` for callers that already hold the owner and repo.

    The fetch functions — the provider `download_archive` dispatcher and the archive
    cache — are handed `(conn, owner, repo, ref)` rather than a URL, so they have the
    canonical form already and need no parsing. That is strictly safer: the two
    historical breaks of this control were both "a second string, derived differently
    from the one the fetch uses, offered to the matcher", and here there is no second
    string to derive.
    """
    if conn is None:
        return False
    patterns, decided = _patterns_or_verdict(conn)
    if decided is not None:
        return decided
    if not owner or not repo:
        return False
    return _matches_any(f"{owner}/{repo}", patterns)


def _patterns_or_verdict(conn: VCSConnection) -> tuple[list[str], bool | None]:
    """The usable patterns, or a verdict when there is nothing to match against.

    An empty list means "any repository", which is what every existing deployment has
    after the migration — the allowlist is opt-in.

    But a list that was NON-empty and left nothing after stripping is a different
    thing: somebody intended a restriction. The API refuses that shape with a 422, so
    reaching here means the row was written another way, and the two options are to
    allow everything or to allow nothing. For a security control the second is right —
    a mangled restriction should fail loudly rather than silently become the widest
    possible setting.
    """
    raw = list(getattr(conn, "allowed_repositories", None) or [])
    patterns = [p.strip() for p in raw if isinstance(p, str) and p.strip()]
    if not patterns:
        return [], (not raw)
    return patterns, None


def _matches_any(canonical: str, patterns: list[str]) -> bool:
    """The ONE matcher. Every entry point reduces to this, by construction."""
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


def _url_host(url: str) -> str:
    """The lowercased host of a URL or bare `host[/path]`, or "" if it has none."""
    from urllib.parse import urlsplit

    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower()


def connection_git_host(conn: VCSConnection | None) -> str:
    """The ONE host a credential minted from this connection may be installed at.

    For GitLab `server_url` is the instance, so it is the git host directly. For
    GitHub it is the **API** base — `https://api.github.com` by default, or
    `https://ghe.example.com/api/v3` for an Enterprise install — so the default has to
    be mapped to `github.com` while a GHE host passes through unchanged.
    """
    if conn is None:
        return ""
    from terrapod.services import github_service, gitlab_service

    raw = (getattr(conn, "server_url", "") or "").strip()
    if getattr(conn, "provider", "") == "gitlab":
        return _url_host(raw or gitlab_service.DEFAULT_GITLAB_URL)
    host = _url_host(raw or github_service.DEFAULT_GITHUB_API_URL)
    # `api.github.com` serves the API; repositories live on `github.com`. Every other
    # value is a GHE host and is already the git host.
    return "github.com" if host == "api.github.com" else host


def credential_scope_host_allowed(conn: VCSConnection | None, scope: str) -> bool:
    """Whether a credential at `scope` would be installed at THIS connection's host.

    **This is a separate and stronger invariant than the repository allowlist, and it
    is enforced whether or not an allowlist is set.** The allowlist is opt-in and
    answers "which repositories"; this answers "whose server", and there is no
    legitimate configuration in which a token minted from one provider account should
    be handed to a different host.

    Without it the allowlist could be satisfied while the credential went elsewhere
    entirely. `_scope_repo_form` discards the scope's first segment as the host and
    nothing compared it to anything, so a connection restricted to `myorg/*` accepted
    the key `evil.tld/myorg`: the runner then wrote
    `[credential "https://evil.tld/myorg"]` with the connection's GitHub App
    installation token, and a module source of `git::https://evil.tld/myorg/x.git` in
    the workspace's own configuration sent that token — `contents: read` across the
    whole installation — to a host the attacker chose. Reachable by anyone who can
    write a workspace variable on a workspace the connection is already attached to,
    because the mint path deliberately skips `may_reference_connection` for the
    workspace's own connection.
    """
    if conn is None:
        return False
    expected = connection_git_host(conn)
    if not expected:
        # Cannot establish the connection's own host, so cannot establish that the
        # scope matches it. Refusing is the only safe answer.
        return False
    return _url_host(scope) == expected


def credential_scope_host_refusal_detail(conn_id: uuid.UUID, scope: str, expected: str) -> str:
    """Named separately because the remedy is not "widen the allowlist"."""
    return (
        f"git credential scope {scope!r} names host {_url_host(scope) or '<none>'!r}, "
        f"but VCS connection vcs-{conn_id} serves {expected!r}. A credential minted "
        "from a connection is only ever installed for that connection's own host — "
        "otherwise its token would be sent to a server the connection has nothing to "
        "do with. Correct the variable's key to use "
        f"{expected!r}, or use a `static` credential holding a token you have scoped "
        "yourself if you genuinely need to authenticate to another host."
    )


def _scope_repo_form(scope: str) -> str:
    """A git credential scope reduced to the `owner/repo` shape patterns are in.

    NOT `_pattern_repo_form`, which only strips a host when a scheme is present. A
    credential scope is always `host[/path]` — the runner writes it as
    `[credential "https://<scope>"]` — so the first segment is the host whether or
    not the operator wrote a scheme, and `github.com` must reduce to the empty
    string, meaning host-wide. Using the pattern reduction here left the host in and
    nothing matched, so every narrowed connection refused every minted credential.
    """
    s = scope.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    _host, slash, rest = s.partition("/")
    return rest.strip("/") if slash else ""


def credential_scope_allowed(conn: VCSConnection | None, scope: str) -> bool:
    """Is every repository a credential installed at `scope` could reach allowed?

    A different question from `repository_allowed`, which asks about ONE repository.
    A minted `git_http_auth` credential is installed by the runner as a git
    `[credential "https://<scope>"]` section, and `<scope>` comes from the variable's
    **key**, which the workspace owner chooses. git matches such a section by host
    **and path prefix**, so `key = github.com` installs the token for the entire host
    and the workspace's own configuration can then clone anything the credential
    reaches. The subject of the allowlist check here is therefore the key, not the
    workspace's own repository URL — the workspace's URL is already checked at create,
    at PATCH and at the config fetch, and checking it a fourth time here bounds
    nothing new.

    Containment here is a **conservative approximation**, not a decision. An earlier
    version of this docstring claimed it was decidable because the scope is a path
    prefix rather than a glob; two samples cannot decide a predicate that discriminates
    on content, and `fnmatch` supports character classes. `prod/[!x]*` accepted the
    scope `prod` while refusing `prod/x-secret`, so the credential reached a repository
    the allowlist did not allow. A pattern containing `[` or `]` is therefore refused
    outright — the same treatment `?`, `#` and `..` get in a scope — which removes the
    class of pattern the approximation cannot reason about rather than pretending to
    handle it.

    Within what remains, two probes settle it: a pattern must either match the
    scope itself — `myorg` against the pattern `myorg`, or `myorg/repo` against
    `myorg/repo`, both of which name no more than the pattern does — or match
    everything one and two segments beneath it, which is what a prefix reaches. Two
    depths rather than one because a GitLab canonical form keeps the nested group
    path, so `group/sub/proj` is three segments.

    This refuses more than a perfect decision procedure would; that is the direction
    to err in, and the refusal says how to narrow the key.
    """
    if conn is None:
        return False

    # The host check runs BEFORE the allowlist's opt-in short-circuit, deliberately:
    # it is a different invariant, and an empty allowlist must not mean "this
    # connection's token may be installed for any host in the world".
    if not credential_scope_host_allowed(conn, scope):
        logger.warning(
            "a git credential scope names a host this connection does not serve; refusing",
            scope=scope,
            expected_host=connection_git_host(conn),
        )
        return False

    patterns, decided = _patterns_or_verdict(conn)
    if decided is not None:
        return decided

    key = _scope_repo_form(scope or "")

    # A character class makes the two-probe containment argument unsound (see the
    # docstring), so a pattern carrying one cannot bound a credential. Refusing is the
    # conservative direction: the operator is told, rather than silently given a
    # credential wider than the pattern they wrote.
    classy = [p for p in patterns if "[" in p or "]" in p]
    if classy:
        logger.warning(
            "an allowlist pattern uses a character class, which cannot bound a "
            "credential scope; refusing",
            patterns=classy,
        )
        return False

    # A credential scope carrying a query, a fragment or a path traversal is a
    # misconfiguration, and refused outright rather than reasoned about. Each of these
    # already fails the containment test for any sensible pattern — `myorg?x=1` does not
    # match `myorg/*` — but only incidentally, and "incidentally refused" is not a
    # property worth relying on in a security check. `..` in particular invites an
    # argument about what git normalises, which is an argument not worth having when
    # the answer is that no legitimate key contains one.
    if any(c in key for c in "?#") or ".." in key.split("/"):
        logger.warning(
            "a git credential scope carries a query, fragment or traversal; refusing",
            scope=scope,
        )
        return False

    for raw_pattern in patterns:
        pattern = _pattern_repo_form(raw_pattern)
        if not pattern:
            continue
        if fnmatch.fnmatchcase(key, pattern):
            return True
        # What a prefix actually reaches. `\x00` cannot occur in a repository name,
        # so a pattern matching these probes matches on its wildcards rather than by
        # coincidence.
        deeper = f"{key}/\x00" if key else "\x00"
        deeper2 = f"{deeper}/\x00"
        if fnmatch.fnmatchcase(deeper, pattern) and fnmatch.fnmatchcase(deeper2, pattern):
            return True
    return False


def credential_scope_refusal_detail(conn_id: uuid.UUID, scope: str, patterns: list) -> str:
    """Distinct from `repository_refusal_detail`: the subject is the key, not a repo.

    An operator told "this repository is not allowed" would go looking at the
    workspace's VCS settings, which are not what refused them.
    """
    shown = ", ".join(repr(p) for p in patterns[:5] if isinstance(p, str))
    more = "" if len(patterns) <= 5 else f" (and {len(patterns) - 5} more)"
    return (
        f"VCS connection vcs-{conn_id} is restricted to specific repositories, and a "
        f"credential installed for {scope!r} would reach more than those. The "
        "credential's scope is the variable's key, and git applies it by host and "
        f"path prefix, so {scope!r} covers everything beneath it. Allowed: "
        f"{shown}{more}. Narrow the key to a repository or an owner inside the "
        "allowlist, widen `allowed-repositories` on the connection, or use a "
        "`static` credential holding a token you have scoped yourself."
    )


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
