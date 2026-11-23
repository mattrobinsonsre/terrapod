"""Terrapod as an OIDC issuer for a STORAGE LOCATION, not for a run (#2590).

A storage location configured `credentials.type: federated` authenticates to its
cloud by presenting a short-lived RS256 JWT this deployment minted and letting the
cloud exchange it for its own credentials. That is the same mechanism per-workspace
cloud identity uses (#1901) and deliberately the same issuer and the same signing
key -- a second key would buy isolation nothing here needs and cost a second
rotation story, a second JWKS entry and a second propagation window, and the
propagation window is precisely what becomes a storage dependency.

**So the audience is the discriminator, and it is the whole of it.** A run identity
token describes a run: which workspace, which phase, which pool. A storage token
describes a LOCATION and is minted by the API for itself, very often outside any run
at all -- a configuration-version upload, a state read, the retention sweep, a cache
eviction -- so there is no workspace and no phase to put in it. The two claim sets
therefore have almost nothing in common, and what keeps one from being presented
where the other belongs is `aud`: `Settings` refuses a configuration in which a
location's audience is also a run audience, or in which two locations share one.
`sub` differs too (`storage:<name>` against `workspace:<name>@<id>`), and that is
worth having because it is the only claim an AWS or Azure trust policy can bind
besides the audience -- but a policy conditioning on audience alone is both legal
and common, which is why the audience half is enforced in configuration rather than
left to the operator's trust policy to get right.

Nothing here is cloud-specific, exactly as `cloud_identity` is not: this mints a
token and `storage.credentials` decides which exchange consumes it.
"""

from __future__ import annotations

#: The `sub` prefix for a storage token. Chosen so it cannot collide with a run
#: subject: `_subject` in `api/routers/cloud_identity.py` always begins
#: `workspace:` and a storage subject always begins `storage:`, and neither prefix
#: is a legal storage-location name (the grammar is `[a-z0-9-]{1,32}`, which admits
#: no colon) so the segments cannot run together.
STORAGE_SUBJECT_PREFIX = "storage:"

#: Claims a RUN identity token carries that a storage token must never carry.
#:
#: Not a style rule -- each one is something a cloud-side policy may condition on,
#: so a storage token carrying any of them could satisfy a policy written for a run.
#: `test_storage_identity.py` asserts the minted claim set is disjoint from this,
#: derived from the mint rather than from a copy of it, so a claim added to the
#: storage token later has to be considered against this list rather than silently
#: widening what the token can be presented as.
RUN_ONLY_CLAIMS = frozenset(
    {
        "workspace",
        "workspace_id",
        "run_id",
        "phase",
        "agent_pool",
        "agent_pool_id",
    }
)


def storage_subject(location: str) -> str:
    """The `sub` a token for this location carries."""
    return f"{STORAGE_SUBJECT_PREFIX}{location}"


def storage_identity_claims(location: str, *, audience: str, issuer: str) -> dict:
    """The claim set, without `iat`/`nbf`/`exp`/`jti`, which the signer adds.

    Separate from `mint_storage_identity_token` so the shape can be asserted without
    a loaded signing key, and so the claims a test reads are the claims the mint
    signs rather than a second copy of them.

    `aud` is a LIST of one. A single-element list and a bare string are equivalent
    to every verifier, and the list form is what the run mint uses -- but the
    deeper reason is that a token audienced for several targets is replayable
    between them, and AWS refuses a multi-valued `aud` outright, so the list is
    kept at one element rather than kept open.
    """
    return {
        "iss": issuer,
        "sub": storage_subject(location),
        "aud": [audience],
        # The location, as a discrete claim as well as inside `sub`, because a
        # target that can read claims -- GCP, OpenBao/Vault, anything with Rego --
        # can then bind it without parsing a composite subject. AWS and Azure
        # cannot, which is why `sub` carries it too.
        "terrapod_storage_location": location,
        # Single-organization deployment, so this is the literal `default`. Here
        # for symmetry with the run token's claim set and with what other issuers
        # publish; the issuer URL is what actually tells two deployments apart.
        "terrapod_organization": "default",
    }


def mint_storage_identity_token(location: str, *, audience: str, ttl_seconds: int) -> str:
    """Sign a storage identity token for `location`.

    Raises if no signing key is loaded -- which is the honest outcome: a federated
    location with no issuer key cannot authenticate, and `Settings` already refuses
    the configuration in which the issuer is disabled altogether, so reaching this
    with nothing loaded means the issuer failed to initialise. The caller is a
    credential refresh inside an SDK, so the failure surfaces as that location
    being unreachable, recorded against the location by the reachability probe and
    leaving every other location serving.
    """
    # In-function, both of them. `terrapod.auth.oidc_signing` pulls in the database
    # session and `issuer_url` lives on an API router, and this module is imported
    # from `terrapod.storage`, which is built during lifespan -- the same ordering
    # `cloud_identity` has and for the same reason.
    from terrapod.api.routers.oidc_issuer import issuer_url
    from terrapod.auth.oidc_signing import sign_identity_token

    claims = storage_identity_claims(location, audience=audience, issuer=issuer_url())
    return sign_identity_token(claims, ttl_seconds=ttl_seconds)
