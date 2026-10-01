"""Where a SAML assertion is expected to arrive, and what it is checked against.

One function answers both, and that is the point. The AuthnRequest advertises an
ACS URL, the IDP mirrors it back as the assertion's `Destination` and
`Recipient`, and the ACS endpoint compares them against what it believes its own
address to be. Let those two be computed differently and the Destination check
stops being a security control and starts being an outage.
"""

from __future__ import annotations

import pytest

from terrapod.api.routers import auth as auth_router
from terrapod.auth.connectors.saml import SAMLConnector
from terrapod.config import SAMLProviderConfig


def _saml(**overrides) -> SAMLConnector:
    base = {"name": "idp", "metadata_url": "https://idp.test/metadata"}
    base.update(overrides)
    return SAMLConnector(SAMLProviderConfig(**base))


class _NotSaml:
    provider_type = "oidc"


class TestTheAcsUrlIsResolvedOnce:
    def test_it_is_built_from_the_callback_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://terrapod.example.com"
        )
        url = auth_router._saml_acs_url(_saml())
        assert url.startswith("https://terrapod.example.com/")
        assert url.endswith("/auth/saml/acs")

    def test_a_provider_acs_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """For a deployment whose external path is not the one it serves."""
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://internal.example.com"
        )
        connector = _saml(acs_url="https://sso.example.com/custom/acs")
        assert auth_router._saml_acs_url(connector) == "https://sso.example.com/custom/acs"

    def test_external_url_is_the_fallback_not_the_first_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order matters.

        `callback_base_url` is what the operator registered with the IDP, so it
        is what the IDP echoes back. Checking against a different base would
        refuse every assertion on a deployment where the two disagree.
        """
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://registered.example.com"
        )
        monkeypatch.setattr(auth_router.settings, "external_url", "https://other.example.com")
        assert auth_router._saml_acs_url(_saml()).startswith("https://registered.example.com/")

        monkeypatch.setattr(auth_router.settings.auth, "callback_base_url", "")
        assert auth_router._saml_acs_url(_saml()).startswith("https://other.example.com/")

    def test_a_trailing_slash_does_not_double_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://terrapod.example.com/"
        )
        assert "//auth" not in auth_router._saml_acs_url(_saml()).removeprefix("https://")

    def test_it_is_built_on_the_native_prefix_this_line_serves(self) -> None:
        """One native prefix here, so there is no switch to follow.

        On the 2.x line this reads `auth.legacy_callback_url` to choose between
        two native prefixes. This line serves `/api/terrapod/v1` alone, so the
        ACS URL is built from `terrapod_prefix` directly — and the thing worth
        pinning is that it names the prefix an assertion can actually be posted
        to, since the URL is also what the operator registers with the IDP.
        """
        from terrapod.config import settings as live

        url = auth_router._saml_acs_url(_saml())
        assert url.endswith(f"{live.terrapod_prefix}/auth/saml/acs")
        assert "/api/v1/" not in url


class TestTheIdpIsToldWhereToPost:
    def test_a_saml_provider_is_sent_to_the_acs_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/auth/callback` is GET-only and cannot accept an assertion.

        The AuthnRequest used to advertise it anyway, which is both wrong on its
        face and the reason the advertised URL and the checked URL could not be
        the same string.
        """
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://terrapod.example.com"
        )
        assert auth_router._idp_callback_url(_saml()).endswith("/auth/saml/acs")

    def test_everything_else_still_goes_to_the_shared_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://terrapod.example.com"
        )
        assert auth_router._idp_callback_url(_NotSaml()).endswith("/auth/callback")

    def test_what_we_advertise_is_what_we_check_against(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invariant the whole Destination check rests on."""
        monkeypatch.setattr(
            auth_router.settings.auth, "callback_base_url", "https://terrapod.example.com"
        )
        connector = _saml(acs_url="https://sso.example.com/custom/acs")
        assert auth_router._idp_callback_url(connector) == auth_router._saml_acs_url(connector)


class TestTheRequestIdIsStoredWhereverALoginStarts:
    """Every flow that issues an AuthnRequest must record its id.

    There are two today — the web `/authorize` and the CLI `/cli-sso-redirect` —
    and a third would be easy to add without noticing this. The failure would not
    be subtle (that flow's SAML logins would start failing outright), but it
    would be reported as "SAML is broken" rather than as the one missing line,
    so it is worth naming here.
    """

    @staticmethod
    def _source() -> str:
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/auth.py"
        return path.read_text()

    def test_every_authorization_request_is_stored_with_its_id(self) -> None:
        src = self._source()
        starts = src.count("await connector.build_authorization_request(")
        stored = src.count("saml_request_id=auth_request.request_id")
        assert starts > 0, "the guard is not looking at anything"
        assert stored == starts, (
            f"{starts} flows start an authorization request but only {stored} store the "
            "AuthnRequest id — the ones that do not cannot check InResponseTo"
        )

    def test_no_flow_builds_an_authorization_request_without_the_shared_helper(self) -> None:
        """Otherwise one flow advertises a different ACS URL than we check."""
        src = self._source()
        assert "callback_url = _idp_callback_url(connector)" in src
        assert src.count("callback_url = _idp_callback_url(connector)") == src.count(
            "await connector.build_authorization_request("
        )

    def test_the_acs_endpoint_passes_the_recorded_id_to_the_connector(self) -> None:
        src = self._source()
        assert "expected_request_id=auth_state.saml_request_id" in src, (
            "the ACS endpoint must hand the connector the id this login sent, or "
            "the InResponseTo check has nothing to compare against"
        )
