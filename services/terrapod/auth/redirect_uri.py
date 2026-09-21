"""Where an authorization code is allowed to be delivered.

Two routes start an authorization flow — `/oauth/authorize` for `terraform
login` and `/auth/authorize` for the web UI — and both store a client-supplied
`redirect_uri` against the auth state. Whatever is stored there is later handed
the authorization code: the CLI flow fetches it from `/auth/cli-complete`, and
the session flow is 302'd straight at it by the callback handlers.

Neither route validated it, so a crafted link sent to a victim returned a code
minted for that victim, exchangeable for a token as them. PKCE is no defence —
the attacker generates both halves of the challenge.

**Validation lives here, and is applied by `store_auth_state`, so it cannot be
bypassed by adding a third route.** That is the point: the bug was two routes
with one missing check, and per-route validation would have left the next route
to remember.

The two flows have genuinely different legitimate shapes, so there are two
allow-lists rather than one:

- `api_token` (the CLI) redirects to a loopback listener the CLI itself started,
  on the port range the service discovery document advertises.
- `session` (the web UI) redirects back to this deployment's own callback page,
  so the only legitimate target is our own origin.

Both are allow-lists rather than deny-lists of known-bad schemes: `javascript:`,
a non-loopback host and a port outside the range are each refused by not being
on the list, which is what keeps the next unanticipated spelling out too.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The loopback port range `terraform login` binds, published in the discovery
#: document as `login.v1.ports`. One constant feeds both, so the contract we
#: advertise and the contract we enforce cannot drift apart — they had, and the
#: allow-list was written down but never checked.
LOGIN_PORTS = (10000, 10010)

#: Hosts a CLI redirect may name. The flow hands a code to a client listening on
#: *this* machine, so anything resolving elsewhere is not a case to support.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class InvalidRedirectURI(ValueError):
    """The supplied `redirect_uri` is not a permitted destination."""


def _split(redirect_uri: str) -> tuple:
    try:
        return urlsplit(redirect_uri)
    except ValueError as exc:  # malformed enough that urlsplit itself refuses
        raise InvalidRedirectURI("redirect_uri is not a valid URL") from exc


def _reject_common(parts) -> None:  # type: ignore[no-untyped-def]
    """Checks both flows share."""
    # Credentials in the authority would make the stored URI carry a second
    # identity nothing downstream expects. It is also the classic way to smuggle
    # a foreign host past a naive check: in `http://localhost:10000@evil.tld/`
    # the host is evil.tld and `localhost:10000` is merely userinfo.
    if parts.username is not None or parts.password is not None:
        raise InvalidRedirectURI("redirect_uri must not contain credentials")
    # A fragment is never delivered to a server, and appending the code after
    # one silently strands it in the browser.
    if parts.fragment:
        raise InvalidRedirectURI("redirect_uri must not contain a fragment")


def validate_cli_redirect_uri(redirect_uri: str) -> None:
    """The `terraform login` flow: a loopback listener on the advertised ports."""
    parts = _split(redirect_uri)
    lo, hi = LOGIN_PORTS
    if parts.scheme != "http":
        raise InvalidRedirectURI("redirect_uri must be an http loopback URL")
    # `.hostname` lower-cases, strips brackets from an IPv6 literal, and returns
    # None when there is no authority — which `javascript:alert(1)` and a bare
    # path both are.
    if parts.hostname not in _LOOPBACK_HOSTS:
        raise InvalidRedirectURI("redirect_uri must name a loopback host")
    # `.port` raises rather than returning None for a non-numeric or
    # out-of-range port, which is itself a rejection.
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidRedirectURI("redirect_uri has an invalid port") from exc
    if port is None or not (lo <= port <= hi):
        raise InvalidRedirectURI(f"redirect_uri port must be in {lo}-{hi}")
    _reject_common(parts)


def validate_session_redirect_uri(redirect_uri: str, *, allowed_origin: str) -> None:
    """The web flow: this deployment's own origin, and nothing else.

    `allowed_origin` is the configured callback base (falling back to
    `external_url`) — the same value the callback URL handed to the IdP is built
    from, so a redirect we accept is one we would have sent the user to anyway.
    """
    parts = _split(redirect_uri)
    origin = _split(allowed_origin.strip().rstrip("/"))
    if not origin.scheme or not origin.netloc:
        # Nothing configured to compare against. Fail closed: an unvalidated
        # redirect is how this bug worked in the first place.
        raise InvalidRedirectURI("no configured origin to validate redirect_uri against")
    if (parts.scheme, parts.netloc.lower()) != (origin.scheme, origin.netloc.lower()):
        raise InvalidRedirectURI("redirect_uri must be on this deployment's own origin")
    _reject_common(parts)


def validate_redirect_uri(redirect_uri: str, *, credential_type: str, allowed_origin: str) -> None:
    """Dispatch on which flow is being started."""
    if credential_type == "api_token":
        validate_cli_redirect_uri(redirect_uri)
    else:
        validate_session_redirect_uri(redirect_uri, allowed_origin=allowed_origin)
