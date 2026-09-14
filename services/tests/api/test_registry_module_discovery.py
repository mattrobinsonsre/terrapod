"""POST /registry-modules/discover (#1584).

Proposes the modules in a repository — never registers any. Platform admin
only, since it reads the repository with the platform's VCS credentials. The
tree walk is the one workspace autodiscovery uses, patched here at its source.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.services import vcs_rate_limit

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_WALK = "terrapod.api.routers.autodiscovery_rules._walk_repo_for_rule"
REPO = "https://github.com/org/terraform-azurerm-management-groups"
PATHS = [
    "main.tf",
    "modules/create/main.tf",
    "modules/update/main.tf",
    "examples/basic/main.tf",
    "README.md",
]


def _user(admin: bool):
    return AuthenticatedUser(
        email="u@test.com",
        display_name="U",
        roles=["admin"] if admin else ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _db(registered=(), connection_found=True):
    """Two queries: the VCS connection, then modules already registered."""
    conn = MagicMock()
    conn.provider = "github"
    conn_result = MagicMock()
    conn_result.scalars.return_value.first.return_value = conn if connection_found else None
    rows = MagicMock()
    rows.all.return_value = list(registered)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[conn_result, rows])
    return db


async def _discover(db, *, admin=True, attrs=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(admin)
    app.dependency_overrides[get_db] = lambda: db
    body = {
        "data": {
            "type": "registry-module-discoveries",
            "attributes": attrs
            or {"vcs-connection-id": f"vcs-{uuid.uuid4()}", "vcs-repo-url": REPO},
        }
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.post("/api/terrapod/v1/registry-modules/discover", json=body, headers=_AUTH)


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestDiscover:
    async def test_proposes_the_root_and_submodules_flagging_what_is_registered(self, *_):
        db = _db(registered=[("management-groups-create", "azurerm", "modules/create")])
        with patch(_WALK, new=AsyncMock(return_value=(PATHS, "main"))):
            resp = await _discover(db)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["vcs-branch"] == "main"
        rows = {c["subdirectory"]: c for c in attrs["candidates"]}
        assert list(rows) == ["", "modules/create", "modules/update"], "examples/ left out"
        assert rows[""]["suggested-name"] == "management-groups"
        assert rows["modules/update"]["suggested-name"] == "management-groups-update"
        assert {c["suggested-provider"] for c in rows.values()} == {"azurerm"}
        assert rows["modules/create"]["registered-as"] == {
            "name": "management-groups-create",
            "provider": "azurerm",
        }
        assert rows["modules/update"]["registered-as"] is None

    async def test_nothing_is_registered(self, *_):
        db = _db()
        with patch(_WALK, new=AsyncMock(return_value=(PATHS, "main"))):
            await _discover(db)
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_the_walk_runs_under_its_own_rate_limit_label(self, *_):
        seen = []

        async def walk(_target):
            seen.append(vcs_rate_limit.current_source())
            return PATHS, "main"

        with patch(_WALK, new=walk):
            await _discover(_db())
        assert seen == ["module-discovery"]

    async def test_non_admins_are_refused(self, *_):
        with patch(_WALK, new=AsyncMock()) as walk:
            resp = await _discover(_db(), admin=False)
        assert resp.status_code == 403
        walk.assert_not_awaited()

    async def test_an_unknown_connection_is_422(self, *_):
        with patch(_WALK, new=AsyncMock()) as walk:
            resp = await _discover(_db(connection_found=False))
        assert resp.status_code == 422
        walk.assert_not_awaited()

    async def test_a_malformed_connection_id_is_422(self, *_):
        resp = await _discover(
            _db(), attrs={"vcs-connection-id": "not-a-uuid", "vcs-repo-url": REPO}
        )
        assert resp.status_code == 422

    async def test_a_truncated_tree_is_413_with_a_module_specific_message(self, *_):
        truncated = HTTPException(status_code=413, detail="autodiscovery wording")
        with patch(_WALK, new=AsyncMock(side_effect=truncated)):
            resp = await _discover(_db())
        assert resp.status_code == 413
        assert "register its modules individually" in resp.json()["detail"]
