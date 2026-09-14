"""A registry module's subdirectory through the API (#1583).

A submodule is an ordinary module whose `subdirectory` is set; it must live in
a repository, and one repository subdirectory can be registered only once (a
partial unique index). These pin how the create, update and VCS endpoints
accept, validate and return it.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_non_runner
from terrapod.auth import capabilities as cap
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_R = "terrapod.api.routers.registry_modules"
REPO = "https://github.com/org/management-groups"
MODULE_PATH = "/api/terrapod/v1/registry-modules/private/default/create/azurerm"


def _module(**overrides):
    base = {
        "id": uuid.uuid4(),
        "name": "create",
        "namespace": "default",
        "provider": "azurerm",
        "status": "pending",
        "labels": {},
        "owner_email": "u@test.com",
        "source": "vcs",
        "vcs_connection_id": None,
        "vcs_repo_url": REPO,
        "vcs_branch": "",
        "vcs_tag_pattern": "v*",
        "vcs_last_tag": "",
        "subdirectory": "",
        "versions": [],
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _app(db):
    user = AuthenticatedUser(
        email="u@test.com",
        display_name="U",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_non_runner] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _call(db, method, path, attrs):
    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url=_BASE) as c:
        return await c.request(
            method,
            path,
            json={"data": {"type": "registry-modules", "attributes": attrs}},
            headers=_AUTH,
        )


def _integrity_error():
    return IntegrityError("INSERT ...", {}, Exception("duplicate key"))


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestCreate:
    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_a_submodule_is_created_with_its_subdirectory(self, create, *_):
        module = _module()
        create.return_value = module

        resp = await _call(
            _db(),
            "POST",
            "/api/terrapod/v1/registry-modules",
            {
                "name": "create",
                "provider": "azurerm",
                "vcs-repo-url": REPO,
                "subdirectory": "/modules/create/",
            },
        )

        assert resp.status_code == 201
        assert module.subdirectory == "modules/create", "stored in canonical form"
        assert resp.json()["data"]["attributes"]["subdirectory"] == "modules/create"

    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_a_root_module_has_an_empty_subdirectory(self, create, *_):
        create.return_value = _module(vcs_repo_url="")

        resp = await _call(
            _db(),
            "POST",
            "/api/terrapod/v1/registry-modules",
            {"name": "create", "provider": "azurerm"},
        )

        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["subdirectory"] == ""

    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_a_subdirectory_without_a_repository_is_refused(self, create, *_):
        resp = await _call(
            _db(),
            "POST",
            "/api/terrapod/v1/registry-modules",
            {"name": "create", "provider": "azurerm", "subdirectory": "modules/create"},
        )

        assert resp.status_code == 422
        create.assert_not_awaited()

    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_an_escaping_subdirectory_is_refused(self, create, *_):
        resp = await _call(
            _db(),
            "POST",
            "/api/terrapod/v1/registry-modules",
            {
                "name": "create",
                "provider": "azurerm",
                "vcs-repo-url": REPO,
                "subdirectory": "../elsewhere",
            },
        )

        assert resp.status_code == 422
        assert "subdirectory" in resp.json()["detail"].lower()
        create.assert_not_awaited()

    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_registering_a_repository_subdirectory_twice_is_a_conflict(self, create, *_):
        create.return_value = _module()
        db = _db()
        db.commit.side_effect = _integrity_error()

        resp = await _call(
            db,
            "POST",
            "/api/terrapod/v1/registry-modules",
            {
                "name": "create2",
                "provider": "azurerm",
                "vcs-repo-url": REPO,
                "subdirectory": "modules/create",
            },
        )

        assert resp.status_code == 409
        assert "modules/create" in resp.json()["detail"]
        db.rollback.assert_awaited()

    @patch(f"{_R}.create_module", new_callable=AsyncMock)
    async def test_an_unrelated_integrity_error_is_not_dressed_up_as_a_subdirectory_clash(
        self, create, *_
    ):
        create.return_value = _module(vcs_repo_url="")
        db = _db()
        db.commit.side_effect = _integrity_error()

        resp = await _call(
            db,
            "POST",
            "/api/terrapod/v1/registry-modules",
            {"name": "create", "provider": "azurerm"},
        )

        # Left to the app's own IntegrityError handling — not reported as a
        # clash of repository subdirectories when there is no subdirectory.
        assert "already registered as another module" not in resp.text


ADMIN = frozenset({cap.REGISTRY_ADMIN, cap.REGISTRY_WRITE, cap.REGISTRY_READ})


@patch(f"{_R}.resolve_registry_capabilities_for", new_callable=AsyncMock, return_value=ADMIN)
@patch(f"{_R}.get_module", new_callable=AsyncMock)
@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestUpdate:
    async def test_patch_sets_the_subdirectory(self, _db_, _redis, _storage, get_mod, _caps):
        module = _module()
        get_mod.return_value = module

        resp = await _call(_db(), "PATCH", MODULE_PATH, {"subdirectory": "modules/create"})

        assert resp.status_code == 200
        assert module.subdirectory == "modules/create"

    async def test_patch_refuses_a_subdirectory_with_no_repository(
        self, _db_, _redis, _storage, get_mod, _caps
    ):
        get_mod.return_value = _module(vcs_repo_url="")

        resp = await _call(_db(), "PATCH", MODULE_PATH, {"subdirectory": "modules/create"})

        assert resp.status_code == 422

    async def test_removing_the_repository_clears_the_subdirectory(
        self, _db_, _redis, _storage, get_mod, _caps
    ):
        module = _module(subdirectory="modules/create")
        get_mod.return_value = module

        resp = await _call(_db(), "PATCH", MODULE_PATH, {"vcs-repo-url": ""})

        assert resp.status_code == 200
        assert module.subdirectory == "", "a submodule of no repository is not kept"

    async def test_vcs_update_leaves_an_unmentioned_subdirectory_alone(
        self, _db_, _redis, _storage, get_mod, _caps
    ):
        module = _module(subdirectory="modules/create")
        get_mod.return_value = module

        resp = await _call(
            _db(),
            "PATCH",
            f"{MODULE_PATH}/vcs",
            {"source": "vcs", "vcs_repo_url": REPO, "vcs_branch": "main", "vcs_tag_pattern": "v*"},
        )

        assert resp.status_code == 200
        assert module.subdirectory == "modules/create"

    async def test_vcs_update_sets_a_subdirectory(self, _db_, _redis, _storage, get_mod, _caps):
        module = _module()
        get_mod.return_value = module

        resp = await _call(
            _db(),
            "PATCH",
            f"{MODULE_PATH}/vcs",
            {"source": "vcs", "vcs_repo_url": REPO, "subdirectory": "modules/create"},
        )

        assert resp.status_code == 200
        assert module.subdirectory == "modules/create"

    async def test_disconnecting_vcs_clears_the_subdirectory(
        self, _db_, _redis, _storage, get_mod, _caps
    ):
        # What the UI's "Disconnect VCS" sends: no subdirectory at all.
        module = _module(subdirectory="modules/create")
        get_mod.return_value = module

        resp = await _call(
            _db(),
            "PATCH",
            f"{MODULE_PATH}/vcs",
            {
                "source": "upload",
                "vcs_connection_id": "",
                "vcs_repo_url": "",
                "vcs_branch": "",
                "vcs_tag_pattern": "v*",
            },
        )

        assert resp.status_code == 200
        assert module.subdirectory == ""
