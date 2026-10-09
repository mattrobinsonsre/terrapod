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
            patch.object(oidc_signing, "_keys", [MagicMock(kid="k1")]),
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
            patch.object(oidc_signing, "_keys", [MagicMock(kid="k1")]),
            patch.object(oidc_signing, "get_jwks", lambda: {"keys": []}),
            patch("terrapod.config.settings", _issuer_settings()),
        ):
            assert (await router.jwks()).status_code == 200
            assert (await router.openid_configuration()).status_code == 200


def _issuer_settings():
    s = MagicMock()
    s.auth.oidc_issuer.public_url = "https://terrapod.example.com"
    s.auth.oidc_issuer.key_propagation_seconds = 600
    s.public_webhook_url = ""
    s.external_url = ""
    return s
