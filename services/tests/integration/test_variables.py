"""
Integration tests: Variable CRUD and Variable Sets.

Tests workspace variables (terraform + env), sensitive masking,
variable set creation, and workspace assignment — all against real Postgres.
"""

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration


WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VARSET_ENDPOINT = "/api/v2/organizations/default/varsets"


def _ws_body(name: str) -> dict:
    return {"data": {"type": "workspaces", "attributes": {"name": name}}}


def _var_body(key: str, value: str = "", category: str = "terraform", **extra) -> dict:
    attrs = {"key": key, "value": value, "category": category}
    attrs.update(extra)
    return {"data": {"type": "vars", "attributes": attrs}}


def _varset_body(name: str, **extra) -> dict:
    attrs = {"name": name}
    attrs.update(extra)
    return {"data": {"type": "varsets", "attributes": attrs}}


async def _create_workspace(client, name: str) -> str:
    resp = await client.post(WS_ENDPOINT, json=_ws_body(name), headers=AUTH)
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# Workspace Variable CRUD
# ---------------------------------------------------------------------------


class TestVariableCRUD:
    async def test_create_workspace_variable(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "var-ws")

        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json=_var_body("region", "us-east-1"),
            headers=AUTH,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["attributes"]["key"] == "region"
        assert data["attributes"]["value"] == "us-east-1"
        assert data["attributes"]["category"] == "terraform"

    async def test_create_env_variable(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "env-var-ws")

        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json=_var_body("AWS_REGION", "eu-west-1", category="env"),
            headers=AUTH,
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["category"] == "env"

    async def test_sensitive_variable_value_masked(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "sens-var-ws")

        await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json=_var_body("secret_key", "super-secret", sensitive=True),
            headers=AUTH,
        )

        # List vars — sensitive value should be null
        resp = await client.get(f"/api/v2/workspaces/{ws_id}/vars", headers=AUTH)
        assert resp.status_code == 200
        var = resp.json()["data"][0]
        assert var["attributes"]["sensitive"] is True
        assert var["attributes"]["value"] is None

    async def test_update_variable(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "upd-var-ws")

        create = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json=_var_body("count", "1"),
            headers=AUTH,
        )
        var_id = create.json()["data"]["id"]

        patch = {"data": {"type": "vars", "attributes": {"value": "5"}}}
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json=patch,
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["value"] == "5"

    async def test_delete_variable(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "del-var-ws")

        create = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json=_var_body("tmp", "val"),
            headers=AUTH,
        )
        var_id = create.json()["data"]["id"]

        resp = await client.delete(f"/api/v2/workspaces/{ws_id}/vars/{var_id}", headers=AUTH)
        assert resp.status_code == 204

        # Confirm empty list
        listing = await client.get(f"/api/v2/workspaces/{ws_id}/vars", headers=AUTH)
        assert listing.json()["data"] == []


# ---------------------------------------------------------------------------
# Variable Sets
# ---------------------------------------------------------------------------


class TestVariableSets:
    async def test_create_variable_set_with_variables(self, app, client):
        set_auth(app, admin_user())

        vs_resp = await client.post(
            VARSET_ENDPOINT,
            json=_varset_body("shared-vars", description="Shared variables"),
            headers=AUTH,
        )
        assert vs_resp.status_code == 201
        vs_id = vs_resp.json()["data"]["id"]

        # Add a variable to the set
        var_resp = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json=_var_body("env", "staging"),
            headers=AUTH,
        )
        assert var_resp.status_code == 201

        # Fetch the varset and verify variable count
        detail = await client.get(f"/api/v2/varsets/{vs_id}", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["data"]["attributes"]["var-count"] == 1

        # Also verify via the vars relationship endpoint
        vars_resp = await client.get(f"/api/v2/varsets/{vs_id}/relationships/vars", headers=AUTH)
        assert vars_resp.status_code == 200
        assert len(vars_resp.json()["data"]) == 1

    async def test_assign_variable_set_to_workspace(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "vs-assign-ws")

        vs_resp = await client.post(VARSET_ENDPOINT, json=_varset_body("assign-vs"), headers=AUTH)
        vs_id = vs_resp.json()["data"]["id"]

        assign_resp = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/workspaces",
            json={"data": [{"id": ws_id, "type": "workspaces"}]},
            headers=AUTH,
        )
        assert assign_resp.status_code == 204

        # Verify workspace is assigned
        detail = await client.get(f"/api/v2/varsets/{vs_id}", headers=AUTH)
        ws_ids = [w["id"] for w in detail.json()["data"]["relationships"]["workspaces"]["data"]]
        assert ws_id in ws_ids

    async def test_global_variable_set_applies_to_all(self, app, client):
        set_auth(app, admin_user())

        vs_resp = await client.post(
            VARSET_ENDPOINT,
            json=_varset_body("global-vs", **{"global": True}),
            headers=AUTH,
        )
        assert vs_resp.status_code == 201

        detail = await client.get(f"/api/v2/varsets/{vs_resp.json()['data']['id']}", headers=AUTH)
        assert detail.json()["data"]["attributes"]["global"] is True
