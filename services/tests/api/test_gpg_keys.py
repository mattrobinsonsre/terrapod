"""Tests for the GPG-key management router (provider signing keys).

Covers create (happy + invalid-armor 422), list, show (happy + bad-uuid
404 + not-found 404), delete (happy 204 + not-found 404), and the
unauthenticated 401 gate.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_KEYS = "/api/terrapod/v1/gpg-keys"


def _user(roles=None):
    return AuthenticatedUser(
        email="user@example.com",
        display_name="User",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _admin():
    """A principal that holds the registry capability the mutating routes need.

    Platform admin is step 1 of the capability resolver, so this exercises the
    real resolution path rather than patching it out — the point of these
    tests after the fix is that the gate is genuinely consulted.
    """
    return _user(roles=["admin"])


def _make_app(user=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


def _mock_key(key_id="1C11AB2FF1189D6C"):
    k = MagicMock()
    k.id = uuid.uuid4()
    k.key_id = key_id
    k.ascii_armor = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END..."
    k.source = "terrapod"
    k.source_url = None
    k.created_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    k.updated_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    return k


def _create_body():
    return {
        "data": {
            "type": "gpg-keys",
            "attributes": {
                "namespace": "default",
                "ascii-armor": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END...",
            },
        }
    }


class TestAuth:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_no_auth_401(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS)
        assert resp.status_code == 401


class TestCreate:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    async def test_happy_path_201(self, mock_create, *mocks):
        mock_create.return_value = _mock_key()
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["type"] == "gpg-keys"
        assert body["data"]["attributes"]["key-id"] == "1C11AB2FF1189D6C"
        assert body["data"]["attributes"]["namespace"] == "default"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    async def test_invalid_armor_422(self, mock_create, *mocks):
        mock_create.side_effect = ValueError("no PGP block found")
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_missing_armor_422(self, *mocks):
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                _KEYS,
                json={"data": {"type": "gpg-keys", "attributes": {"namespace": "default"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


class TestList:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.list_gpg_keys", new_callable=AsyncMock)
    async def test_list_returns_keys(self, mock_list, *mocks):
        mock_list.return_value = [_mock_key(), _mock_key(key_id="AAAA1111BBBB2222")]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS, headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2


class TestShow:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.get_gpg_key", new_callable=AsyncMock)
    async def test_show_happy(self, mock_get, *mocks):
        key = _mock_key()
        mock_get.return_value = key
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/{key.id}", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["key-id"] == "1C11AB2FF1189D6C"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_bad_uuid_404(self, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/not-a-uuid", headers=_AUTH)
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.get_gpg_key", new_callable=AsyncMock)
    async def test_show_not_found_404(self, mock_get, *mocks):
        mock_get.return_value = None
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404


def _revoke_body():
    return {
        "data": {
            "type": "gpg-key-revocations",
            "attributes": {
                "revocation-certificate": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END...",
            },
        }
    }


class TestRevoke:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.revoke_gpg_key", new_callable=AsyncMock)
    async def test_revoke_happy_200(self, mock_revoke, *mocks):
        key = _mock_key()
        mock_revoke.return_value = key
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(f"{_KEYS}/{key.id}/revoke", json=_revoke_body(), headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["key-id"] == "1C11AB2FF1189D6C"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.revoke_gpg_key", new_callable=AsyncMock)
    async def test_revoke_invalid_cert_422(self, mock_revoke, *mocks):
        mock_revoke.side_effect = ValueError("not a valid self-revocation certificate for this key")
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"{_KEYS}/{uuid.uuid4()}/revoke", json=_revoke_body(), headers=_AUTH
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.revoke_gpg_key", new_callable=AsyncMock)
    async def test_revoke_not_found_404(self, mock_revoke, *mocks):
        mock_revoke.return_value = None
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"{_KEYS}/{uuid.uuid4()}/revoke", json=_revoke_body(), headers=_AUTH
            )
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_revoke_bad_uuid_404(self, *mocks):
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(f"{_KEYS}/xyz/revoke", json=_revoke_body(), headers=_AUTH)
        assert resp.status_code == 404


class TestDelete:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.delete_gpg_key", new_callable=AsyncMock)
    async def test_delete_happy_204(self, mock_delete, *mocks):
        mock_delete.return_value = True
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.delete_gpg_key", new_callable=AsyncMock)
    async def test_delete_not_found_404(self, mock_delete, *mocks):
        mock_delete.return_value = False
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_bad_uuid_404(self, *mocks):
        app = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/xyz", headers=_AUTH)
        assert resp.status_code == 404


class TestTrustAnchorAuthorization:
    """The GPG key store is the trust anchor for provider signature
    verification: `_verify_and_store_shasums_signature` reads the issuer key id
    out of a publisher's detached SHA256SUMS.sig and verifies it against
    whichever registered key matches. Every route here was previously gated on
    `get_current_user` alone, so any authenticated principal could register a
    key of their own and have Terrapod accept signatures they made, or delete a
    legitimate publisher's key and break verification for them.

    These pin the gate per route, because a check added to two of three
    mutating endpoints is the same hole with a smaller entrance.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    async def test_ordinary_principal_cannot_add_a_trust_anchor(self, mock_create, *mocks):
        mock_create.return_value = _mock_key()
        app = _make_app(_user())  # authenticated, holds only `everyone`
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)
        assert resp.status_code == 403
        # The service must not have been reached — a 403 raised after the write
        # would be a different bug wearing the same status code.
        mock_create.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.revoke_gpg_key", new_callable=AsyncMock)
    async def test_ordinary_principal_cannot_revoke(self, mock_revoke, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"{_KEYS}/{uuid.uuid4()}/revoke", json=_revoke_body(), headers=_AUTH
            )
        # A valid body on purpose: with an invalid one FastAPI answers 422 from
        # request validation before the authz check runs, which would prove
        # nothing about the gate.
        assert resp.status_code == 403
        mock_revoke.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.delete_gpg_key", new_callable=AsyncMock)
    async def test_ordinary_principal_cannot_delete(self, mock_delete, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 403
        mock_delete.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.list_gpg_keys", new_callable=AsyncMock)
    async def test_reads_stay_open_to_any_authenticated_principal(self, mock_list, *mocks):
        """Deliberately NOT gated. These are public keys, and this ships in a
        patch to supported lines — tightening a read buys no security and can
        break a consumer that was legitimately listing them."""
        mock_list.return_value = [_mock_key()]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS, headers=_AUTH)
        assert resp.status_code == 200


class TestNonPlatformAdminGrant:
    """The allow path for a role-granted registry admin.

    Every existing test here uses platform admin, which is step 1 of the
    capability resolver — so nothing proved a *role*-granted registry admin
    actually resolves through the empty resource this gate passes
    (`resolve_registry_capabilities_for(db, user, "", {}, "")`) (#1297).

    The empty resource is deliberate: the GPG store is a single trust anchor
    with no name of its own, so only unscoped grants can match. A role's
    `allow_labels` cannot — `matches_labels` requires the key to be present on
    the resource — and an earlier version that passed a literal "gpg-keys"
    name put a magic string in the same namespace as real provider names. So
    "does an ordinary registry-admin role actually work here" is a real
    question, not a formality.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    @patch("terrapod.api.routers.gpg_keys.resolve_registry_capabilities_for")
    async def test_a_role_granted_registry_admin_may_create(
        self, mock_resolve, mock_create, *mocks
    ):
        from terrapod.auth.capabilities import _REGISTRY_LEVELS

        mock_resolve.return_value = _REGISTRY_LEVELS["admin"]
        mock_create.return_value = _mock_key()

        app = _make_app(_user(roles=["registry-owner"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)

        assert resp.status_code == 201, resp.text
        # The gate is consulted against the unscoped store — no name, no
        # labels, no owner — which is what makes a label-scoped role unable to
        # reach it and a plain registry-admin role able to.
        args = mock_resolve.await_args.args
        assert args[2] == "" and args[3] == {} and args[4] == ""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.resolve_registry_capabilities_for")
    async def test_registry_read_is_not_enough_to_register_a_key(self, mock_resolve, *mocks):
        """Registering a signing key is adding a trust anchor: every provider
        signed by it is then installable. Read is not that."""
        from terrapod.auth.capabilities import _REGISTRY_LEVELS

        mock_resolve.return_value = _REGISTRY_LEVELS["read"]

        app = _make_app(_user(roles=["registry-reader"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)

        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.list_gpg_keys", new_callable=AsyncMock)
    @patch("terrapod.api.routers.gpg_keys.resolve_registry_capabilities_for")
    async def test_registry_read_can_list(self, mock_resolve, mock_list, *mocks):
        """Public keys are public by nature — reading them is not the
        privileged act, registering one is."""
        from terrapod.auth.capabilities import _REGISTRY_LEVELS

        mock_resolve.return_value = _REGISTRY_LEVELS["read"]
        mock_list.return_value = [_mock_key()]

        app = _make_app(_user(roles=["registry-reader"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS, headers=_AUTH)

        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1
