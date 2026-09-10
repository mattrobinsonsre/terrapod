"""Native workspace read, update and list against real Postgres (#1554).

The services-api tests pin the wiring with a mocked database; this pins the SQL.
The load-bearing assertions are the two engine boundaries, both properties of
the query rather than of the code's control flow:

- the native surface returns a Pulumi workspace while the TFE surface — which a
  `terraform` CLI talks to — still never does;
- gating an engine off makes its workspaces absent from the native surface, and
  turning it back on brings them back unchanged, because nothing was deleted.
"""

from unittest.mock import patch

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

NATIVE = "/api/v1/workspaces"
TFE_LIST = "/api/v2/organizations/default/workspaces"


async def _create(client, name: str, engine: str) -> str:
    r = await client.post(
        NATIVE,
        json={"data": {"type": "workspaces", "attributes": {"name": name, "engine": engine}}},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _names(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {d["attributes"]["name"] for d in resp.json()["data"]}


class TestTheNativeSurfaceServesEveryEngine:
    async def test_list_read_and_update_a_pulumi_workspace(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::dev", "pulumi")
        await _create(client, "tf-native", "terraform")

        assert {"proj::dev", "tf-native"} <= _names(await client.get(NATIVE, headers=AUTH))
        assert _names(
            await client.get(NATIVE, params={"filter[engine]": "pulumi"}, headers=AUTH)
        ) == {"proj::dev"}

        by_id = await client.get(f"{NATIVE}/{pid}", headers=AUTH)
        by_name = await client.get(f"{NATIVE}/proj::dev", headers=AUTH)
        assert by_id.status_code == by_name.status_code == 200
        assert by_id.json()["data"]["id"] == by_name.json()["data"]["id"] == pid
        assert by_id.json()["data"]["attributes"]["engine"] == "pulumi"

        # A rename follows the Pulumi name rule, which the plain rule rejects.
        upd = await client.patch(
            f"{NATIVE}/{pid}",
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {"name": "proj::prod", "pulumi-bind-plan": True},
                }
            },
            headers=AUTH,
        )
        assert upd.status_code == 200, upd.text
        attrs = upd.json()["data"]["attributes"]
        assert attrs["name"] == "proj::prod"
        assert attrs["pulumi-bind-plan"] is True
        again = await client.get(f"{NATIVE}/{pid}", headers=AUTH)
        assert again.json()["data"]["attributes"]["pulumi-bind-plan"] is True

    async def test_a_terraform_workspace_reads_the_same_on_both_surfaces(self, app, client):
        set_auth(app, admin_user())
        tid = await _create(client, "tf-both", "terraform")
        native = await client.get(f"{NATIVE}/{tid}", headers=AUTH)
        tfe = await client.get(f"/api/v2/workspaces/{tid}", headers=AUTH)
        assert native.status_code == tfe.status_code == 200
        assert native.json()["data"]["attributes"] == tfe.json()["data"]["attributes"]


class TestTheTfeSurfaceStaysTerraformOnly:
    async def test_it_never_returns_a_pulumi_workspace(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::hidden", "pulumi")
        assert "proj::hidden" not in _names(await client.get(TFE_LIST, headers=AUTH))
        assert (await client.get(f"/api/v2/workspaces/{pid}", headers=AUTH)).status_code == 404
        patched = await client.patch(
            f"/api/v2/workspaces/{pid}",
            json={"data": {"type": "workspaces", "attributes": {"pulumi-bind-plan": True}}},
            headers=AUTH,
        )
        assert patched.status_code == 404


class TestGatingAnEngineOffHidesItsWorkspaces:
    async def test_absent_while_off_and_back_unchanged_when_on(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::gated", "pulumi")
        await client.patch(
            f"{NATIVE}/{pid}",
            json={"data": {"type": "workspaces", "attributes": {"pulumi-bind-plan": True}}},
            headers=AUTH,
        )

        with patch("terrapod.engines.engine_enabled", side_effect=lambda e: e != "pulumi"):
            assert "proj::gated" not in _names(await client.get(NATIVE, headers=AUTH))
            assert (await client.get(f"{NATIVE}/{pid}", headers=AUTH)).status_code == 404
            assert (await client.get(f"{NATIVE}/proj::gated", headers=AUTH)).status_code == 404
            assert (
                await client.patch(
                    f"{NATIVE}/{pid}", json={"data": {"attributes": {}}}, headers=AUTH
                )
            ).status_code == 404

        back = await client.get(f"{NATIVE}/{pid}", headers=AUTH)
        assert back.status_code == 200
        assert back.json()["data"]["attributes"]["pulumi-bind-plan"] is True
