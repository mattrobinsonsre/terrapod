"""Where an authorization code may be delivered (GHSA-cq5h-4hqp-c62v).

Two routes start an authorization flow and both store a client-supplied
`redirect_uri` that is later handed the code. Neither validated it, so a crafted
link returned a code minted for the victim. PKCE is no defence -- the attacker
generates both halves of the challenge.

The first attempt at this fix validated only `/oauth/authorize` and left
`/auth/authorize` -- the route the web UI actually uses, and the one that 302s
the code straight at the stored URI with no intermediate page. Hence the guard
lives in `store_auth_state`, and the load-bearing test here is the one asserting
every route goes through it.
"""

import pytest

from terrapod.auth.redirect_uri import (
    LOGIN_PORTS,
    InvalidRedirectURI,
    validate_cli_redirect_uri,
    validate_session_redirect_uri,
)

ORIGIN = "https://terrapod.example.com"


class TestTheCLIFlow:
    def test_the_published_contract_is_what_is_enforced(self):
        # The discovery document advertises this range; if the two drift, we
        # advertise one contract and enforce another.
        assert LOGIN_PORTS == (10000, 10010)

    def test_the_forms_the_cli_really_uses(self):
        lo, hi = LOGIN_PORTS
        for uri in (
            f"http://127.0.0.1:{lo}/login",
            f"http://localhost:{lo}/login",
            f"http://127.0.0.1:{hi}/login",
            f"http://[::1]:{lo}/login",
            f"http://LOCALHOST:{lo}/login",  # host comparison is case-folded
        ):
            validate_cli_redirect_uri(uri)  # must not raise

    def test_the_reported_values_are_refused(self):
        # Three of the four probes from the report. The fourth,
        # http://127.0.0.1:10000/login, is legitimate and asserted accepted
        # above -- a fix that refused it would have broken `terraform login`.
        for uri in ("https://evil.tld/steal", "javascript:alert(1)", "http://localhost:99999/x"):
            with pytest.raises(InvalidRedirectURI):
                validate_cli_redirect_uri(uri)

    def test_other_shapes_that_must_not_get_through(self):
        for uri in (
            "http://127.0.0.1:9999/login",  # below the range
            "http://127.0.0.1:10011/login",  # above it
            "http://127.0.0.1/login",  # no port
            "https://127.0.0.1:10000/login",  # right host, wrong scheme
            "http://127.0.0.1.evil.tld:10000/",  # loopback as a prefix, not the host
            "http://127.1:10000/",  # an alternative spelling that resolves to loopback
            "http://0.0.0.0:10000/",
            "http://2130706433:10000/",  # decimal
            "http://[::ffff:127.0.0.1]:10000/",  # IPv6-mapped
            "http://localhost:10000@evil.tld/",  # loopback smuggled into userinfo
            "http://user:pw@127.0.0.1:10000/",
            "//127.0.0.1:10000/login",  # scheme-relative
            "http://127.0.0.1:10000/cb#frag",  # a fragment strands the code
            "",
        ):
            with pytest.raises(InvalidRedirectURI):
                validate_cli_redirect_uri(uri)


class TestTheWebFlow:
    def test_our_own_origin_is_accepted(self):
        validate_session_redirect_uri(f"{ORIGIN}/auth/callback", allowed_origin=ORIGIN)
        validate_session_redirect_uri(f"{ORIGIN}/auth/callback?x=1", allowed_origin=ORIGIN)

    def test_anywhere_else_is_refused(self):
        for uri in (
            "https://evil.tld/auth/callback",
            "http://terrapod.example.com/auth/callback",  # scheme must match too
            "https://terrapod.example.com.evil.tld/",
            "https://evil.tld@terrapod.example.com/",  # userinfo
            f"{ORIGIN}/auth/callback#frag",
        ):
            with pytest.raises(InvalidRedirectURI):
                validate_session_redirect_uri(uri, allowed_origin=ORIGIN)

    def test_it_fails_closed_with_no_configured_origin(self):
        # An unvalidated redirect is how the bug worked; absent configuration
        # must not mean "allow anything".
        with pytest.raises(InvalidRedirectURI):
            validate_session_redirect_uri(f"{ORIGIN}/auth/callback", allowed_origin="")

    def test_a_loopback_uri_is_not_a_web_redirect(self):
        # The two allow-lists are not interchangeable: laundering a CLI URI into
        # a session flow (or the reverse) must not pass.
        with pytest.raises(InvalidRedirectURI):
            validate_session_redirect_uri("http://127.0.0.1:10000/login", allowed_origin=ORIGIN)


class TestTheGuardCannotBeBypassed:
    """The regression guard for how this bug happened.

    `cq5h` was not one missing check, it was two routes and one of them
    remembered. A per-route test would have passed on the route someone thought
    of. These assert the structural property instead: the validation is in
    `store_auth_state`, and no route constructs an `AuthState` without going
    through it.
    """

    async def test_store_auth_state_refuses_and_stores_nothing(self):
        """Behavioural, not source-matching.

        An earlier version of this test asserted `"validate_redirect_uri" in
        inspect.getsource(store_auth_state)` and passed with the call deleted,
        because the function imports that name. A guard that survives the bug it
        guards against is worse than none.
        """
        from unittest.mock import AsyncMock, patch

        from terrapod.auth.auth_state import AuthState, store_auth_state

        state = AuthState(
            provider_name="pending",
            client_redirect_uri="https://evil.tld/steal",
            client_state="probe",
            code_challenge="c",
            code_challenge_method="S256",
            idp_state="i",
            credential_type="api_token",
        )
        redis = AsyncMock()
        with patch("terrapod.auth.auth_state.get_redis_client", return_value=redis):
            with pytest.raises(InvalidRedirectURI):
                await store_auth_state(state)
        redis.set.assert_not_called()

    async def test_store_auth_state_accepts_a_legitimate_one(self):
        from unittest.mock import AsyncMock, patch

        from terrapod.auth.auth_state import AuthState, store_auth_state

        state = AuthState(
            provider_name="pending",
            client_redirect_uri=f"http://127.0.0.1:{LOGIN_PORTS[0]}/login",
            client_state="s",
            code_challenge="c",
            code_challenge_method="S256",
            idp_state="i",
            credential_type="api_token",
        )
        redis = AsyncMock()
        with patch("terrapod.auth.auth_state.get_redis_client", return_value=redis):
            await store_auth_state(state)
        redis.set.assert_called_once()

    def test_every_route_storing_an_authstate_goes_through_it(self):
        """No route may persist an AuthState by another path."""
        import pathlib
        import re

        routers = pathlib.Path(__file__).resolve().parents[2] / "terrapod" / "api" / "routers"
        offenders = []
        for path in routers.glob("*.py"):
            src = path.read_text()
            if "AuthState(" not in src:
                continue
            # Every construction must be followed by a store_auth_state call in
            # the same file; anything else is a new way to persist an unchecked
            # redirect_uri.
            constructions = len(re.findall(r"\bAuthState\(", src))
            stores = len(re.findall(r"\bstore_auth_state\(", src))
            if stores < constructions:
                offenders.append(f"{path.name}: {constructions} AuthState(...) vs {stores} stores")
        assert not offenders, (
            "an AuthState is constructed without being stored through the "
            f"validating helper: {offenders}"
        )
