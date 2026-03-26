"""
Integration tests: Workspace CRUD + State Versioning + Locking.

All tests hit real Postgres and filesystem storage through the full
FastAPI request path.
"""

import hashlib

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


def _ws_body(name: str, **overrides) -> dict:
    attrs = {"name": name}
    attrs.update(overrides)
    return {"data": {"type": "workspaces", "attributes": attrs}}


def _sv_body(serial: int, lineage: str = "test-lineage") -> dict:
    state_bytes = f'{{"serial": {serial}, "lineage": "{lineage}"}}'.encode()
    md5 = hashlib.md5(state_bytes).hexdigest()
    return {
        "data": {
            "type": "state-versions",
            "attributes": {
                "serial": serial,
                "lineage": lineage,
                "md5": md5,
            },
        }
    }


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


class TestWorkspaceLifecycle:
    async def test_create_workspace_returns_201_with_owner(self, app, client):
        user = admin_user()
        set_auth(app, user)

        resp = await client.post(WS_ENDPOINT, json=_ws_body("test-ws"), headers=AUTH)
        assert resp.status_code == 201

        data = resp.json()["data"]
        assert data["type"] == "workspaces"
        assert data["attributes"]["name"] == "test-ws"
        # Creator is recorded as owner
        ws_id = data["id"]

        detail = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["data"]["attributes"]["owner-email"] == user.email

    async def test_create_duplicate_workspace_returns_422(self, app, client):
        set_auth(app, admin_user())

        await client.post(WS_ENDPOINT, json=_ws_body("dup-ws"), headers=AUTH)
        resp = await client.post(WS_ENDPOINT, json=_ws_body("dup-ws"), headers=AUTH)
        assert resp.status_code == 422

    async def test_get_workspace_by_name(self, app, client):
        set_auth(app, admin_user())

        await client.post(WS_ENDPOINT, json=_ws_body("by-name"), headers=AUTH)
        resp = await client.get(f"{WS_ENDPOINT}/by-name", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "by-name"

    async def test_get_workspace_by_id(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("by-id"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == ws_id

    async def test_update_workspace_settings(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("patch-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        patch_body = {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "auto-apply": True,
                    "terraform-version": "1.9.0",
                },
            }
        }
        resp = await client.patch(f"/api/v2/workspaces/{ws_id}", json=patch_body, headers=AUTH)
        assert resp.status_code == 200

        attrs = resp.json()["data"]["attributes"]
        assert attrs["auto-apply"] is True
        assert attrs["terraform-version"] == "1.9.0"

    async def test_delete_workspace(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("del-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        resp = await client.delete(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 204

        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 404

    async def test_list_workspaces_filtered_by_search(self, app, client):
        set_auth(app, admin_user())

        await client.post(WS_ENDPOINT, json=_ws_body("alpha-one"), headers=AUTH)
        await client.post(WS_ENDPOINT, json=_ws_body("alpha-two"), headers=AUTH)
        await client.post(WS_ENDPOINT, json=_ws_body("beta-one"), headers=AUTH)

        resp = await client.get(WS_ENDPOINT, params={"search[name]": "alpha"}, headers=AUTH)
        assert resp.status_code == 200
        names = [d["attributes"]["name"] for d in resp.json()["data"]]
        assert "alpha-one" in names
        assert "alpha-two" in names
        assert "beta-one" not in names


# ---------------------------------------------------------------------------
# Workspace Locking
# ---------------------------------------------------------------------------


class TestWorkspaceLocking:
    async def test_lock_and_unlock_workspace(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("lock-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        lock_resp = await client.post(f"/api/v2/workspaces/{ws_id}/actions/lock", headers=AUTH)
        assert lock_resp.status_code == 200
        assert lock_resp.json()["data"]["attributes"]["locked"] is True

        unlock_resp = await client.post(f"/api/v2/workspaces/{ws_id}/actions/unlock", headers=AUTH)
        assert unlock_resp.status_code == 200
        assert unlock_resp.json()["data"]["attributes"]["locked"] is False

    async def test_double_lock_returns_409(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("dbl-lock"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        await client.post(f"/api/v2/workspaces/{ws_id}/actions/lock", headers=AUTH)
        resp = await client.post(f"/api/v2/workspaces/{ws_id}/actions/lock", headers=AUTH)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# State Versions
# ---------------------------------------------------------------------------


class TestStateVersions:
    async def test_create_and_upload_state_version(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("sv-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        # Create state version
        sv_resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json=_sv_body(1),
            headers=AUTH,
        )
        assert sv_resp.status_code == 201
        sv_id = sv_resp.json()["data"]["id"]
        assert sv_id.startswith("sv-")

        # Upload content via the presigned-style URL
        state_bytes = b'{"serial": 1, "lineage": "test-lineage"}'
        upload_resp = await client.put(
            f"/api/v2/state-versions/{sv_id}/content",
            content=state_bytes,
        )
        assert upload_resp.status_code == 200

    async def test_state_serial_conflict_returns_409(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("serial-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json=_sv_body(1),
            headers=AUTH,
        )
        dup = await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json=_sv_body(1),
            headers=AUTH,
        )
        assert dup.status_code == 409

    async def test_list_state_versions_ordered_by_serial_desc(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("list-sv"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        for serial in range(1, 4):
            await client.post(
                f"/api/v2/workspaces/{ws_id}/state-versions",
                json=_sv_body(serial),
                headers=AUTH,
            )

        resp = await client.get(f"/api/v2/workspaces/{ws_id}/state-versions", headers=AUTH)
        assert resp.status_code == 200
        serials = [d["attributes"]["serial"] for d in resp.json()["data"]]
        assert serials == [3, 2, 1]

    async def test_current_state_version_is_latest(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("curr-sv"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        for serial in range(1, 3):
            await client.post(
                f"/api/v2/workspaces/{ws_id}/state-versions",
                json=_sv_body(serial),
                headers=AUTH,
            )

        resp = await client.get(f"/api/v2/workspaces/{ws_id}/current-state-version", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["serial"] == 2

    async def test_no_current_state_version_returns_404(self, app, client):
        set_auth(app, admin_user())

        create = await client.post(WS_ENDPOINT, json=_ws_body("empty-sv"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        resp = await client.get(f"/api/v2/workspaces/{ws_id}/current-state-version", headers=AUTH)
        assert resp.status_code == 404
