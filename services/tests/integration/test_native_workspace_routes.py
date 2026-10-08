"""Native workspace read, update and list against real Postgres (#1554).

The services-api tests pin the wiring with a mocked database; this pins the SQL.
The load-bearing assertion is the engine boundary, a property of the query
rather than of the code's control flow: the native surface returns a Pulumi
workspace while the TFE surface — which a `terraform` CLI talks to — still never
does.

There was a second boundary here, that gating an engine off hid its workspaces.
That switch is withdrawn (#1986), so there is nothing left to hide them and the
tests went with it; `test_engines_endpoint.py` now pins the inverse — that no
setting can shrink the engine list.
"""

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

    #: Attributes the native surface serves and the compatibility surface does
    #: not, with the reason. Enumerated rather than tolerated: a difference that
    #: nobody had to write down is a difference nobody notices growing.
    NATIVE_ONLY_ATTRS = {
        "pulumi-bind-plan": "a Pulumi concept, and this surface serves Terraform alone (#1911)",
    }

    async def test_a_terraform_workspace_reads_the_same_on_both_surfaces(self, app, client):
        """The same workspace, described identically wherever the two overlap.

        This asserted byte-identity until #1911, which was true and slightly
        stronger than the property worth holding. The surfaces are *designed* to
        differ — the native one serves every engine — so the native body is a
        strict SUPERSET, and what matters is that no shared attribute disagrees
        and that every extra one is there on purpose.

        Gated on the surface rather than on the workspace's engine, deliberately:
        gating on the engine would drop the attribute from the native body too,
        where the provider reads it as `Optional+Computed` and a value that turned
        null under an unchanged configuration is a perpetual diff.
        """
        set_auth(app, admin_user())
        tid = await _create(client, "tf-both", "terraform")
        native = await client.get(f"{NATIVE}/{tid}", headers=AUTH)
        tfe = await client.get(f"/api/v2/workspaces/{tid}", headers=AUTH)
        assert native.status_code == tfe.status_code == 200

        n = native.json()["data"]["attributes"]
        t = tfe.json()["data"]["attributes"]
        assert set(t) <= set(n), (
            f"the TFE surface serves attributes the native one does not: {set(t) - set(n)}"
        )
        assert {k: n[k] for k in t} == t, "a shared attribute disagrees between the two surfaces"
        assert set(n) - set(t) == set(self.NATIVE_ONLY_ATTRS), (
            f"native-only attributes changed: {set(n) - set(t)}. Add it to "
            f"NATIVE_ONLY_ATTRS with a reason, or stop serving it only there."
        )


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


class TestDeletingAWorkspaceOfAnyEngine:
    """The delete half of the same boundary (#1574).

    `DELETE /api/v1/workspaces/{id}` resolved through the TFE surface's
    Terraform-only lookup, so a Pulumi workspace answered 404 and could not be
    deleted at all — not from the UI's delete button, nor through go-terrapod,
    the provider or MCP. It is the one route that had no native counterpart.
    """

    async def _marker(self, client, name: str) -> dict | None:
        r = await client.get("/api/v1/deleted-workspaces", headers=AUTH)
        assert r.status_code == 200, r.text
        for d in r.json()["data"]:
            if d["attributes"].get("workspace-name") == name:
                return d
        return None

    async def test_a_pulumi_workspace_deletes_and_leaves_a_marker(self, app, client):
        set_auth(app, admin_user())
        pid = await _create(client, "proj::doomed", "pulumi")

        gone = await client.delete(f"{NATIVE}/{pid}", headers=AUTH)
        assert gone.status_code == 204, gone.text

        assert (await client.get(f"{NATIVE}/{pid}", headers=AUTH)).status_code == 404
        assert "proj::doomed" not in _names(await client.get(NATIVE, headers=AUTH))
        # The marker is what makes the deletion recoverable; a delete that
        # skipped it would look identical until someone needed it back.
        assert await self._marker(client, "proj::doomed") is not None

    async def test_a_terraform_workspace_still_deletes(self, app, client):
        # The boundary moved for Pulumi; it must not have moved for Terraform.
        set_auth(app, admin_user())
        tid = await _create(client, "tf-doomed", "terraform")

        gone = await client.delete(f"{NATIVE}/{tid}", headers=AUTH)
        assert gone.status_code == 204, gone.text
        assert (await client.get(f"{NATIVE}/{tid}", headers=AUTH)).status_code == 404

    async def test_it_deletes_by_name_too(self, app, client):
        # A Pulumi workspace is addressed by its project::stack name everywhere
        # a person meets it, so the delete has to accept one.
        set_auth(app, admin_user())
        await _create(client, "proj::by-name", "pulumi")

        gone = await client.delete(f"{NATIVE}/proj::by-name", headers=AUTH)
        assert gone.status_code == 204, gone.text
        assert "proj::by-name" not in _names(await client.get(NATIVE, headers=AUTH))
