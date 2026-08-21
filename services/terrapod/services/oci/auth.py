"""Credential handling for the OCI registry surface (#1408).

Container clients authenticate differently from everything else that talks to
Terrapod, so this is the one place the difference is absorbed.

**Basic, not Bearer.** Kubernetes ``imagePullSecrets`` are
``kubernetes.io/dockerconfigjson``, which carries a username and a password and
nothing else. The distribution spec permits Basic alongside the Docker bearer
flow, so accepting Basic means Terrapod needs **no token service** — a whole
subsystem avoided, and the reason this module is forty lines rather than four
hundred.

**The password is the credential; the username is ignored.** This is the pattern
GHCR uses, and it is what lets ``docker login terrapod.example.com -u anything
-p <terrapod-token>`` work without inventing a registry-specific identity.

**Any Terrapod credential is accepted**, because the password is treated exactly
as a Bearer token would be: API tokens, runner tokens and sessions all resolve.
That matters for the kubelet, whose pull secret is minted per-Job and scoped to
one run — see #1407 §9.

Bearer is accepted too, so ``curl`` and the Terrapod SDK can reach the surface
with the header they already send.

The resolution order below deliberately mirrors
:func:`terrapod.api.dependencies.authenticate_request`. It is not shared code
because the two differ in both credential encoding and error shape — this one
must answer in the spec's error envelope with a Basic challenge, or clients
report an unhelpful "unknown error" — but any change to the credential *types*
Terrapod accepts has to land in both.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import Request

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.services.oci.errors import UNAUTHORIZED, OCIError

#: Sent on a 401 so a client knows to retry with credentials. Docker will not
#: prompt for, or send, credentials without a challenge it recognises.
BASIC_CHALLENGE = 'Basic realm="terrapod"'


def extract_credential(request: Request) -> str | None:
    """Pull the bearer-equivalent credential out of an Authorization header.

    Returns ``None`` when the header is absent or unusable, so the caller can
    decide whether anonymous access is permitted for that route rather than
    having the decision made here.
    """
    header = request.headers.get("authorization", "")
    if not header:
        return None

    scheme, _, value = header.partition(" ")
    scheme = scheme.lower()

    if scheme == "bearer":
        return value.strip() or None

    if scheme == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            # Malformed base64 is indistinguishable from a wrong password as far
            # as the client is concerned, and saying which would be a small
            # oracle. Treated as "no credential".
            return None
        # Split on the FIRST colon. RFC 7617 forbids a colon in the userid and
        # permits one in the password, so everything after the first separator
        # is the password — which matters immediately: a runner token is
        # `runtok:{run}:{ttl}:{ts}:{sig}` and splitting on the last colon would
        # hand back only the signature.
        _username, sep, password = decoded.partition(":")
        if not sep:
            return None
        return password.strip() or None

    return None


async def authenticate_oci(request: Request) -> AuthenticatedUser:
    """Authenticate a registry request, or raise :class:`OCIError`.

    Mirrors ``authenticate_request``'s resolution order — runner token, then API
    token, then session — using its own short-lived DB session so nothing is
    held open across a large blob transfer.
    """
    from terrapod.api.dependencies import PEER_KIND, _resolve_user_roles, validate_api_token
    from terrapod.auth.runner_tokens import verify_runner_token
    from terrapod.auth.sessions import get_session
    from terrapod.db.session import get_db_session

    token = extract_credential(request)
    if not token:
        raise OCIError(UNAUTHORIZED, message="authentication required")

    # Runner token first: pure HMAC verification, no I/O.
    if token.startswith("runtok:"):
        run_id = verify_runner_token(token)
        if run_id is not None:
            request.state.user_email = "runner"
            return AuthenticatedUser(
                email="runner",
                display_name="Runner Job",
                roles=["everyone"],
                provider_name="runner_token",
                auth_method="runner_token",
                run_id=run_id,
            )

    async with get_db_session() as db:
        api_token = await validate_api_token(db, token)
        if api_token is not None:
            # A replication peer is not a user, and must not be able to act as
            # one here any more than anywhere else.
            if api_token.kind == PEER_KIND:
                raise OCIError(UNAUTHORIZED, message="authentication required")
            email = api_token.bound_to or ""
            roles = await _resolve_user_roles(db, email) if email else []
            request.state.user_email = email
            return AuthenticatedUser(
                email=email,
                display_name=None,
                roles=roles,
                provider_name="api_token",
                auth_method="api_token",
                kind=api_token.kind,
                pinned_roles=api_token.pinned_roles,
            )

    session = await get_session(token)
    if session is not None:
        request.state.user_email = session.email
        return AuthenticatedUser(
            email=session.email,
            display_name=session.display_name,
            roles=session.roles,
            provider_name=session.provider_name,
            auth_method="session",
        )

    raise OCIError(UNAUTHORIZED, message="authentication required")
