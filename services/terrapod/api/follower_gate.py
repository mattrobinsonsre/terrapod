"""Refuse mutating requests on a node that does not hold the shared name (#1130).

`ensure_leader` guards the run lifecycle at the service layer, and it has to:
the scheduler and the triggered-task consumer write without an HTTP request
anywhere in sight, so no middleware can see them. But the *management* surface —
workspaces, variables, roles, tokens, pools, VCS connections, policy sets,
registry entries, hooks, catalog items — is reached only over HTTP, and adding
a `ensure_leader` call per router is the shape that rots: it closes today's gap
and reopens it the moment someone adds a router.

So this is **default-deny at one chokepoint**. Every mutating method is refused
on a follower unless its path is on the allow-list below. A router added in six
months is covered without anyone remembering to cover it.

**Why refusing is kinder than allowing.** Settings replication flows leader →
follower only. A change made on a follower is not a change that goes anywhere:
the next backfill reconciles against the leader and the row is silently
reverted. The operator watches the write succeed and then watches it disappear.
A 503 says what is actually true.

**Inert on a single node.** Under the shipped default (`ha.role: leader`) the
leadership check is a configuration read with no I/O and always passes, so the
overwhelmingly common install gains no dependency and no failure mode. Only
`auto` consults Redis, and only on mutating requests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from terrapod.api.errors import jsonapi_error_response
from terrapod.logging_config import get_logger
from terrapod.services.ha_role import NotLeaderError, is_leader

logger = get_logger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Exact paths a follower still serves for a mutating method.
#:
#: The principle, and the only one that keeps this list from growing into a
#: loophole: **allow what records or reduces access on this node, never what
#: changes platform state.**
#:
#: An operator has to be able to open the follower's UI and read its HA status
#: *before* deciding to move DNS — so login has to work. Sessions live in that
#: node's own Redis and the only Postgres side effect is `last_login_at`.
#:
#: Deliberately absent: `POST /oauth/token`. The `terraform login` flow mints an
#: `APIToken` row, and `api_tokens` is a replicated class, so a token minted
#: here is erased by the next reconciliation. Handing out a credential that
#: later vanishes is worse than refusing it. (The `client_credentials` grant on
#: that same endpoint is the peer link, and a follower is the side that *calls*
#: it, never the side that serves it.)
FOLLOWER_WRITABLE_PATHS = frozenset(
    {
        "/api/terrapod/v1/auth/local/authorize",
        "/api/terrapod/v1/auth/local/login",
        "/api/terrapod/v1/auth/saml/acs",
        "/api/terrapod/v1/auth/token",
        "/api/terrapod/v1/auth/logout",
        "/api/terrapod/v1/auth/logout/all",
        # A listener must be able to enrol against whichever node it is pointed
        # at, and stay enrolled. It should not have to know, or care, whether
        # that node currently leads — a follower simply never sends it work
        # (`claim_next_run` returns nothing rather than failing), so there is no
        # work to leak by letting it attach.
        #
        # Enrolling records node-local state only: the listener lives in THIS
        # node's Redis with a TTL, and its certificate is signed by THIS node's
        # CA, neither of which replicates (#1143). That is the allow-list's
        # stated rule — records state on this node, does not change platform
        # state — so it belongs here rather than being a standing exception.
        #
        # Without this the whole listener protocol is refused on a follower, so
        # a standby's fleet can never be ready before promotion (#1191).
        "/api/terrapod/v1/agent-pools/join",
    }
)

#: Prefixes for the same, where the path carries an identifier.
#:
#: Session revocation only ever *removes* access, and only on this node, so it
#: is safe on a follower for the same reason logout is.
#: Keeping a listener alive is likewise node-local: the heartbeat refreshes a
#: Redis TTL, and a renewal re-signs against this node's own CA. The pool-scoped
#: join carries the pool id in the path, so it needs a prefix rather than an
#: exact match.
#:
#: Deliberately NOT here: the run-lifecycle calls under
#: `/listeners/{id}/runs/...` (job-launched, job-status, the PATCH). Those touch
#: run rows, which DO replicate. A follower dispatches nothing, so a listener
#: attached to one never has a run to report on — leaving them gated costs
#: nothing and keeps the boundary where the replication story needs it.
_LISTENER_KEEPALIVE = (
    "/api/terrapod/v1/listeners/{id}/heartbeat",
    "/api/terrapod/v1/listeners/{id}/renew",
)

FOLLOWER_WRITABLE_PREFIXES: tuple[str, ...] = (
    "/api/terrapod/v1/auth/sessions/user/",
    "/api/terrapod/v1/agent-pools/",  # …/{pool_id}/listeners/join — see below
)


def _matches_template(path: str, template: str) -> bool:
    """Exact segment-for-segment match, with `{...}` segments as wildcards."""
    parts = path.strip("/").split("/")
    expected = template.strip("/").split("/")
    if len(parts) != len(expected):
        return False
    return all(e.startswith("{") or e == p for e, p in zip(expected, parts, strict=True))


def _is_listener_keepalive(path: str) -> bool:
    """True for a listener's heartbeat or certificate renewal.

    Driven off `_LISTENER_KEEPALIVE` so the templates above are the single
    source of truth. They previously read as the allow-list while this function
    kept its own copy of the terminal segments, which agreed by luck: an edit to
    either one would not have reached the other, and the comment explaining
    which paths are deliberately excluded sits on the constant.

    Matched structurally rather than by prefix, so the run-lifecycle calls under
    the same `/listeners/{id}/` root stay refused.
    """
    return any(_matches_template(path, t) for t in _LISTENER_KEEPALIVE)


def is_follower_writable(path: str) -> bool:
    if path in FOLLOWER_WRITABLE_PATHS or _is_listener_keepalive(path):
        return True
    # The agent-pool prefix is deliberately narrow: only the pool-scoped join,
    # never pool or token administration, which changes platform state.
    if path.startswith("/api/terrapod/v1/agent-pools/") and path.endswith("/listeners/join"):
        return True
    return path.startswith(FOLLOWER_WRITABLE_PREFIXES[:1])


async def follower_write_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware: refuse a mutating request unless this node leads.

    Registered as the **innermost** user middleware, deliberately. A refusal is
    still counted by the metrics middleware, still carries the security headers,
    and is still written to the audit log — an attempted write against a
    follower is exactly the kind of thing an operator wants a record of.

    Returns the response rather than raising: an exception from a middleware
    escapes `ExceptionMiddleware` entirely, so the registered `NotLeaderError`
    handler would never see it and the caller would get a 500.
    """
    if request.method not in WRITE_METHODS or is_follower_writable(request.url.path):
        return await call_next(request)

    if await is_leader():
        return await call_next(request)

    exc = NotLeaderError("accept writes")
    logger.info(
        "Write refused: not the leader",
        method=request.method,
        path=request.url.path,
    )
    return jsonapi_error_response(str(exc), 503)
