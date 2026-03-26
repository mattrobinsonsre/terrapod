"""
Integration tests: RBAC enforcement at the API level.

Tests admin bypass, owner permissions, regular-user isolation, label-based
RBAC, and deny rules — all with real role rows in Postgres.
"""

import hashlib

import pytest

from tests.integration.conftest import (
    AUTH,
    admin_user,
    assign_role,
    insert_role,
    regular_user,
    set_auth,
    user_with_roles,
)

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


def _ws_body(name: str, **overrides) -> dict:
    attrs = {"name": name}
    attrs.update(overrides)
    return {"data": {"type": "workspaces", "attributes": attrs}}


def _sv_body(serial: int, lineage: str = "rbac-lineage") -> dict:
    state_bytes = f'{{"serial": {serial}, "lineage": "{lineage}"}}'.encode()
    md5 = hashlib.md5(state_bytes).hexdigest()
    return {
        "data": {
            "type": "state-versions",
            "attributes": {"serial": serial, "lineage": lineage, "md5": md5},
        }
    }


# ---------------------------------------------------------------------------
# Admin bypass
# ---------------------------------------------------------------------------


class TestAdminBypass:
    async def test_admin_can_read_any_workspace(self, app, client):
        # Owner creates workspace
        owner = regular_user("owner@test.com")
        set_auth(app, owner)
        create = await client.post(WS_ENDPOINT, json=_ws_body("admin-read-ws"), headers=AUTH)
        assert create.status_code == 201, create.text
        ws_id = create.json()["data"]["id"]

        # Admin reads it
        set_auth(app, admin_user("other-admin@test.com"))
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 200

    async def test_admin_can_delete_any_workspace(self, app, client):
        owner = regular_user("owner2@test.com")
        set_auth(app, owner)
        create = await client.post(WS_ENDPOINT, json=_ws_body("admin-del-ws"), headers=AUTH)
        assert create.status_code == 201, create.text
        ws_id = create.json()["data"]["id"]

        set_auth(app, admin_user("admin-del@test.com"))
        resp = await client.delete(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Owner permissions
# ---------------------------------------------------------------------------


class TestOwnerPermissions:
    async def test_owner_can_update_their_workspace(self, app, client):
        owner = regular_user("ws-owner@test.com")
        set_auth(app, owner)

        create = await client.post(WS_ENDPOINT, json=_ws_body("owner-patch"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        patch = {
            "data": {
                "type": "workspaces",
                "attributes": {"auto-apply": True},
            }
        }
        resp = await client.patch(f"/api/v2/workspaces/{ws_id}", json=patch, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["auto-apply"] is True

    async def test_owner_can_delete_their_workspace(self, app, client):
        owner = regular_user("ws-owner2@test.com")
        set_auth(app, owner)

        create = await client.post(WS_ENDPOINT, json=_ws_body("owner-del"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        resp = await client.delete(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Regular user isolation
# ---------------------------------------------------------------------------


class TestRegularUserAccess:
    async def test_regular_user_cannot_see_others_workspace(self, app, client):
        # Owner creates workspace
        set_auth(app, regular_user("alice@test.com"))
        create = await client.post(WS_ENDPOINT, json=_ws_body("private-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        # Bob can't see it (ID-based lookup returns 403, not 404)
        set_auth(app, regular_user("bob@test.com"))
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 403

    async def test_regular_user_cannot_delete_others_workspace(self, app, client):
        set_auth(app, regular_user("charlie@test.com"))
        create = await client.post(WS_ENDPOINT, json=_ws_body("nodelet-ws"), headers=AUTH)
        ws_id = create.json()["data"]["id"]

        set_auth(app, regular_user("dave@test.com"))
        resp = await client.delete(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Label-based RBAC
# ---------------------------------------------------------------------------


class TestLabelBasedRBAC:
    async def test_custom_role_grants_read_via_labels(self, app, client, _create_tables):
        engine = _create_tables

        # Admin creates workspace with labels
        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body("labeled-ws", labels={"env": "staging"}),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        # Create role allowing access to env=staging workspaces
        await insert_role(
            engine,
            "staging-reader",
            workspace_permission="read",
            allow_labels={"env": "staging"},
        )
        await assign_role(engine, "local", "reader@test.com", "staging-reader")

        # User with custom role can read the workspace
        set_auth(app, user_with_roles("reader@test.com", ["staging-reader", "everyone"]))
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 200

    async def test_custom_role_with_write_allows_state_creation(self, app, client, _create_tables):
        engine = _create_tables

        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body("write-ws", labels={"env": "dev"}),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        await insert_role(
            engine,
            "dev-writer",
            workspace_permission="write",
            allow_labels={"env": "dev"},
        )
        await assign_role(engine, "local", "writer@test.com", "dev-writer")

        set_auth(app, user_with_roles("writer@test.com", ["dev-writer", "everyone"]))
        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json=_sv_body(1),
            headers=AUTH,
        )
        assert resp.status_code == 201

    async def test_read_role_cannot_create_state_version(self, app, client, _create_tables):
        engine = _create_tables

        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body("ro-ws", labels={"env": "prod"}),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        await insert_role(
            engine,
            "prod-reader",
            workspace_permission="read",
            allow_labels={"env": "prod"},
        )
        await assign_role(engine, "local", "readonly@test.com", "prod-reader")

        set_auth(app, user_with_roles("readonly@test.com", ["prod-reader", "everyone"]))
        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json=_sv_body(1),
            headers=AUTH,
        )
        assert resp.status_code == 403

    async def test_everyone_label_grants_read(self, app, client):
        # Admin creates workspace with access: everyone label
        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body("public-ws", labels={"access": "everyone"}),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        # Random user (only 'everyone' role) can read it
        set_auth(app, regular_user("anon@test.com"))
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Deny rules
# ---------------------------------------------------------------------------


class TestDenyRules:
    async def test_deny_label_blocks_access(self, app, client, _create_tables):
        engine = _create_tables

        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body(
                "deny-label-ws",
                labels={"env": "prod", "sensitive": "true"},
            ),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        # Role: allows env=prod but denies sensitive=true
        await insert_role(
            engine,
            "prod-not-sensitive",
            workspace_permission="write",
            allow_labels={"env": "prod"},
            deny_labels={"sensitive": "true"},
        )
        await assign_role(engine, "local", "denied@test.com", "prod-not-sensitive")

        set_auth(
            app,
            user_with_roles("denied@test.com", ["prod-not-sensitive", "everyone"]),
        )
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 403

    async def test_deny_name_blocks_access(self, app, client, _create_tables):
        engine = _create_tables

        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json=_ws_body("forbidden-ws", labels={"env": "dev"}),
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]

        # Role: allows env=dev but denies workspace by name
        await insert_role(
            engine,
            "dev-except-forbidden",
            workspace_permission="write",
            allow_labels={"env": "dev"},
            deny_names=["forbidden-ws"],
        )
        await assign_role(engine, "local", "blocked@test.com", "dev-except-forbidden")

        set_auth(
            app,
            user_with_roles("blocked@test.com", ["dev-except-forbidden", "everyone"]),
        )
        resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert resp.status_code == 403
