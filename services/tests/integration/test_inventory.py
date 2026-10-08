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
        assert sources[0]["api-resolvable"] is True
        assert sources[0]["id"].startswith("invsrc-")


class TestCascade:
    """Deleting the owner removes the rows; deleting a *view* does not."""

    async def test_deleting_the_workspace_removes_its_inventory_rows(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)
        # Resolve so there is a version row to cascade too.
        r = await client.post(f"{V1}/inventories/{inv_id}/actions/resolve", headers=AUTH)
        assert r.status_code == 200, r.text

        gone = await client.delete(f"{V1}/workspaces/{ws_id}", headers=AUTH)
        assert gone.status_code in (200, 204), gone.text

        from sqlalchemy import func, select

        from terrapod.db.models import (
            Inventory,
            InventoryItem,
            InventorySource,
            InventoryVersion,
        )
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            for model in (Inventory, InventorySource, InventoryItem, InventoryVersion):
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

    async def test_deleting_an_inventory_removes_its_snapshots(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)
        await client.post(f"{V1}/inventories/{inv_id}/actions/resolve", headers=AUTH)

        await client.delete(f"{V1}/inventories/{inv_id}", headers=AUTH)

        from sqlalchemy import func, select

        from terrapod.db.models import InventoryVersion
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            count = await db.execute(select(func.count()).select_from(InventoryVersion))
            assert count.scalar() == 0


class TestResolutionEndToEnd:
    """Declared items through the real route to a real snapshot."""

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
        assert attrs["produced-by"] == "api"

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
        so enumerating the host set from that shape loses it. The snapshot holds
        every host explicitly for exactly this reason."""
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
        resp = await client.post(f"{V1}/inventories/{inv_id}/actions/resolve", headers=AUTH)

        assert list(resp.json()["data"]["attributes"]["hosts"]) == ["web-1"]

    async def test_the_resolved_view_creates_the_first_snapshot_itself(self, client, app):
        """#1967's observability scope: a workspace that has just declared its
        hosts can see them without waiting for a configure to exist."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)

        before = await client.get(f"{V1}/inventories/{inv_id}/versions", headers=AUTH)
        assert before.json()["data"] == []

        await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)

        after = await client.get(f"{V1}/inventories/{inv_id}/versions", headers=AUTH)
        assert len(after.json()["data"]) == 1


class TestSnapshotPruning:
    """The history is bounded, and the bound keeps the newest."""

    async def test_it_keeps_the_newest_twenty(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = await _default_inventory(client, ws_id)

        from terrapod.services import inventory_service as inv

        # A host per iteration, because the action writes only when the
        # resolution has moved -- which is the behaviour the class below pins.
        # Resolving the same set twenty-five times is one row, so a loop that
        # only resolved would exercise no pruning at all while still passing a
        # bare length assertion.
        for i in range(inv.MAX_VERSIONS_PER_INVENTORY + 5):
            await _declare(client, ws_id, f"web-{i + 2}")
            resp = await client.post(f"{V1}/inventories/{inv_id}/actions/resolve", headers=AUTH)
            assert resp.status_code == 200, resp.text
        newest = resp.json()["data"]["id"]

        listed = await client.get(f"{V1}/inventories/{inv_id}/versions", headers=AUTH)
        ids = [v["id"] for v in listed.json()["data"]]

        assert len(ids) == inv.MAX_VERSIONS_PER_INVENTORY
        # Newest first, and the one just written survived -- a prune that kept
        # the *oldest* would pass a bare length assertion.
        assert ids[0] == newest


class TestRunnerPostedSnapshots:
    """What a configure will post on its first day (#1972)."""

    async def test_a_runner_snapshot_records_its_run(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "declared-1")
        inv_id = await _default_inventory(client, ws_id)

        run_id = str(uuid.uuid4())
        from terrapod.auth.runner_tokens import generate_runner_token

        token = generate_runner_token(run_id)

        # The grant is "a runner token may manage the inventory of its own run's
        # workspace", so the run has to exist and belong to this workspace.
        # Seeded through the ORM rather than raw SQL: `runs` has ~70 columns and
        # a hand-written INSERT breaks on the next NOT NULL anyone adds, with a
        # driver error that names the column and nothing about why this test
        # cares.
        from terrapod.db.models import Run
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            db.add(
                Run(
                    id=uuid.UUID(run_id),
                    workspace_id=uuid.UUID(ws_id.removeprefix("ws-")),
                    status="planning",
                    source="tfe-api",
                    plan_only=False,
                )
            )
            await db.commit()

        resp = await client.post(
            f"{V1}/inventories/{inv_id}/versions",
            json={
                "data": {
                    "type": "inventory-versions",
                    "attributes": {
                        "hosts": {"from-ansible": {"ansible_host": "10.9.9.9"}},
                        "groups": {"discovered": ["from-ansible"]},
                    },
                }
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["produced-by"] == "runner"
        assert attrs["produced-by-ref"] == run_id
        assert attrs["host-count"] == 1

        # It lands in the history, which is what posting it is for: the basis a
        # partial-configure retry subtracts against (#1973).
        versions = await client.get(f"{V1}/inventories/{inv_id}/versions", headers=AUTH)
        assert versions.status_code == 200, versions.text
        posted = [v for v in versions.json()["data"] if v["id"] == resp.json()["data"]["id"]]
        assert posted, "the runner's snapshot is not in the history"

        # But it is NOT what a reader sees, and that is deliberate. This
        # inventory's only source is `terraform`, which the API resolves itself
        # from the declared rows -- so the live answer is authoritative and a
        # recorded row describing the same source is history, however recent.
        # A runner's resolution wins only where the API cannot resolve at all,
        # which is the kind git (#1929) will be the first to add.
        read = await client.get(f"{V1}/inventories/{inv_id}/resolved", headers=AUTH)
        assert read.status_code == 200, read.text
        assert list(read.json()["data"]["attributes"]["hosts"]) == ["declared-1"]

        # And it carries no stamp, because the producer could not say what it
        # resolved against -- which is why it never reads as current.
        from sqlalchemy import select

        from terrapod.db.models import InventoryVersion

        async with get_db_session() as db:
            got = await db.execute(
                select(InventoryVersion.source_stamp).where(
                    InventoryVersion.produced_by == InventoryVersion.SOURCE_RUNNER
                )
            )
            assert [row[0] for row in got.all()] == [""]


class TestLimitPreviewAgainstRealData:
    async def test_it_expands_a_group_term_from_the_stored_snapshot(self, client, app):
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


class TestTheSourceStampDecidesWhenARowIsWritten:
    """Live reads without a write per read, against a real database.

    Resolving is a query, so a read is live. What the stamp buys is that the
    bounded version history -- the basis a partial-configure retry subtracts
    against (#1973) -- is not evicted by reading. Every assertion here is about
    row COUNT rather than payload, because the payload is identical either way
    and that is exactly what hid this class of bug.
    """

    @staticmethod
    async def _versions(inv_id: str) -> int:
        from sqlalchemy import func, select

        from terrapod.db.models import InventoryVersion
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            got = await db.execute(
                select(func.count())
                .select_from(InventoryVersion)
                .where(InventoryVersion.inventory_id == uuid.UUID(inv_id.removeprefix("invver-")))
            )
            return int(got.scalar() or 0)

    @staticmethod
    async def _stamps(inv_id: str) -> list[str]:
        from sqlalchemy import select

        from terrapod.db.models import InventoryVersion
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            got = await db.execute(
                select(InventoryVersion.source_stamp)
                .where(InventoryVersion.inventory_id == uuid.UUID(inv_id))
                .order_by(InventoryVersion.created_at)
            )
            return [row[0] for row in got.all()]

    async def test_repeated_reads_do_not_write_a_row(self, client, app):
        """The eviction hazard: a dashboard left open must not be able to push
        out the snapshot a configure is pinned to."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        first = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert first.status_code == 200, first.text
        after_first = await self._versions(inv_id)
        assert after_first == 1

        for _ in range(5):
            again = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
            assert again.status_code == 200, again.text
            # Same answer, same row -- and `taken-at` does not move, because the
            # resolution did not.
            assert again.json()["data"]["id"] == first.json()["data"]["id"]
            assert (
                again.json()["data"]["attributes"]["taken-at"]
                == first.json()["data"]["attributes"]["taken-at"]
            )

        assert await self._versions(inv_id) == after_first, "a read wrote a row"

    async def test_declaring_a_host_moves_the_stamp(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        before = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert before.json()["data"]["attributes"]["host-count"] == 1

        await _declare(client, ws_id, "web-2")
        after = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert after.status_code == 200, after.text
        assert after.json()["data"]["attributes"]["host-count"] == 2
        assert await self._versions(inv_id) == 2

        stamps = await self._stamps(inv_id)
        assert stamps[0] != stamps[1], stamps
        assert all(s for s in stamps), "an API resolution must carry a stamp"

    async def test_DELETING_a_host_moves_the_stamp(self, client, app):
        """The one `max(updated_at)` alone gets wrong, and it fails in the
        direction that matters: a deleted host would go on being served as part
        of the target set, because removing a row leaves the maximum exactly
        where it was. The count is what catches it.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        # Declare the one to delete FIRST, so the surviving host carries the
        # later `updated_at` and the maximum is provably unmoved by the delete.
        doomed = await _declare(client, ws_id, "web-doomed")
        await _declare(client, ws_id, "web-keep")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        before = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert before.json()["data"]["attributes"]["host-count"] == 2

        gone = await client.delete(f"{V1}/inventory-items/{doomed}", headers=AUTH)
        assert gone.status_code in (200, 204), gone.text

        after = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert after.status_code == 200, after.text
        hosts = after.json()["data"]["attributes"]["hosts"]
        assert "web-doomed" not in hosts, "a deleted host was still being served"
        assert after.json()["data"]["attributes"]["host-count"] == 1
        assert await self._versions(inv_id) == 2

    async def test_updating_a_host_moves_the_stamp(self, client, app):
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        item_id = await _declare(client, ws_id, "web-1", address="10.0.0.1")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)

        moved = await client.patch(
            f"{V1}/inventory-items/{item_id}",
            json={"data": {"attributes": {"address": "10.0.0.9"}}},
            headers=AUTH,
        )
        assert moved.status_code == 200, moved.text

        after = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert after.status_code == 200, after.text
        assert after.json()["data"]["attributes"]["hosts"]["web-1"]["ansible_host"] == "10.0.0.9"
        assert await self._versions(inv_id) == 2

    async def test_the_limit_preview_sees_a_change_without_a_resolve(self, client, app):
        """The preview is the safety surface, so it must not be the one place
        still answering from a stale set."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1", groups=["web"])
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        first = await client.post(
            f"{V1}/inventories/inv-{inv_id}/actions/preview-limit",
            json={"data": {"attributes": {"limit": "web"}}},
            headers=AUTH,
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["attributes"]["hosts"] == ["web-1"]

        await _declare(client, ws_id, "web-2", groups=["web"])

        second = await client.post(
            f"{V1}/inventories/inv-{inv_id}/actions/preview-limit",
            json={"data": {"attributes": {"limit": "web"}}},
            headers=AUTH,
        )
        assert second.status_code == 200, second.text
        assert second.json()["data"]["attributes"]["hosts"] == ["web-1", "web-2"]
        assert second.json()["data"]["attributes"]["of-host-count"] == 2

    async def test_an_inventory_with_no_sources_is_stamped_rather_than_unstampable(
        self, client, app
    ):
        """`api_can_resolve([])` is vacuously True, so the stamp must not answer
        "cannot say" for the same input -- the caller would resolve, the empty
        stamp would never match, and a row would be written on every read, which
        is the eviction the stamp exists to prevent.

        Unreachable through the API (an inventory is created with a terraform
        source and no route removes one), so the source row is deleted through
        the ORM to reach it at all. That is the point: the two helpers disagreed
        about one input, and a latent disagreement is worth closing while it is
        still cheap.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        from sqlalchemy import delete

        from terrapod.db.models import InventorySource
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            await db.execute(
                delete(InventorySource).where(InventorySource.inventory_id == uuid.UUID(inv_id))
            )
            await db.commit()

        first = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
        assert first.status_code == 200, first.text
        # No sources resolves to the empty set, deterministically.
        assert first.json()["data"]["attributes"]["host-count"] == 0
        baseline = await self._versions(inv_id)

        for _ in range(3):
            again = await client.get(f"{V1}/inventories/inv-{inv_id}/resolved", headers=AUTH)
            assert again.status_code == 200, again.text

        assert await self._versions(inv_id) == baseline, (
            "a sourceless inventory wrote a row per read"
        )

    async def test_the_resolve_action_is_a_no_op_when_nothing_has_moved(self, client, app):
        """`POST .../actions/resolve` takes the same stamped path a read does.

        It used to write unconditionally. That handed anyone holding write an
        eviction vector for nothing: the history is bounded, so a duplicate row
        prunes the oldest while carrying no information a configure could
        subtract against -- a matching stamp already proves the existing row IS
        the current resolution. The action still exists because it GUARANTEES a
        row describes the current resolution; it does not promise a new one.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        await _declare(client, ws_id, "web-1")
        inv_id = (await _default_inventory(client, ws_id)).removeprefix("inv-")

        first = await client.post(f"{V1}/inventories/inv-{inv_id}/actions/resolve", headers=AUTH)
        assert first.status_code == 200, first.text
        assert await self._versions(inv_id) == 1

        for _ in range(4):
            again = await client.post(
                f"{V1}/inventories/inv-{inv_id}/actions/resolve", headers=AUTH
            )
            assert again.status_code == 200, again.text
            # The same row, returned again -- not a new one with the same bytes.
            assert again.json()["data"]["id"] == first.json()["data"]["id"]

        assert await self._versions(inv_id) == 1, "the action wrote a duplicate row"

        # And it still writes when there is something to record, or the
        # guarantee it exists for would be hollow.
        await _declare(client, ws_id, "web-2")
        moved = await client.post(f"{V1}/inventories/inv-{inv_id}/actions/resolve", headers=AUTH)
        assert moved.status_code == 200, moved.text
        assert moved.json()["data"]["id"] != first.json()["data"]["id"]
        assert await self._versions(inv_id) == 2
