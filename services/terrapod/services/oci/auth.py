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

Header parsing lives in :mod:`terrapod.api.credentials`, shared with the
package-cache proxies, which need Basic for pip and Bearer for npm (#1417). The
*error shape* stays here and is not shared: this surface must answer in the
distribution spec's error envelope with a Basic challenge, or clients report an
unhelpful "unknown error".

The resolution order below deliberately mirrors
:func:`terrapod.api.dependencies.authenticate_request`; any change to the
credential *types* Terrapod accepts has to land in both.
"""

from __future__ import annotations

from fastapi import Request

from terrapod.api.credentials import extract_credential
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.services.oci.errors import UNAUTHORIZED, OCIError

#: Sent on a 401 so a client knows to retry with credentials. Docker will not
#: prompt for, or send, credentials without a challenge it recognises.
BASIC_CHALLENGE = 'Basic realm="terrapod"'


async def authenticate_oci(request: Request) -> AuthenticatedUser:
    """Authenticate a registry request, or raise :class:`OCIError`.

    Mirrors ``authenticate_request``'s resolution order — runner token, then API
    token, then session — using its own short-lived DB session so nothing is
    held open across a large blob transfer.
    """
    from terrapod.api.dependencies import PEER_KIND, _resolve_user_roles, validate_api_token
    from terrapod.auth.runner_token_state import is_run_token_usable_on_its_own_session
    from terrapod.auth.runner_tokens import verify_runner_token_claims
    from terrapod.auth.sessions import get_session
    from terrapod.db.session import get_db_session

    token = extract_credential(request)
    if not token:
        raise OCIError(UNAUTHORIZED, message="authentication required")

    # Runner token first: HMAC verification, then the run-liveness check every
    # runner-token path makes (GHSA-xmrf-hxq9-m59m). This surface has its own auth
    # function, so it needs its own call — a token whose run has ended must not
    # keep pulling images any more than it keeps uploading artifacts.
    if token.startswith("runtok:"):
        claims = verify_runner_token_claims(token)
        if claims is not None:
            if not await is_run_token_usable_on_its_own_session(claims.run_id):
                raise OCIError(UNAUTHORIZED, message="authentication required")
            request.state.user_email = "runner"
            return AuthenticatedUser(
                email="runner",
                display_name="Runner Job",
                roles=["everyone"],
                provider_name="runner_token",
                auth_method="runner_token",
                run_id=claims.run_id,
                run_phase=claims.phase,
            )

    async with get_db_session() as db:
        api_token = await validate_api_token(db, token)
        if api_token is not None:
            # A replication peer is not a user, and must not be able to act as
            # one here any more than anywhere else.
            if api_token.kind == PEER_KIND:
                raise OCIError(UNAUTHORIZED, message="authentication required")
            email = api_token.bound_to or ""
            roles = (
                await _resolve_user_roles(
                    db, email, api_token.identity_provider, api_token.identity_subject
                )
                if email
                else []
            )
            request.state.user_email = email
            return AuthenticatedUser(
                email=email,
                display_name=None,
                roles=roles,
                provider_name="api_token",
                auth_method="api_token",
                identity_provider=api_token.identity_provider,
                identity_subject=api_token.identity_subject,
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
            identity_provider=session.provider_name,
            identity_subject=session.subject,
        )

    raise OCIError(UNAUTHORIZED, message="authentication required")
