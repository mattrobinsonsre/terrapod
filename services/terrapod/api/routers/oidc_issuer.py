"""Terrapod as an OIDC issuer for runs — the two public GETs (#1901).

    GET /.well-known/openid-configuration   the discovery document
    GET /.well-known/jwks.json              the signing key set

Both are **unauthenticated by necessity, not by oversight**: a cloud fetches them
anonymously, before any token exists, to decide whether to trust one. They
publish only public key material and the issuer's own URL.

Both are mounted only when `auth.oidc_issuer.enabled` — and off means the router
is not mounted at all rather than mounted and refusing. A deployment that has not
opted in publishes no trust root, which is a stronger and more legible statement
than a 404 on a path that exists.

These sit under `/.well-known/` beside `terraform.json`, which the BFF already
proxies (`web/next.config.js`), so they need no new rewrite — everything still
arrives through the BFF as architecture rule 8 requires. They do need listing in
`webhookIngress.paths`, because that allow-list is what makes a path publicly
reachable, and they must be listed EXACTLY rather than as `/.well-known`: the
prefix would bring the terraform service-discovery document along with it, which
is harmless but should be a decision rather than a side effect.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["oidc-issuer"])


def issuer_url() -> str:
    """The issuer URL. **One source, and every consumer reads it from here.**

    OIDC issuer matching is exact: the cloud is configured with an issuer, fetches
    `<issuer>/.well-known/openid-configuration` itself, and then validates that a
    token's `iss` equals what it was configured with. Three things inside the API
    have to agree — this value, the discovery document's `issuer`, and its
    `jwks_uri` — so computing it in three places means one of them eventually uses
    the private management hostname, and then every token is rejected at
    assume-role time, in the cloud, with nothing wrong on our side to look at.

    Derived rather than configured where it can be: the explicit setting wins,
    else the public webhook URL (the surface already deliberately public, which is
    the only one a cloud can reach), else `external_url`.
    """
    from terrapod.config import settings

    explicit = (settings.auth.oidc_issuer.public_url or "").strip()
    if explicit:
        return explicit.rstrip("/")
    webhook = (settings.public_webhook_url or "").strip()
    if webhook:
        return webhook.rstrip("/")
    return (settings.external_url or "").strip().rstrip("/")


@router.get("/.well-known/openid-configuration")
async def openid_configuration() -> JSONResponse:
    """The discovery document.

    Deliberately minimal. Terrapod is not an OAuth authorization server for
    these tokens — there is no authorization endpoint, no token endpoint and no
    client registration, because the only consumer is a cloud validating a token
    Terrapod already minted. Advertising endpoints that do not exist would invite
    a client to try them.

    `response_types_supported` and `subject_types_supported` are present because
    the OIDC discovery spec requires them and some validators refuse a document
    without them, even for a flow that never runs.
    """
    base = issuer_url()
    return JSONResponse(
        content={
            "issuer": base,
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "nbf",
                "jti",
                "workspace",
                "workspace_id",
                "phase",
                "run_id",
                "terrapod_organization",
            ],
        }
    )


@router.get("/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    """The published signing keys.

    A set rather than one key, because a rotation publishes the new key before it
    starts signing and keeps the retired one until the tokens it signed expire.
    A cloud picks the key by the token's `kid`.
    """
    from terrapod.auth.oidc_signing import get_jwks

    return JSONResponse(content=get_jwks())
