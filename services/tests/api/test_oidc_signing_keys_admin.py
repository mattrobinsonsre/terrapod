"""The two admin signing-key endpoints (#1901), through the mounted app.

    GET  /api/terrapod/v1/oidc/signing-keys
    POST /api/terrapod/v1/oidc/signing-keys/actions/rotate

Neither had a test. The generated attribute snapshot covers them, but it pins
response attribute *names* — it cannot see who is allowed to call them, nor
which status code an exception comes out as.

**The authorisation half is the one with teeth.** `rotate` is a write against a
*published* trust root: it retires the key that is signing and publishes a new
one that does not sign until the propagation window has passed. A caller who can
force rotations can therefore make every federated run fail at the cloud's token
exchange until the clouds re-fetch the JWKS — a denial of service with no
Terrapod-side symptom at all. `require_admin` is the only thing standing there
and it is a `Depends`, so a test that overrides it (as most of this suite's
admin tests do, to reach the handler) proves nothing about it. These override
`get_current_user` instead, so the real `require_admin` runs.

**The status-code half is the quiet one.** `rotate_signing_key` raises
`ValueError` for a BYO-key deployment and `RuntimeError` when no key is loaded,
and the route maps both to 409. Both are tested at the service layer, so the
mapping itself is unpinned: deleting the `except ValueError` clause turns an
operator's "this deployment signs with your own key, replace the secret" into a
500 with no explanation, and nothing fails.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_LIST = "/api/terrapod/v1/oidc/signing-keys"
_ROTATE = "/api/terrapod/v1/oidc/signing-keys/actions/rotate"


def _user(*, roles):
    return AuthenticatedUser(
        email="someone@terrapod.test",
        display_name="Someone",
        roles=roles,
        provider_name="local",
        auth_method="session",
    )


def _row(kid, *, created=None, activates=None, retired=None):
    """A stand-in for an `OIDCSigningKey` row, shaped as the serializer reads it."""
    now = created or datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        kid=kid,
        private_key_pem="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
        created_at=now,
        activates_at=activates if activates is not None else now,
        retired_at=retired,
    )


def _list_result(rows):
    scalars = MagicMock()
    scalars.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _one_result(row):
    result = MagicMock()
    result.scalar_one.return_value = row
    return result


def _app(user, *, execute_result=None):
    app = create_app()
    # `get_current_user`, NOT `require_admin` — the whole point is that the real
    # `require_admin` runs against this user.
    app.dependency_overrides[get_current_user] = lambda: user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result or _list_result([]))
    db.commit = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.get(path, headers=_AUTH)


async def _post(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.post(path, headers=_AUTH)


class TestOnlyAPlatformAdminMayTouchTheSigningKeys:
    """Both endpoints, both directions.

    A forced rotation is a denial of service against every federated run in the
    deployment until the clouds re-fetch the JWKS, and a published key set is
    deployment-wide infrastructure rather than anything a workspace owns — so
    there is no tier below platform admin that should reach either.
    """

    @pytest.mark.parametrize("roles", [["everyone"], ["audit"], ["everyone", "audit"]])
    async def test_a_non_admin_cannot_list_the_signing_keys(self, roles):
        """`audit` is in the parametrisation deliberately: it reads most things,
        and the private half never leaves the API, so this is the plausible
        place for someone to widen the gate to `require_admin_or_audit`. That
        would also hand it the rotate endpoint if the two were ever changed
        together, so pin the narrower gate on both."""
        app, db = _app(_user(roles=roles))
        resp = await _get(app, _LIST)
        assert resp.status_code == 403, resp.text
        db.execute.assert_not_awaited()

    @pytest.mark.parametrize("roles", [["everyone"], ["audit"]])
    async def test_a_non_admin_cannot_rotate(self, roles):
        app, db = _app(_user(roles=roles))
        with patch(
            "terrapod.auth.oidc_signing.rotate_signing_key", new_callable=AsyncMock
        ) as rotate:
            resp = await _post(app, _ROTATE)
        assert resp.status_code == 403, resp.text
        rotate.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_an_admin_can_list(self):
        """The positive case, so the two above are not passing for some
        unrelated reason (a 403 from an unmounted route would look identical)."""
        app, _db = _app(_user(roles=["admin"]), execute_result=_list_result([_row("KID-A")]))
        with patch(
            "terrapod.auth.oidc_signing.get_signing_key",
            return_value=SimpleNamespace(kid="KID-A"),
        ):
            resp = await _get(app, _LIST)
        assert resp.status_code == 200, resp.text


class TestListingWhatIsPublished:
    async def _list(self, rows, *, signing_kid):
        app, _db = _app(_user(roles=["admin"]), execute_result=_list_result(rows))
        if signing_kid is None:
            ctx = patch(
                "terrapod.auth.oidc_signing.get_signing_key",
                side_effect=RuntimeError("OIDC issuer signing key not initialised"),
            )
        else:
            ctx = patch(
                "terrapod.auth.oidc_signing.get_signing_key",
                return_value=SimpleNamespace(kid=signing_kid),
            )
        with ctx:
            resp = await _get(app, _LIST)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_the_signing_key_is_flagged_and_the_others_are_not(self):
        now = datetime.now(UTC)
        body = await self._list(
            [
                _row("OLD", created=now - timedelta(days=30), retired=now),
                _row("NEW", created=now, activates=now + timedelta(seconds=600)),
            ],
            signing_kid="OLD",
        )
        flags = {k["id"]: k["attributes"]["signing"] for k in body["data"]}
        assert flags == {"OLD": True, "NEW": False}
        assert body["meta"]["signing-kid"] == "OLD"

    async def test_nothing_is_flagged_when_no_key_is_loaded(self):
        """The fallback, which exists because `get_signing_key` raises before
        `init_oidc_signing` has run — and the lifespan only WARNS when that
        fails, so a pod really can serve this route with nothing loaded.

        A 500 here would be the worst possible answer: an operator reaching for
        this page is doing so precisely because federation is not working, and
        the page is what tells them whether a key exists at all.
        """
        body = await self._list([_row("ONLY")], signing_kid=None)
        assert body["meta"]["signing-kid"] is None
        assert [k["attributes"]["signing"] for k in body["data"]] == [False]
        # The row itself still comes back — the question "is there a key in the
        # table?" is the one the operator has.
        assert [k["id"] for k in body["data"]] == ["ONLY"]

    async def test_no_private_key_material_is_returned(self):
        """The rows carry the PEM; the serializer must not. Public key material
        only, as the docstring says — and `kid` is an RFC 7638 thumbprint, so it
        is the same value a cloud sees in a token header."""
        app, _db = _app(_user(roles=["admin"]), execute_result=_list_result([_row("KID-A")]))
        with patch(
            "terrapod.auth.oidc_signing.get_signing_key",
            return_value=SimpleNamespace(kid="KID-A"),
        ):
            resp = await _get(app, _LIST)
        assert "PRIVATE KEY" not in resp.text
        assert "private" not in resp.text

    async def test_timestamps_are_rfc3339_with_a_trailing_z(self):
        """Architecture rule 10, and the attribute snapshot cannot see the
        VALUE's shape — only that the key is present."""
        now = datetime.now(UTC)
        body = await self._list([_row("KID-A", created=now, retired=now)], signing_kid="KID-A")
        attrs = body["data"][0]["attributes"]
        for field in ("created-at", "activates-at", "retired-at"):
            assert attrs[field].endswith("Z"), f"{field} is {attrs[field]!r}"
            assert "+00:00" not in attrs[field]


class TestRotating:
    async def _rotate(self, *, rotate_side_effect=None, new_row=None):
        row = new_row or _row("NEW-KID", activates=datetime.now(UTC) + timedelta(seconds=600))
        app, db = _app(_user(roles=["admin"]), execute_result=_one_result(row))
        kw = (
            {"side_effect": rotate_side_effect}
            if rotate_side_effect is not None
            else {"return_value": SimpleNamespace(kid=row.kid, row_id=row.id)}
        )
        with patch("terrapod.auth.oidc_signing.rotate_signing_key", new_callable=AsyncMock, **kw):
            resp = await _post(app, _ROTATE)
        return resp, row

    async def test_a_rotation_returns_201_with_the_new_key_not_yet_signing(self):
        """The design, read off the response: published immediately, signing
        only after the propagation window. A response saying `signing: true`
        would tell an operator the handover had already happened, which is the
        state the window exists to avoid.
        """
        resp, row = await self._rotate()
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["kid"] == row.kid
        assert attrs["signing"] is False
        assert attrs["retired-at"] is None
        assert datetime.fromisoformat(attrs["activates-at"]) > datetime.now(UTC)
        # The note is what tells an operator the key is not live yet, so it has
        # to name the field they should look at.
        assert "activates-at" in resp.json()["meta"]["note"]

    async def test_a_byo_key_deployment_is_refused_with_409_not_500(self):
        """`rotate_signing_key` raises ValueError when
        `auth.oidc_issuer.signing_key_pem` is set: the key is the operator's and
        so is rotating it. The service-layer test pins the raise; this pins that
        the operator is told, in the service's own words, rather than getting an
        unexplained server error."""
        resp, _row = await self._rotate(
            rotate_side_effect=ValueError(
                "This deployment signs with an operator-supplied key "
                "(api.config.auth.oidc_issuer.signing_key_pem), so Terrapod does not rotate it."
            )
        )
        assert resp.status_code == 409, resp.text
        assert "operator-supplied key" in resp.json()["detail"]

    async def test_a_runtime_error_is_also_409_and_carries_its_reason(self):
        """Raised when there is no key to retire — nothing initialised, or every
        key retired past its grace. Same shape of answer: the caller is entitled
        to the request, the deployment is not in a state to serve it."""
        resp, _row = await self._rotate(
            rotate_side_effect=RuntimeError(
                "OIDC issuer signing key not initialised — init_oidc_signing() "
                "runs in the app lifespan."
            )
        )
        assert resp.status_code == 409, resp.text
        assert "not initialised" in resp.json()["detail"]

    async def test_no_private_key_material_is_returned_on_a_rotation_either(self):
        """The one response that has just handled a brand-new private key."""
        resp, _row = await self._rotate()
        assert "PRIVATE KEY" not in resp.text
        assert "not-a-real-key" not in resp.text
