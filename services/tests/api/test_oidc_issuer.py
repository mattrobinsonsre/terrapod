"""The OIDC issuer's two public documents (#1901).

These are the only endpoints Terrapod serves that MUST be anonymous: a cloud
fetches them before any token exists, to decide whether to trust one. So the
properties worth pinning are not about authorisation but about agreement and
caching.

**Agreement is the one that fails invisibly.** OIDC issuer matching is exact —
the cloud is configured with an issuer, fetches the discovery document itself,
and then checks that a token's `iss` equals what it was configured with. Three
values have to agree: `issuer_url()`, the document's `issuer`, and its
`jwks_uri`. If one of them resolves to the private management hostname, every
token is rejected at the cloud's token exchange, with nothing wrong on the
Terrapod side to look at.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from terrapod.api.routers import oidc_issuer as router


def _settings(*, public_url="", webhook="", external="", propagation=600):
    s = MagicMock()
    s.auth.oidc_issuer.public_url = public_url
    s.auth.oidc_issuer.key_propagation_seconds = propagation
    s.public_webhook_url = webhook
    s.external_url = external
    return s


class TestTheIssuerURL:
    """One source, because three consumers must agree on it."""

    def test_the_explicit_setting_wins(self):
        with patch(
            "terrapod.config.settings", _settings(public_url="https://a", webhook="https://b")
        ):
            assert router.issuer_url() == "https://a"

    def test_the_public_webhook_url_is_preferred_over_external_url(self):
        """The webhook surface is the one already deliberately public, so it is
        the only one a cloud can be assumed to reach."""
        with patch(
            "terrapod.config.settings", _settings(webhook="https://hooks", external="https://mgmt")
        ):
            assert router.issuer_url() == "https://hooks"

    def test_external_url_is_the_last_resort(self):
        with patch("terrapod.config.settings", _settings(external="https://mgmt")):
            assert router.issuer_url() == "https://mgmt"

    def test_a_trailing_slash_is_stripped(self):
        """Issuer matching is exact, so a trailing slash is a different issuer
        and would be rejected by every cloud."""
        with patch("terrapod.config.settings", _settings(public_url="https://a/")):
            assert router.issuer_url() == "https://a"


class TestTheDiscoveryDocument:
    async def _doc(self, **kw):
        with patch("terrapod.config.settings", _settings(**kw)):
            resp = await router.openid_configuration()
        return resp, json.loads(resp.body)

    async def test_the_issuer_and_the_jwks_uri_agree(self):
        """The failure this guards is silent: a mismatch is only visible at the
        cloud's token exchange."""
        _, doc = await self._doc(public_url="https://issuer.example.com")
        assert doc["issuer"] == "https://issuer.example.com"
        assert doc["jwks_uri"] == "https://issuer.example.com/.well-known/jwks.json"

    async def test_only_rs256_is_advertised(self):
        _, doc = await self._doc(public_url="https://i")
        assert doc["id_token_signing_alg_values_supported"] == ["RS256"]

    async def test_the_phase_claim_is_advertised(self):
        """A trust policy conditions on it, so a cloud's own validation tooling
        needs to see it listed."""
        _, doc = await self._doc(public_url="https://i")
        assert "phase" in doc["claims_supported"]
        assert "workspace" in doc["claims_supported"]

    async def test_no_token_or_authorization_endpoint_is_advertised(self):
        """Terrapod is not an authorization server for these tokens. Advertising
        endpoints that do not exist invites a client to try them."""
        _, doc = await self._doc(public_url="https://i")
        assert "token_endpoint" not in doc
        assert "authorization_endpoint" not in doc
        assert "registration_endpoint" not in doc

    async def test_it_is_cacheable(self):
        resp, _ = await self._doc(public_url="https://i")
        assert "max-age" in resp.headers["cache-control"]


class TestTheJWKSResponse:
    async def _call(self, *, propagation):
        with (
            patch("terrapod.config.settings", _settings(propagation=propagation)),
            patch("terrapod.auth.oidc_signing.get_jwks", return_value={"keys": []}),
        ):
            return await router.jwks()

    async def test_the_max_age_is_half_the_propagation_window(self):
        """Derived, not configured: the propagation window exists *because* the
        clouds cache this document, so advertising a longer cache lifetime than
        the window would mean a cloud still holding the old key set at the
        moment Terrapod starts signing with the new one."""
        resp = await self._call(propagation=600)
        assert resp.headers["cache-control"] == "public, max-age=300"

    async def test_a_short_window_still_gets_the_floor(self):
        """Caching is also the mitigation for the endpoint being necessarily
        unauthenticated, so it must not be effectively disabled."""
        resp = await self._call(propagation=10)
        assert resp.headers["cache-control"] == f"public, max-age={router._JWKS_MIN_MAX_AGE}"

    async def test_a_zero_window_still_gets_the_floor(self):
        resp = await self._call(propagation=0)
        assert resp.headers["cache-control"] == f"public, max-age={router._JWKS_MIN_MAX_AGE}"

    async def test_the_max_age_never_exceeds_the_propagation_window(self):
        """The property, across a range — stated as the inequality that matters
        rather than as one arithmetic example."""
        for window in (120, 600, 1800, 7200):
            with (
                patch("terrapod.config.settings", _settings(propagation=window)),
                patch("terrapod.auth.oidc_signing.get_jwks", return_value={"keys": []}),
            ):
                resp = await router.jwks()
            age = int(resp.headers["cache-control"].rsplit("=", 1)[1])
            assert age <= window, f"advertised a {age}s cache inside a {window}s window"
