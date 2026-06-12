"""Services-API tests for the service-token endpoints (#495).

Covers create (kind gating), admin kind-filter, rotate, expiring, revoke-all,
re-tag, and the HARD INVARIANT that no token path is mounted under /api/v2.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser


def _make_app(user: AuthenticatedUser):
    app = create_app()
    from terrapod.api.dependencies import get_current_user
    from terrapod.db.session import get_db

    async def override_auth():
        return user

    async def override_db():
        return AsyncMock()

    app.dependency_overrides[get_current_user] = override_auth
    app.dependency_overrides[get_db] = override_db
    return app


def _user(email="dev@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Dev",
        roles=roles or [],
        provider_name="local",
        auth_method="session",
    )


def _token_mock(**kw):
    t = MagicMock()
    t.id = kw.get("id", "at-svc")
    t.description = kw.get("description", "")
    t.kind = kw.get("kind", "service_bound")
    t.bound_to = kw.get("bound_to", "dev@example.com")
    t.created_by = kw.get("created_by", "dev@example.com")
    t.token_type = "user"
    t.pinned_roles = kw.get("pinned_roles")
    t.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    t.rotated_at = kw.get("rotated_at")
    t.last_used_at = None
    t.lifespan_hours = None
    return t


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_APP_PATCHES = (
    patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
    patch("terrapod.api.app.init_redis"),
    patch("terrapod.api.app.init_db"),
)


def _app_patched(fn):
    for p in _APP_PATCHES:
        fn = p(fn)
    return fn


@_app_patched
@patch("terrapod.api.routers.tokens.create_api_token")
async def test_create_service_bound(mock_create, *_):
    mock_create.return_value = (
        _token_mock(kind="service_bound", pinned_roles=["plan-only"]),
        "raw.tpod.secret",
    )
    app = _make_app(_user())
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/users/dev/authentication-tokens",
            json={"data": {"attributes": {"kind": "service_bound", "pinned_roles": ["plan-only"]}}},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 201
    attrs = r.json()["data"]["attributes"]
    assert attrs["kind"] == "service_bound"
    assert attrs["token"] == "raw.tpod.secret"
    assert attrs["bound-to"] == "dev@example.com"


@_app_patched
async def test_create_detached_gated_422(*_):
    app = _make_app(_user(roles=["admin"]))
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/users/dev/authentication-tokens",
            json={"data": {"attributes": {"kind": "service_detached"}}},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 422


@_app_patched
@patch("terrapod.api.routers.tokens.list_all_tokens")
async def test_admin_list_kind_filter(mock_list, *_):
    mock_list.return_value = [_token_mock(kind="service_bound")]
    app = _make_app(_user(roles=["admin"]))
    async with _client(app) as c:
        r = await c.get(
            "/api/terrapod/v1/admin/authentication-tokens?kind=service_bound",
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    # the kind filter is passed through to the query
    assert mock_list.call_args.kwargs.get("kind") == "service_bound"


@_app_patched
async def test_admin_list_detached_filter_is_valid_not_422(*_):
    # service_detached is a valid kind: filtering by it returns empty, not 422
    app = _make_app(_user(roles=["admin"]))
    with patch("terrapod.api.routers.tokens.list_all_tokens", return_value=[]):
        async with _client(app) as c:
            r = await c.get(
                "/api/terrapod/v1/admin/authentication-tokens?kind=service_detached",
                headers={"Authorization": "Bearer x"},
            )
    assert r.status_code == 200
    assert r.json()["data"] == []


@_app_patched
async def test_admin_list_non_admin_forbidden(*_):
    app = _make_app(_user())  # no admin role
    async with _client(app) as c:
        r = await c.get(
            "/api/terrapod/v1/admin/authentication-tokens",
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 403


@_app_patched
@patch("terrapod.api.routers.tokens.rotate_token")
@patch("terrapod.api.routers.tokens.get_token_by_id")
async def test_rotate_returns_new_value(mock_get, mock_rotate, *_):
    tok = _token_mock(bound_to="dev@example.com", rotated_at=datetime(2026, 6, 1, tzinfo=UTC))
    mock_get.return_value = tok
    mock_rotate.return_value = (tok, "rotated.tpod.secret")
    app = _make_app(_user())
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/authentication-tokens/at-svc/actions/rotate",
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    assert r.json()["data"]["attributes"]["token"] == "rotated.tpod.secret"


@_app_patched
@patch("terrapod.api.routers.tokens.get_token_by_id")
async def test_rotate_non_owner_forbidden(mock_get, *_):
    mock_get.return_value = _token_mock(bound_to="someone-else@example.com")
    app = _make_app(_user())  # not owner, not admin
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/authentication-tokens/at-svc/actions/rotate",
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 403


@_app_patched
@patch("terrapod.api.routers.tokens.get_redis_client")
@patch("terrapod.api.routers.tokens.revoke_all_for_user")
async def test_revoke_all_admin(mock_revoke_all, mock_redis, *_):
    mock_revoke_all.return_value = 4
    mock_redis.return_value = MagicMock(delete=AsyncMock())
    app = _make_app(_user(roles=["admin"]))
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/admin/authentication-tokens/actions/revoke-all",
            json={"email": "leaver@example.com"},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    assert r.json()["data"] == {"email": "leaver@example.com", "revoked": 4}
    mock_redis.return_value.delete.assert_awaited_once()


@_app_patched
async def test_revoke_all_non_admin_forbidden(*_):
    app = _make_app(_user())
    async with _client(app) as c:
        r = await c.post(
            "/api/terrapod/v1/admin/authentication-tokens/actions/revoke-all",
            json={"email": "x@example.com"},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 403


@_app_patched
@patch("terrapod.api.routers.tokens.list_expiring_service_tokens")
async def test_expiring_caller_scoped(mock_expiring, *_):
    mock_expiring.return_value = [_token_mock(kind="service_bound")]
    app = _make_app(_user())
    async with _client(app) as c:
        r = await c.get(
            "/api/terrapod/v1/authentication-tokens/expiring",
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    # caller scoping is passed to the query
    assert mock_expiring.call_args.kwargs["caller_email"] == "dev@example.com"
    assert mock_expiring.call_args.kwargs["is_admin"] is False


@_app_patched
@patch("terrapod.api.routers.tokens.get_token_by_id")
async def test_retag_to_detached_gated_422(mock_get, *_):
    mock_get.return_value = _token_mock(kind="interactive", bound_to="dev@example.com")
    app = _make_app(_user(roles=["admin"]))
    async with _client(app) as c:
        r = await c.patch(
            "/api/terrapod/v1/authentication-tokens/at-svc",
            json={"data": {"attributes": {"kind": "service_detached"}}},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 422


def test_no_token_path_under_api_v2():
    """HARD INVARIANT (#495): token management is Terrapod-native only."""
    app = create_app()
    offenders = [
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v2") and "authentication-token" in route.path
    ]
    assert offenders == [], f"token paths leaked under /api/v2: {offenders}"
