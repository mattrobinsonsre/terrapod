"""Recovery when the issuer's signing set fails to load, and what it serves meanwhile.

The defect these pin: `init_oidc_signing` is the only path that GENERATES a key,
the periodic task only ever called `reload_signing_keys`, and the lifespan
catches an init failure and merely warns. So a pod that lost the startup race --
a transient database error, or starting before the migration Job finishes -- had
nothing that would ever make it succeed. `_keys` stayed None for ever, the JWKS
raised into the catch-all handler as a 500 on an unauthenticated public
endpoint, the discovery document happily returned 200 advertising a `jwks_uri`
that failed, and the pod reported Ready throughout. Only a manual restart fixed
it, and the lifespan's own warning said the opposite -- "the issuer routes will
refuse until it succeeds" -- which nothing made true.

Deliberately NOT in `test_oidc_issuer.py`: that file is being extended in
parallel for the mounted-app coverage gap, and two branches editing one file is
how a merge silently drops a test.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers import oidc_issuer as router
from terrapod.auth import oidc_signing


@contextlib.contextmanager
def _session():
    """Stand in for the session the cycle opens for itself.

    Patched at the source module, not on `oidc_signing`: the import is lazy
    inside the function, so it resolves from `terrapod.db.session` at call time
    and patching the attribute here would patch nothing -- the test would
    silently drive the real database.
    """

    @contextlib.asynccontextmanager
    async def _fake():
        yield MagicMock()

    with patch("terrapod.db.session.get_db_session", _fake):
        yield


class TestTheRefreshCycleRecoversAFailedStartup:
    """The blocker: the periodic task has to be able to CREATE a key, not only re-read one."""

    async def test_it_initialises_when_this_process_holds_no_set(self):
        """The recovery path. `reload` raises on an empty table for ever, so a
        task that only reloads can never undo a failed startup."""
        init = AsyncMock(return_value=[])
        reload_ = AsyncMock()
        with (
            patch.object(oidc_signing, "_keys", None),
            patch.object(oidc_signing, "init_oidc_signing", init),
            patch.object(oidc_signing, "reload_signing_keys", reload_),
            _session(),
        ):
            await oidc_signing.refresh_signing_keys_cycle()

        init.assert_awaited_once()
        reload_.assert_not_awaited()

    async def test_it_reloads_once_a_set_is_loaded(self):
        """The steady state keeps `reload`: a lighter read, and it deliberately
        does not assign on failure, so a database blip leaves a working cache
        intact rather than tearing it down."""
        init = AsyncMock()
        reload_ = AsyncMock()
        with (
            patch.object(
                oidc_signing,
                "_keys",
                # A real `SigningKey`, not a MagicMock: `_live_keys` compares
                # `retired_at` against a datetime on every read, and a
                # MagicMock attribute raises there rather than standing in
                # for "never retired".
                [oidc_signing.SigningKey(kid="k1", private_key_pem="")],
            ),
            patch.object(oidc_signing, "init_oidc_signing", init),
            patch.object(oidc_signing, "reload_signing_keys", reload_),
            _session(),
        ):
            await oidc_signing.refresh_signing_keys_cycle()

        reload_.assert_awaited_once()
        init.assert_not_awaited()


class TestBothDocumentsRefuseWithout503RatherThan500:
    """503, not 500 -- and the discovery document refuses too.

    A 500 tells a cloud and an operator that something is broken rather than
    starting, and carries no retry hint. The discovery half is the less obvious
    one: it touches no key material, so it would serve 200 while the `jwks_uri`
    it names failed, and a cloud would cache that unresolvable trust root for
    the document's full max-age even after the key arrived.
    """

    async def test_the_jwks_refuses(self):
        with patch.object(oidc_signing, "_keys", None), pytest.raises(HTTPException) as e:
            await router.jwks()
        assert e.value.status_code == 503
        assert e.value.headers["Retry-After"] == "30"

    async def test_the_discovery_document_refuses_too(self):
        with patch.object(oidc_signing, "_keys", None), pytest.raises(HTTPException) as e:
            await router.openid_configuration()
        assert e.value.status_code == 503
        assert e.value.headers["Retry-After"] == "30"

    async def test_a_loaded_set_serves_both(self):
        """The negative path: this must not have turned the documents off for everyone."""
        with (
            patch.object(
                oidc_signing,
                "_keys",
                # A real `SigningKey`, not a MagicMock: `_live_keys` compares
                # `retired_at` against a datetime on every read, and a
                # MagicMock attribute raises there rather than standing in
                # for "never retired".
                [oidc_signing.SigningKey(kid="k1", private_key_pem="")],
            ),
            patch.object(oidc_signing, "get_jwks", lambda: {"keys": []}),
            patch("terrapod.config.settings", _issuer_settings()),
        ):
            assert (await router.jwks()).status_code == 200
            assert (await router.openid_configuration()).status_code == 200


def _issuer_settings():
    s = MagicMock()
    s.auth.oidc_issuer.public_url = "https://terrapod.example.com"
    s.auth.oidc_issuer.key_propagation_seconds = 600
    # A real int: `_live_keys` builds a timedelta from it on every read, so the
    # route's loaded-check raises TypeError on a MagicMock -- a different failure
    # wearing the shape of the one these tests are about.
    s.auth.oidc_issuer.retired_key_grace_seconds = 3600
    s.public_webhook_url = ""
    s.external_url = ""
    return s


class TestAnUnusableOperatorSuppliedKeyFailsSTARTUP:
    """The one failure the tolerant handler above must NOT forgive.

    Everything else this file is about is transient: a database that is not
    ready, a lost startup race, a blip. The lifespan warns and the refresh cycle
    recovers the process, which is right. An operator-supplied key that cannot
    be loaded is the opposite -- it comes from a Secret, so no retry can change
    the answer, and forgiving it leaves the pod Ready for ever while both issuer
    routes answer 503 and the only evidence is one line in the startup log.
    """

    def _pem(self, *, bits=2048, exponent=65537):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        return (
            rsa.generate_private_key(public_exponent=exponent, key_size=bits)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )

    def test_no_supplied_key_is_a_no_op(self):
        """The overwhelmingly common case: Terrapod generates its own."""
        with patch.object(oidc_signing, "_configured_key_pem", return_value=None):
            oidc_signing.validate_configured_signing_key()

    def test_a_good_supplied_key_passes(self):
        with patch.object(oidc_signing, "_configured_key_pem", return_value=self._pem()):
            oidc_signing.validate_configured_signing_key()

    def test_a_key_under_2048_bits_raises(self):
        with patch.object(oidc_signing, "_configured_key_pem", return_value=self._pem(bits=1024)):
            with pytest.raises(ValueError, match="at least 2048 bits"):
                oidc_signing.validate_configured_signing_key()

    def test_an_unreadable_pem_raises(self):
        with patch.object(
            oidc_signing,
            "_configured_key_pem",
            return_value="-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n",
        ):
            with pytest.raises(ValueError, match="signing_key_pem is not usable"):
                oidc_signing.validate_configured_signing_key()

    def test_the_message_names_the_setting_AND_the_reason(self):
        """Two strings, answering different questions: which Secret to go and
        fix, and what is wrong with what is in it. The library's own error says
        only the second, and Pydantic is not in this path to supply the first."""
        with patch.object(oidc_signing, "_configured_key_pem", return_value=self._pem(exponent=3)):
            with pytest.raises(ValueError) as exc:
                oidc_signing.validate_configured_signing_key()
        text = str(exc.value)
        assert "auth.oidc_issuer.signing_key_pem" in text
        assert "api.oidcSigningKey.existingSecret" in text
        assert "public exponent 65537" in text

    def test_the_supplied_key_check_is_outside_the_try(self):
        """A positional gate, because no behavioural test can see this.

        The helper raising is necessary and not sufficient: moved one line down,
        into the `try` that wraps `init_oidc_signing`, it raises exactly as it
        does now and the `except Exception` swallows it into a warning. The pod
        starts, and the fix is gone with every test still green -- the same shape
        as a `db.flush()` that drifts above its handler.
        """
        import ast
        import inspect
        import textwrap

        from terrapod.api import app as app_module

        src = textwrap.dedent(inspect.getsource(app_module.lifespan))
        tree = ast.parse(src)

        def _calls(node):
            return {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }

        inside_a_try = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    inside_a_try |= _calls(stmt)

        assert "validate_configured_signing_key" in _calls(tree), (
            "the lifespan no longer validates the operator-supplied key at all"
        )
        assert "validate_configured_signing_key" not in inside_a_try, (
            "the supplied-key check sits inside a try in the lifespan, so its "
            "refusal is caught and downgraded to a warning and the pod starts "
            "with an unusable signing key"
        )
        assert "init_oidc_signing" in inside_a_try, (
            "init_oidc_signing is no longer inside a try, so a pod that starts "
            "before the migration Job finishes now crash-loops -- the tolerance "
            "this gate is contrasted against has been removed"
        )
