"""SAML identity provider connector.

Uses python3-saml for metadata parsing and assertion validation.

**Five checks are configurable per provider** — `validate_destination`,
`validate_in_response_to`, `reject_replayed_assertions`, `want_assertions_signed`
and `reject_deprecated_algorithm` — because different identity providers get
different things wrong and relaxing one should never cost you the others.

**All five default to `True` on this line**, which is the 2.0 posture
(GHSA-hgx9-xwfp-5qcr): a deployment that configures nothing is protected, and an
operator whose IDP cannot satisfy one check relaxes that one check rather than
inheriting a permissive default nobody chose. The 1.x release lines default all
five to `False`, because a patch release must not change what a running
deployment does; `docs/upgrading-to-2.0.md` records what to check before
upgrading. Read the default off `SAMLProviderConfig` rather than from here if it
matters to you — a sentence in a docstring is not a gate, which is why
`tests/auth/test_saml.py` pins each one.

The thing worth understanding before touching `_request_data`: python3-saml
decides what the assertion was *addressed to* by reconstructing the current URL
from `http_host` and `script_name`, and compares `Destination` and `Recipient`
against it. Passing an empty `http_host` makes that reconstruction the literal
string ``https://``, which every https URL starts with and contains — so both
checks run, both pass, and an assertion minted for an entirely different service
provider is accepted. The settings must carry the real externally-reachable ACS
URL or the checks are decoration.
"""

import hashlib
import math
import time
from typing import Any
from urllib.parse import urlsplit

from terrapod.auth.idp_groups import roles_from_idp_groups
from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector
from terrapod.config import SAMLProviderConfig
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

#: One key per accepted assertion, so a second presentation of the same one is
#: refused. Redis rather than process memory because the API runs several
#: replicas with no leader: an in-process set would let the same assertion
#: through once per pod, which is not replay protection at all.
ASSERTION_REPLAY_PREFIX = "tp:saml_assertion:"

#: Floor for the replay record's TTL. The natural TTL is whatever is left of the
#: assertion's own validity window, but that can read as nearly zero when our
#: clock runs ahead of the IDP's — and a replica whose clock lags would still
#: accept the assertion after our record had expired. 300s matches the login
#: window (`auth_state.AUTH_STATE_TTL`), which is the longest a login is alive.
REPLAY_TTL_FLOOR_SECONDS = 300

#: Used when the assertion carries no SubjectConfirmationData NotOnOrAfter, so
#: there is no window to derive from. Generous on purpose: an assertion with no
#: stated expiry is the one most worth remembering.
REPLAY_TTL_NO_WINDOW_SECONDS = 3600


def _absolute_parts(url: str) -> tuple[str, str, bool] | None:
    """Split an absolute http(s) URL into (host[:port], path, is_https).

    Returns None for anything that is not absolute — a bare path, an empty
    string, a scheme we do not serve. Callers treat that as "we do not know
    where this deployment lives", which is a refusal rather than a default.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return parts.netloc, parts.path, parts.scheme == "https"


class SAMLConnector(SSOConnector):
    """SAML 2.0 identity provider connector."""

    def __init__(self, config: SAMLProviderConfig) -> None:
        self._config = config
        self._idp_metadata: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def display_name(self) -> str:
        return self._config.display_name or self._config.name

    @property
    def provider_type(self) -> str:
        return "saml"

    @property
    def configured_acs_url(self) -> str:
        """The operator's explicit ACS URL, or "" to derive one from settings."""
        return self._config.acs_url

    def _get_saml_settings(self, acs_url: str) -> dict[str, Any]:
        """Build python3-saml settings dict."""
        return {
            "strict": True,
            "sp": {
                "entityId": self._config.entity_id,
                "assertionConsumerService": {
                    "url": acs_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
            },
            "security": {
                # Without a security block python3-saml picks its own defaults,
                # which require *a* signature somewhere but not one on the
                # assertion, and accept SHA-1. Both are stated here so the
                # posture is a decision rather than whatever the library ships.
                "wantAssertionsSigned": self._config.want_assertions_signed,
                "rejectDeprecatedAlgorithm": self._config.reject_deprecated_algorithm,
            },
            "idp": {},  # Populated from metadata
        }

    def _request_data(self, acs_url: str, post_data: dict[str, Any]) -> dict[str, Any]:
        """The pseudo-request python3-saml reconstructs the current URL from.

        With `validate_destination` on — the default — `http_host` and
        `script_name` come from the real ACS URL, which is what makes the
        Destination and Recipient comparisons mean anything. With it explicitly
        turned off we send the empty host the connector sent before the check
        existed, so an operator who has to relax it lands exactly where the 1.x
        release lines sit rather than somewhere new.
        """
        if not self._config.validate_destination:
            return {
                "https": "on",
                "http_host": "",
                "script_name": "",
                "get_data": {},
                "post_data": post_data,
            }

        parts = _absolute_parts(acs_url)
        if parts is None:
            raise ValueError(
                f"SAML provider {self.name!r} has validate_destination on but no "
                "absolute ACS URL to check assertions against. Set "
                "auth.sso.saml[].acs_url, or auth.callback_base_url / external_url "
                "to this deployment's externally-reachable URL."
            )
        host, path, is_https = parts
        return {
            "https": "on" if is_https else "off",
            "http_host": host,
            "script_name": path,
            "get_data": {},
            "post_data": post_data,
        }

    def _load_idp_metadata(self) -> dict[str, Any]:
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

        if self._idp_metadata is None:
            self._idp_metadata = OneLogin_Saml2_IdPMetadataParser.parse_remote(
                self._config.metadata_url
            )
        return self._idp_metadata

    async def build_authorization_request(
        self,
        callback_url: str,
        state: str,
    ) -> AuthorizationRequest:
        """Build the SAML authorization redirect URL.

        For SAML, this creates an AuthnRequest and returns the IDP's SSO URL
        with the SAMLRequest parameter. The AuthnRequest's id is returned so the
        ACS endpoint can require the assertion to answer *this* request.
        """
        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth
        except ImportError as e:
            raise RuntimeError(
                "python3-saml is required for SAML providers. "
                "Install it with: poetry add python3-saml"
            ) from e

        saml_settings = self._get_saml_settings(callback_url)
        saml_settings.update(self._load_idp_metadata())

        auth = OneLogin_Saml2_Auth(self._request_data(callback_url, {}), saml_settings)
        sso_url = auth.login(return_to=state)

        # The SSO URL contains the SAMLRequest + RelayState
        return AuthorizationRequest(
            authorize_url=sso_url,
            state=state,
            request_id=auth.get_last_request_id(),
        )

    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Handle the SAML assertion callback.

        `expected_request_id` is the id of the AuthnRequest this login started,
        carried in the Redis auth state. It is required when
        `validate_in_response_to` is on and ignored otherwise.
        """
        saml_response = kwargs["saml_response"]
        relay_state = kwargs.get("relay_state", "")
        expected_request_id = kwargs.get("expected_request_id")

        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth
        except ImportError as e:
            raise RuntimeError("python3-saml is required for SAML providers.") from e

        saml_settings = self._get_saml_settings(callback_url)
        saml_settings.update(self._load_idp_metadata())

        request_data = self._request_data(
            callback_url,
            {"SAMLResponse": saml_response, "RelayState": relay_state},
        )

        auth = OneLogin_Saml2_Auth(request_data, saml_settings)
        auth.process_response(
            request_id=expected_request_id if self._config.validate_in_response_to else None
        )

        errors = auth.get_errors()
        if errors:
            # `get_errors()` collapses nearly every validation failure to the
            # single code `invalid_response`, so on its own it cannot tell an
            # operator WHICH check refused the assertion — which defeats the
            # point of having one switch per check, and makes the
            # message-to-cause table in the docs unusable. The library's
            # specific reason (`WRONG_DESTINATION`, `WRONG_AUDIENCE`, a
            # signature complaint) is in `get_last_error_reason()`.
            #
            # It is the library's own message about its own validation, but it
            # interpolates values out of the response, so it is truncated: the
            # response is attacker-supplied and this string reaches a log.
            reason = (auth.get_last_error_reason() or "").strip()[:300]
            detail = f"{errors}: {reason}" if reason else f"{errors}"
            raise ValueError(f"SAML validation failed for {self.name}: {detail}")

        if not auth.is_authenticated():
            raise ValueError(f"SAML authentication failed for {self.name}")

        self._check_in_response_to(auth, expected_request_id)
        await self._claim_assertion(auth)

        attributes = auth.get_attributes()
        name_id = auth.get_nameid()

        # Extract identity from SAML attributes
        email = (
            attributes.get("email", [None])[0]
            or attributes.get(
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress", [None]
            )[0]
            or name_id
            or ""
        )
        display_name = (
            attributes.get("displayName", [None])[0]
            or attributes.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name", [None])[
                0
            ]
        )
        groups = attributes.get("groups", []) or attributes.get(
            "http://schemas.xmlsoap.org/claims/Group", []
        )
        # `role_prefixes` has always been a documented key on SAMLProviderConfig and
        # this connector never read it (GHSA-22vg-4g2w-7w34), which is worse than not
        # supporting it: an operator could set it, have it accepted by the schema,
        # and get no filtering at all. Shared with the OIDC connector so the two
        # cannot drift apart again — that asymmetry WAS the defect.
        groups = roles_from_idp_groups(groups, self._config.role_prefixes, provider=self.name)

        # Build raw claims from all attributes
        raw_claims: dict[str, Any] = {"nameId": name_id}
        raw_claims.update(dict(attributes.items()))

        logger.info(
            "SAML authentication successful",
            provider=self.name,
            subject=name_id,
            email=email,
        )

        return AuthenticatedIdentity(
            provider_name=self.name,
            subject=name_id or "",
            email=email,
            display_name=display_name,
            groups=groups,
            raw_claims=raw_claims,
        )

    def _check_in_response_to(self, auth: Any, expected_request_id: str | None) -> None:
        """Require the assertion to answer the AuthnRequest we actually sent.

        python3-saml compares InResponseTo against `request_id` only when BOTH
        are present, so an assertion carrying no InResponseTo passes its check
        untouched — and an attacker who can relocate a signature need only drop
        the attribute to reach that branch. The presence requirement is ours.
        """
        if not self._config.validate_in_response_to:
            return
        if not expected_request_id:
            raise ValueError(
                f"SAML validation failed for {self.name}: this login has no recorded "
                "AuthnRequest id to match the assertion against"
            )
        in_response_to = auth.get_last_response_in_response_to()
        if not in_response_to:
            raise ValueError(
                f"SAML validation failed for {self.name}: the response carries no "
                "InResponseTo, so it does not answer an authentication request we sent"
            )
        # python3-saml has already rejected a mismatch; this is the belt to that
        # brace, and it is what makes the check hold if the library's own
        # precondition changes under us.
        if in_response_to != expected_request_id:
            raise ValueError(
                f"SAML validation failed for {self.name}: the response answers a "
                "different authentication request"
            )

    def _replay_key(self, assertion_id: str) -> str:
        # The id is hashed rather than embedded: it is an opaque IDP-supplied
        # string of unbounded length, and hashing keeps the key a fixed size and
        # free of anything that could be read as key structure.
        digest = hashlib.sha256(assertion_id.encode("utf-8")).hexdigest()
        return f"{ASSERTION_REPLAY_PREFIX}{self.name}:{digest}"

    def _replay_ttl(self, not_on_or_after: int | float | None) -> int:
        """How long to remember an assertion: the rest of its own window.

        Deliberately uncapped at the top. An assertion stays replayable for as
        long as the IDP said it was valid, so trimming the record to some
        tidier ceiling would quietly reopen the window this exists to close.
        """
        if not not_on_or_after:
            return REPLAY_TTL_NO_WINDOW_SECONDS
        remaining = math.ceil(float(not_on_or_after) - time.time())
        return max(remaining, REPLAY_TTL_FLOOR_SECONDS)

    async def _claim_assertion(self, auth: Any) -> None:
        """Record this assertion id, and refuse it if it is already recorded.

        Runs only after python3-saml has accepted the response, so nothing an
        attacker sends can poison the cache with an id they chose. A claimed
        assertion is spent even if the login fails later, which is the right way
        round: it is one assertion, one login.
        """
        if not self._config.reject_replayed_assertions:
            return

        assertion_id = auth.get_last_assertion_id()
        if not assertion_id:
            raise ValueError(
                f"SAML validation failed for {self.name}: the assertion has no id, so "
                "it cannot be checked for replay"
            )

        ttl = self._replay_ttl(auth.get_last_assertion_not_on_or_after())
        claimed = await get_redis_client().set(self._replay_key(assertion_id), "1", ex=ttl, nx=True)
        if not claimed:
            logger.warning("SAML assertion replayed", provider=self.name, assertion_id=assertion_id)
            raise ValueError(
                f"SAML validation failed for {self.name}: this assertion has already been used"
            )
