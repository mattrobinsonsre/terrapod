"""The two public issuer documents, through the REAL application (#1901).

Everything else that watches these endpoints looks at something other than the
mounted app, and that is the gap this file closes.

**`test_oidc_issuer.py` reads the module-level `APIRouter`.** Its
`TestTheTwoPublicPathsCannotMove._mounted()` iterates `router.router.routes`, so
it pins the two path strings as the *decorators* spell them and says nothing
about where `create_application` ends up serving them. Change
`app.include_router(oidc_issuer_router)` in `api/app.py` to the
`include_terrapod(...)` helper every other router uses and both documents move
under the API prefixes: a cloud's anonymous fetch of
`/.well-known/openid-configuration` 404s, every federated workspace fails at
token exchange, and that whole file stays green.

**The route contract cannot see it either**, and structurally so: `app.py`
mounts this router inside `if settings.auth.oidc_issuer.enabled:`, the setting
defaults to false, and the snapshot generator builds the app with the defaults —
so neither path has ever appeared in `api_route_contract.json`. It is the only
conditionally mounted router in the application, which makes it the only place
the route gate is blind, and it happens to be the place where a rename is least
recoverable: a cloud is CONFIGURED with the issuer URL and fetches these exact
paths itself, so there is no deprecation window available for a trust root.

**And `TestTheJWKSResponse` patches `get_jwks` to `{"keys": []}`.** Every
assertion in that class is about `Cache-Control`; the response body is never
inspected, with real keys or at all. So the single highest-consequence bug this
endpoint can have — serving private key material from the deployment's own
signing key, anonymously — fails no test. Making the route return
`content={**get_jwks(), "pem": [k.private_key_pem for k in _keys]}` passes the
entire suite.

So these tests do the two things those cannot: they go through the mounted
application with no `Authorization` header, and they inspect the body with a
real RSA key loaded.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.auth import oidc_signing
from terrapod.config import settings

_BASE = "http://test"

DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/.well-known/jwks.json"

#: The private half of an RSA JWK, plus `oth` for the multi-prime form. Named
#: individually because the property is that each is absent, not that some
#: filter happened to run over the key's own members.
PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth")


@pytest.fixture(scope="module")
def signing_key():
    """One real 2048-bit key for the module — generation is the slow part."""
    return oidc_signing.generate_private_key()


@pytest.fixture
def issuer_app(signing_key, monkeypatch):
    """The real application, built with the issuer published and keys loaded.

    `enabled` is read inside `create_application`, so it has to be true BEFORE
    the app is built — which is the whole reason nothing else in the suite
    exercises these routes.

    The key set is installed directly rather than through `init_oidc_signing`
    because what is under test is the HTTP surface over it, not the resolution
    of the set; a real key is what matters, so the response body carries real
    modulus material and a leak would be visible.

    `public_url` is set with `monkeypatch` rather than a context manager around
    the build, because `issuer_url()` is read per REQUEST — a patch that lapses
    after `create_application` returns leaves the document advertising an empty
    issuer, which is itself the agreement failure this surface exists to avoid.
    """
    from terrapod.api.app import create_application

    pem = oidc_signing.serialize_private_key(signing_key)
    kid = oidc_signing.compute_kid(signing_key)

    monkeypatch.setattr(
        oidc_signing, "_keys", [oidc_signing.SigningKey(kid=kid, private_key_pem=pem)]
    )
    monkeypatch.setattr(oidc_signing, "_signing_kid", kid)
    monkeypatch.setattr(oidc_signing, "_jwks_cache", None)
    monkeypatch.setattr(settings.auth.oidc_issuer, "public_url", "https://issuer.example.test")
    monkeypatch.setattr(settings.auth.oidc_issuer, "enabled", True)

    return create_application(), kid, pem


async def _get(app, path):
    """A GET with NO Authorization header — the only way a cloud ever arrives."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        resp = await c.get(path)
    assert "authorization" not in {k.lower() for k in resp.request.headers}
    return resp


class TestBothDocumentsAreServedAnonymouslyAtTheRootWhenTheIssuerIsEnabled:
    """The mounting itself, which is what no other test reaches.

    A cloud fetches these at a fixed well-known path, before any token exists,
    with no credential. So the three things that have to hold are: 200, at the
    exact unprefixed path, with nothing presented.
    """

    async def test_the_discovery_document_is_served_unprefixed_and_unauthenticated(
        self, issuer_app
    ):
        app, _kid, _pem = issuer_app
        resp = await _get(app, DISCOVERY_PATH)
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["issuer"] == "https://issuer.example.test"
        assert doc["jwks_uri"] == "https://issuer.example.test" + JWKS_PATH

    async def test_the_jwks_is_served_unprefixed_and_unauthenticated(self, issuer_app):
        app, kid, _pem = issuer_app
        resp = await _get(app, JWKS_PATH)
        assert resp.status_code == 200, resp.text
        assert [k["kid"] for k in resp.json()["keys"]] == [kid]

    async def test_neither_path_is_served_under_an_api_prefix(self, issuer_app):
        """The mutation's own signature, stated positively.

        `include_terrapod` would serve both under `/api/terrapod/v1` and
        `/api/v2` and leave the root paths 404. Asserting the root paths work is
        most of the guard; asserting the prefixed ones do NOT is what stops
        someone "fixing" a 404 by mounting the router in both places, which
        would publish a second address for a trust root.
        """
        app, _kid, _pem = issuer_app
        for prefix in ("/api/terrapod/v1", "/api/v2", "/api/v1"):
            for path in (DISCOVERY_PATH, JWKS_PATH):
                resp = await _get(app, prefix + path)
                assert resp.status_code == 404, f"{prefix + path} is served: {resp.status_code}"

    async def test_the_advertised_jwks_uri_is_fetchable_from_the_same_app(self, issuer_app):
        """End to end as a cloud does it: read `jwks_uri` out of the document,
        strip the issuer, and fetch that path from the same application.

        The two path literals are independent — a `@router.get` decorator and an
        f-string inside the discovery document — and the cloud believes the
        document over the router.
        """
        app, kid, _pem = issuer_app
        doc = (await _get(app, DISCOVERY_PATH)).json()
        advertised = doc["jwks_uri"].removeprefix(doc["issuer"])
        resp = await _get(app, advertised)
        assert resp.status_code == 200, (
            f"the discovery document advertises {advertised!r}, which this "
            f"application does not serve — a cloud would fetch it and get a "
            f"{resp.status_code}"
        )
        assert [k["kid"] for k in resp.json()["keys"]] == [kid]

    async def test_nothing_is_published_when_the_issuer_is_not_enabled(self):
        """The opt-in, from the other side: off means the router is not mounted
        at all rather than mounted and refusing. A deployment that has not opted
        in publishes no trust root, which is a stronger statement than a 404 on
        a path that exists — but it is the same 404 to a caller, so this is what
        proves the fixture above is doing something.
        """
        from terrapod.api.app import create_application

        with patch.object(settings.auth.oidc_issuer, "enabled", False):
            app = create_application()
        for path in (DISCOVERY_PATH, JWKS_PATH):
            assert (await _get(app, path)).status_code == 404


class TestTheJWKSBodyCarriesNoPrivateKeyMaterial:
    """The leak that fails nothing today.

    `public_jwk` is well covered one layer down, but the route is what the
    public reaches, and the route is free to add to the document it serves. With
    the body never inspected at HTTP level, adding the PEMs beside the keys
    passes every Python test in the repository — and the deployment's signing
    key is then downloadable by anyone who can reach the ingress, which is
    everyone, because a cloud has to be able to.

    Asserted over the WHOLE response body rather than per-JWK, because the leak
    does not have to be inside a `keys` entry to be a leak.
    """

    async def test_no_private_rsa_member_appears_anywhere_in_the_body(self, issuer_app):
        app, _kid, _pem = issuer_app
        body = (await _get(app, JWKS_PATH)).json()

        for jwk in body["keys"]:
            for member in PRIVATE_JWK_MEMBERS:
                assert member not in jwk, f"the published JWK carries {member!r}"
            # The allow-list, pinned here too: a route that reached past
            # `public_jwk` and built its own dict would satisfy the loop above
            # while publishing something nobody reviewed.
            assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}

        # Anything outside `keys` is just as public. RFC 7517 defines no sibling
        # member of a JWK Set, so a second top-level key is by definition
        # something we added.
        assert set(body) == {"keys"}

    async def test_no_pem_material_appears_anywhere_in_the_body(self, issuer_app):
        """A substring check over the serialised response, which catches a leak
        under any member name — including one nested inside a JWK, which the
        per-member loop above would miss."""
        app, _kid, pem = issuer_app
        raw = (await _get(app, JWKS_PATH)).text
        assert "PRIVATE KEY" not in raw
        assert "BEGIN" not in raw
        assert pem not in raw
        # The base64 body of the PEM, in case something strips the armour.
        inner = "".join(line for line in pem.splitlines() if "-----" not in line)
        assert inner[:64] not in raw

    async def test_the_published_modulus_is_the_real_key_so_this_is_not_vacuous(self, issuer_app):
        """The assertions above are all absences, and an empty body satisfies
        every one of them — which is exactly what `TestTheJWKSResponse` patches
        `get_jwks` down to. Pin that a real key reached the response, so the
        file cannot quietly become a test of nothing."""
        app, kid, _pem = issuer_app
        key = (await _get(app, JWKS_PATH)).json()["keys"][0]
        assert key["kid"] == kid
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        # 2048-bit modulus, base64url, unpadded.
        assert len(key["n"]) > 300

        # And it verifies: the published key is the one that signs, so a token
        # minted here is checkable with nothing but this document.
        import jwt

        with patch.object(settings.auth.oidc_issuer, "token_ttl_seconds", 900):
            token = oidc_signing.sign_identity_token(
                {"iss": "https://issuer.example.test", "sub": "workspace:w", "aud": ["a"]},
                ttl_seconds=900,
            )
        pub = jwt.PyJWK({**key, "use": "sig"}).key
        decoded = jwt.decode(token, pub, algorithms=["RS256"], audience="a")
        assert decoded["sub"] == "workspace:w"

    async def test_the_discovery_document_carries_no_key_material_either(self, issuer_app):
        """It is the other anonymous document, and it is the one an operator is
        most likely to extend — `claims_supported` grows, and a key or a PEM
        would be just as public from here."""
        app, _kid, pem = issuer_app
        raw = (await _get(app, DISCOVERY_PATH)).text
        assert "PRIVATE KEY" not in raw
        assert pem not in raw
        doc = json.loads(raw)
        for member in PRIVATE_JWK_MEMBERS:
            assert member not in doc
        assert "keys" not in doc
