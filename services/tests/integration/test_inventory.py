"""Declared inventory against a real engine (#1967, #1968).

The integration tier because the properties here are the engine's, not the
code's: a per-workspace unique constraint, `ON DELETE CASCADE` reaching four
tables, and the lazy creation of the `default` inventory on first use. A mocked
session cannot exercise any of them -- it would answer from the fixture, which
is how a missing constraint ships looking tested.

The two that matter most:

* **a duplicate host name is a 409, not a 500.** The translation reads SQLSTATE
  off the driver exception, so it only means anything when a real constraint
  raises. Two applies racing on one host name take the same path.
* **deleting an inventory does not delete the hosts.** An item belongs to the
  workspace and is owned by the Terraform that declares it; an inventory is a
  view over them. If CASCADE were wired from the inventory, `terraform destroy`
  on a view would silently empty every other view.
"""

import uuid

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS = "/api/v2/organizations/default/workspaces"
V1 = "/api/v1"


async def _workspace(client, name: str | None = None) -> str:
    name = name or f"ws-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        WS,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]["id"]


async def _item(client, ws_id: str, name: str, **attrs):
    resp = await client.post(
        f"{V1}/workspaces/{ws_id}/inventory-items",
        json={"data": {"type": "inventory-items", "attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )
    return resp


async def _declare(client, ws_id: str, name: str, **attrs) -> str:
    resp = await _item(client, ws_id, name, **attrs)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _default_inventory(client, ws_id: str) -> str:
    """The `default` inventory, which exists only once a host is declared."""
    resp = await client.get(f"{V1}/workspaces/{ws_id}/inventories", headers=AUTH)
    assert resp.status_code == 200, resp.text
    found = [i for i in resp.json()["data"] if i["attributes"]["name"] == "default"]
    assert found, "no default inventory"
    return found[0]["id"]


class TestTheUniqueConstraint:
    """A host name is unique per workspace, and the refusal is the caller's."""

    async def test_a_duplicate_host_name_is_a_409_not_a_500(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")

        resp = await _item(client, ws_id, "web-1")

        assert resp.status_code == 409, resp.text
        assert "web-1" in resp.json()["detail"]

    async def test_the_same_host_name_in_ANOTHER_workspace_is_fine(self, client, app):
        """The constraint is per workspace, not global.

        Without this, the 409 above would pass equally well for a global unique
        index -- which would make two workspaces unable to each have a `web-1`.
        """
        set_auth(app, admin_user())
        first = await _workspace(client)
        second = await _workspace(client)
        await _declare(client, first, "web-1")

        resp = await _item(client, second, "web-1")

        assert resp.status_code == 201, resp.text

    async def test_a_duplicate_inventory_name_is_a_409(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        body = {"data": {"type": "inventories", "attributes": {"name": "staging"}}}
        first = await client.post(f"{V1}/workspaces/{ws_id}/inventories", json=body, headers=AUTH)
        assert first.status_code == 201, first.text

        second = await client.post(f"{V1}/workspaces/{ws_id}/inventories", json=body, headers=AUTH)

        assert second.status_code == 409, second.text

    async def test_renaming_ONTO_an_existing_host_is_a_409(self, client, app):
        """The update path takes the same translation as create.

        Worth its own test: the two call sites are separate, and a rename is the
        one that would otherwise 500 on a constraint the caller can see coming.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        second = await _declare(client, ws_id, "web-2")

        resp = await client.patch(
            f"{V1}/inventory-items/{second}",
            json={"data": {"type": "inventory-items", "attributes": {"name": "web-1"}}},
            headers=AUTH,
        )

        assert resp.status_code == 409, resp.text


class TestLazyCreation:
    """A workspace that declares nothing carries no rows at all (#1986)."""

    async def test_a_fresh_workspace_has_no_inventories(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)

        resp = await client.get(f"{V1}/workspaces/{ws_id}/inventories", headers=AUTH)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == []

    async def test_declaring_the_first_host_creates_the_default_inventory(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)

        await _declare(client, ws_id, "web-1")

        resp = await client.get(f"{V1}/workspaces/{ws_id}/inventories", headers=AUTH)
        names = [i["attributes"]["name"] for i in resp.json()["data"]]
        assert names == ["default"]

    async def test_a_second_host_does_not_create_a_second_default(self, client, app):
        """`get_or_create` really is get-or-create against the real constraint.

        A create-unconditionally would raise on the unique index here, so this
        fails loudly rather than quietly duplicating.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client)

        await _declare(client, ws_id, "web-1")
        await _declare(client, ws_id, "web-2")

        resp = await client.get(f"{V1}/workspaces/{ws_id}/inventories", headers=AUTH)
        assert len(resp.json()["data"]) == 1

    async def test_the_default_inventory_starts_with_its_platform_source(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")

        inv_id = await _default_inventory(client, ws_id)
        resp = await client.get(f"{V1}/inventories/{inv_id}", headers=AUTH)

        sources = resp.json()["data"]["attributes"]["sources"]
        assert [s["kind"] for s in sources] == ["platform"]
        assert [s["position"] for s in sources] == [0]
        assert sources[0]["id"].startswith("invsrc-")


class TestCascade:
    """Deleting the owner removes the rows; deleting a *view* does not."""

    async def test_deleting_the_workspace_removes_its_inventory_rows(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        await _default_inventory(client, ws_id)

        gone = await client.delete(f"{V1}/workspaces/{ws_id}", headers=AUTH)
        assert gone.status_code in (200, 204), gone.text

        from sqlalchemy import func, select

        from terrapod.db.models import Inventory, InventoryItem, InventorySource
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            for model in (Inventory, InventorySource, InventoryItem):
                count = await db.execute(select(func.count()).select_from(model))
                assert count.scalar() == 0, f"{model.__tablename__} survived"

    async def test_deleting_an_inventory_leaves_the_declared_hosts_alone(self, client, app):
        """The hazard this guards is quiet: an item belongs to the workspace, so
        CASCADE from an inventory would let destroying one view empty every
        other view of the same hosts."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)

        gone = await client.delete(f"{V1}/inventories/{inv_id}", headers=AUTH)
        assert gone.status_code == 204, gone.text

        items = await client.get(f"{V1}/workspaces/{ws_id}/inventory-items", headers=AUTH)
        assert [i["attributes"]["name"] for i in items.json()["data"]] == ["web-1"]


class TestResolutionEndToEnd:
    """Declared items through the real route to a real resolution."""

    async def test_it_resolves_hosts_groups_and_the_address_fold(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(
            client,
            ws_id,
            "web-1",
            address="10.0.1.10",
            groups=["web", "prod"],
            vars={"role": "frontend"},
        )
        await _declare(client, ws_id, "db-1", address="10.0.2.10", groups=["db", "prod"])
        inv_id = await _default_inventory(client, ws_id)

        resp = await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["host-count"] == 2
        assert set(attrs["hosts"]) == {"web-1", "db-1"}
        # `address` is folded into `ansible_host` at resolution, so the stored
        # row keeps saying what the operator declared.
        assert attrs["hosts"]["web-1"]["ansible_host"] == "10.0.1.10"
        assert attrs["hosts"]["web-1"]["role"] == "frontend"
        assert attrs["groups"]["prod"] == ["db-1", "web-1"]

    async def test_an_explicit_ansible_host_beats_the_declared_address(self, client, app):
        """The fold is a default, not an override: a host var the operator wrote
        wins, because they wrote it more specifically."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(
            client,
            ws_id,
            "web-1",
            address="10.0.1.10",
            vars={"ansible_host": "bastion.internal"},
        )
        inv_id = await _default_inventory(client, ws_id)

        resp = await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        hosts = resp.json()["data"]["attributes"]["hosts"]
        assert hosts["web-1"]["ansible_host"] == "bastion.internal"

    async def test_a_host_with_no_vars_is_still_in_the_host_set(self, client, app):
        """`ansible-inventory --list` omits a var-less host from `_meta.hostvars`,
        so enumerating the host set from that shape loses it. The resolved view
        holds every host explicitly for exactly this reason."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "bare-1")
        inv_id = await _default_inventory(client, ws_id)

        resp = await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        attrs = resp.json()["data"]["attributes"]
        assert attrs["hosts"] == {"bare-1": {}}
        assert attrs["host-count"] == 1

    async def test_removing_a_host_shrinks_the_next_resolution(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        doomed = await _declare(client, ws_id, "web-2")
        inv_id = await _default_inventory(client, ws_id)
        await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        await client.delete(f"{V1}/inventory-items/{doomed}", headers=AUTH)
        resp = await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        assert list(resp.json()["data"]["attributes"]["hosts"]) == ["web-1"]


class TestLimitPreviewAgainstRealData:
    async def test_it_expands_a_group_term_against_the_live_resolution(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1", groups=["web"])
        await _declare(client, ws_id, "web-2", groups=["web"])
        await _declare(client, ws_id, "db-1", groups=["db"])
        inv_id = await _default_inventory(client, ws_id)
        await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        resp = await client.post(
            f"{V1}/inventories/{inv_id}/actions/preview-limit",
            json={"data": {"attributes": {"limit": "web:!web-2"}}},
            headers=AUTH,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["hosts"] == ["web-1"]

    async def test_it_refuses_a_regex_term_rather_than_matching_nothing(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)
        await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        resp = await client.post(
            f"{V1}/inventories/{inv_id}/actions/preview-limit",
            json={"data": {"attributes": {"limit": "~web.*"}}},
            headers=AUTH,
        )

        assert resp.status_code == 422, resp.text
