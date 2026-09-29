"""The (category, key) identity, against a real engine (#1898).

The service tier proves how `resolve_variables` keys its accumulator. Only a
real database proves the other half — the unique constraints — and the two have
to agree. They did not before: the schema permitted one variable per key
whatever the category, so an operator could not add `pulumi_config:region`
beside an existing `terraform:region`, which is what moving a workspace between
engines looks like.

The migration widens, so what is asserted here is that the newly-permitted pair
is actually accepted and that the narrower guarantee still holds within a
category. A mocked session cannot answer either: it has no constraints.
"""

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VARSET_ENDPOINT = "/api/v2/organizations/default/varsets"


async def _workspace(client, name: str) -> str:
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _put_var(client, ws_id: str, key: str, category: str, value: str = "v"):
    return await client.post(
        f"/api/v2/workspaces/{ws_id}/vars",
        json={
            "data": {
                "type": "vars",
                "attributes": {"key": key, "value": value, "category": category},
            }
        },
        headers=AUTH,
    )


class TestAWorkspaceMayHoldAKeyInTwoCategories:
    async def test_the_engine_pair_is_accepted(self, app, client):
        """The case that motivated the change: staging a Pulumi equivalent
        alongside the Terraform variable it will replace, on a live workspace,
        instead of deleting the old one first and hoping."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "vi-engine-pair")

        first = await _put_var(client, ws, "region", "terraform", "eu-west-1")
        assert first.status_code == 201, first.text
        second = await _put_var(client, ws, "region", "pulumi_config", "us-east-1")
        assert second.status_code == 201, second.text

        resp = await client.get(f"/api/v2/workspaces/{ws}/vars", headers=AUTH)
        by_cat = {
            v["attributes"]["category"]: v["attributes"]["value"]
            for v in resp.json()["data"]
            if v["attributes"]["key"] == "region"
        }
        assert by_cat == {"terraform": "eu-west-1", "pulumi_config": "us-east-1"}

    async def test_a_terraform_and_an_env_variable_may_share_a_name(self, app, client):
        """Not a multi-engine case at all — this was refused for every operator,
        on every workspace, since the schema was written."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "vi-tf-and-env")
        assert (await _put_var(client, ws, "region", "terraform")).status_code == 201
        assert (await _put_var(client, ws, "region", "env")).status_code == 201


class TestTheNarrowerGuaranteeStillHolds:
    """Widening the key must not make the table a free-for-all: one key per
    category is still exactly one."""

    async def test_the_same_key_twice_in_one_category_is_refused(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "vi-dup-in-category")
        assert (await _put_var(client, ws, "region", "terraform")).status_code == 201

        dup = await _put_var(client, ws, "region", "terraform", "again")
        assert dup.status_code >= 400, (
            f"a second terraform:region was accepted ({dup.status_code}) — the "
            f"constraint was widened into uselessness"
        )
        assert dup.status_code != 500, f"should be a 4xx, not a crash: {dup.text}"


class TestTheSameHoldsForAVariableSet:
    async def test_a_set_may_hold_a_key_in_two_categories(self, app, client):
        set_auth(app, admin_user())
        resp = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": "vi-set"}}},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        vs_id = resp.json()["data"]["id"]

        async def add(category: str):
            return await client.post(
                f"/api/v2/varsets/{vs_id}/relationships/vars",
                json={
                    "data": {
                        "type": "vars",
                        "attributes": {"key": "region", "value": "v", "category": category},
                    }
                },
                headers=AUTH,
            )

        assert (await add("terraform")).status_code in (200, 201)
        second = await add("env")
        assert second.status_code in (200, 201), second.text
