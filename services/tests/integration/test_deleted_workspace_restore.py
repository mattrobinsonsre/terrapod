"""Restoring a deleted workspace (#1253 slice 2), against real Postgres + storage.

This needs the real engine rather than mocks: restore inserts a `Workspace`
plus one `StateVersion` per recovered blob under a `(workspace_id, serial)`
unique constraint, and the interesting failure modes — a duplicate serial
aborting the whole transaction, a name collision, lineage not surviving the
round trip — are all properties of the database and the object store rather
than of the code's control flow.

The load-bearing assertion is `test_restore_preserves_lineage_and_serial`.
Everything else about a restored workspace can be fixed by editing it; a lost
or altered lineage cannot, because the next apply either fails on a mismatch
or treats live infrastructure as unmanaged.
"""

import json

import pytest

from terrapod.services import deleted_workspace_service as dws
from terrapod.storage import get_storage
from tests.integration.conftest import AUTH, admin_user, regular_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


def _state_doc(serial: int, lineage: str) -> bytes:
    return json.dumps(
        {"version": 4, "terraform_version": "1.12.0", "serial": serial, "lineage": lineage}
    ).encode()


async def _delete_with_state(client, name: str, serials: list[int], lineage: str) -> str:
    """Create a workspace, give it state versions, delete it. Returns its raw id."""
    create = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": name,
                    "auto-apply": True,
                    "drift-detection-enabled": True,
                    "labels": {"team": "platform"},
                },
            }
        },
        headers=AUTH,
    )
    assert create.status_code == 201, create.text
    ws_id = create.json()["data"]["id"]
    raw_id = ws_id.removeprefix("ws-")

    for serial in serials:
        body = _state_doc(serial, lineage)
        sv = await client.post(
            f"/api/v2/workspaces/{ws_id}/state-versions",
            json={
                "data": {
                    "type": "state-versions",
                    "attributes": {
                        "serial": serial,
                        "lineage": lineage,
                        "md5": __import__("hashlib").md5(body).hexdigest(),
                    },
                }
            },
            headers=AUTH,
        )
        assert sv.status_code == 201, sv.text
        up = await client.put(
            f"/api/v2/state-versions/{sv.json()['data']['id']}/content", content=body
        )
        assert up.status_code == 200

    delete = await client.delete(f"/api/terrapod/v1/workspaces/{ws_id}", headers=AUTH)
    assert delete.status_code == 204
    return raw_id


class TestRestore:
    async def test_restore_preserves_lineage_and_serial(self, app, client):
        """The one hard correctness constraint.

        A restored workspace must continue the original state, so both the
        lineage and every serial have to survive — recovered from inside the
        state documents, since the rows that recorded them were CASCADEd away
        with the workspace.
        """
        set_auth(app, admin_user())
        lineage = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        old_id = await _delete_with_state(client, "restore-lineage", [1, 2, 3], lineage)

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["state-versions-restored"] == 3

        new_id = resp.json()["data"]["id"]
        listing = await client.get(f"/api/v2/workspaces/{new_id}/state-versions", headers=AUTH)
        assert listing.status_code == 200
        versions = listing.json()["data"]
        assert [v["attributes"]["serial"] for v in versions] == [3, 2, 1]
        assert {v["attributes"]["lineage"] for v in versions} == {lineage}

        # And the state itself round-trips byte-for-byte, decrypting on the way
        # out if encryption is on.
        current = await client.get(
            f"/api/v2/workspaces/{new_id}/current-state-version", headers=AUTH
        )
        assert current.json()["data"]["attributes"]["serial"] == 3
        dl = await client.get(
            f"/api/v2/state-versions/{current.json()['data']['id']}/download", headers=AUTH
        )
        assert json.loads(dl.content)["lineage"] == lineage

    async def test_restore_is_a_new_workspace_not_a_revival(self, app, client):
        """Restore produces a NEW id and leaves the original prefix alone.

        Deliberate: re-attaching the original id would need no copy at all and
        would be a one-click undo. Deletion is meant to stay consequential, so
        recovery is an explicit operation that yields a visibly new workspace.
        """
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-newid", [1], "lin-newid")

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 201
        new_id = resp.json()["data"]["id"].removeprefix("ws-")
        assert new_id != old_id
        assert resp.json()["data"]["attributes"]["restored-from"] == old_id

        # The source prefix survives, so the marker still governs it and the
        # restore can be repeated or discarded.
        storage = get_storage()
        assert [o.key for o in await storage.list_prefix(f"state/{old_id}/")]
        assert await dws.read_marker(storage, old_id) is not None

    async def test_restored_workspace_comes_back_inert(self, app, client):
        """Auto-apply and drift are forced off however the snapshot was set.

        A workspace that applies the moment it is restored, against infra that
        may have drifted or been partly torn down since the delete, is the
        worst outcome this feature could produce.
        """
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-inert", [1], "lin-inert")

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 201
        suppressed = resp.json()["data"]["attributes"]["suppressed"]
        assert "auto_apply" in suppressed
        assert "drift_detection_enabled" in suppressed

        new_id = resp.json()["data"]["id"]
        ws = await client.get(f"/api/v2/workspaces/{new_id}", headers=AUTH)
        assert ws.json()["data"]["attributes"]["auto-apply"] is False

        # Non-dangerous settings ARE carried over — the restore is still useful.
        assert ws.json()["data"]["attributes"]["labels"] == {"team": "platform"}

    async def test_restore_renames_when_the_name_is_taken(self, app, client):
        """The original name is usually free, but must never block a restore."""
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-collide", [1], "lin-collide")

        # Someone recreates a workspace under the freed name.
        again = await client.post(
            WS_ENDPOINT,
            json={"data": {"type": "workspaces", "attributes": {"name": "restore-collide"}}},
            headers=AUTH,
        )
        assert again.status_code == 201

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["name"] != "restore-collide"
        assert resp.json()["data"]["attributes"]["name"].startswith("restore-collide")

    async def test_restore_honours_an_explicit_name(self, app, client):
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-named", [1], "lin-named")

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore",
            json={"data": {"attributes": {"name": "recovered-by-hand"}}},
            headers=AUTH,
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["name"] == "recovered-by-hand"

    async def test_restore_with_no_recoverable_state_is_a_conflict(self, app, client):
        """An empty restore is a failure, not an empty success.

        Otherwise the operator is handed a bare workspace and told the restore
        worked, having to discover for themselves that no state came back.
        """
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-empty", [1], "lin-empty")

        storage = get_storage()
        for obj in await storage.list_prefix(f"state/{old_id}/"):
            await storage.delete(obj.key)

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 409

        # ...and no half-made workspace is left behind.
        listing = await client.get(
            WS_ENDPOINT, params={"search[name]": "restore-empty"}, headers=AUTH
        )
        assert [w["attributes"]["name"] for w in listing.json()["data"]] == []

    async def test_restore_of_an_unknown_workspace_is_404(self, app, client):
        set_auth(app, admin_user())
        resp = await client.post(
            "/api/terrapod/v1/deleted-workspaces/8f1c2b3a-0000-4000-8000-000000000000/restore",
            headers=AUTH,
        )
        assert resp.status_code == 404

    async def test_backup_blobs_are_not_restored_as_versions(self, app, client):
        """`{id}.backup.tfstate` shares the prefix but is not a version.

        Restoring one would fabricate history that never existed.
        """
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "restore-backup", [1], "lin-backup")

        storage = get_storage()
        await storage.put(
            f"state/{old_id}/00000000-0000-4000-8000-000000000001.backup.tfstate",
            _state_doc(99, "lin-backup"),
            content_type="application/json",
        )

        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["state-versions-restored"] == 1

        new_id = resp.json()["data"]["id"]
        listing = await client.get(f"/api/v2/workspaces/{new_id}/state-versions", headers=AUTH)
        assert [v["attributes"]["serial"] for v in listing.json()["data"]] == [1]


class TestDeletedWorkspaceList:
    async def test_deleted_workspace_appears_in_the_list(self, app, client):
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "list-me", [1, 2], "lin-list")

        resp = await client.get("/api/terrapod/v1/deleted-workspaces", headers=AUTH)
        assert resp.status_code == 200
        entry = next(d for d in resp.json()["data"] if d["id"] == old_id)
        assert entry["attributes"]["workspace-name"] == "list-me"
        assert entry["attributes"]["state-versions-available"] == 2
        assert entry["attributes"]["restorable-until"]

    async def test_the_list_never_carries_variable_values(self, app, client):
        """The marker records variable NAMES so an operator knows what to
        recreate. Values must never reach it — it is a plain object in the
        bucket, replicated to any standby."""
        set_auth(app, admin_user())
        create = await client.post(
            WS_ENDPOINT,
            json={"data": {"type": "workspaces", "attributes": {"name": "list-secrets"}}},
            headers=AUTH,
        )
        ws_id = create.json()["data"]["id"]
        var = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "api_token",
                        "value": "SUPERSECRETVALUE",
                        "category": "env",
                        "sensitive": True,
                    },
                }
            },
            headers=AUTH,
        )
        assert var.status_code == 201, var.text
        await client.delete(f"/api/terrapod/v1/workspaces/{ws_id}", headers=AUTH)

        resp = await client.get("/api/terrapod/v1/deleted-workspaces", headers=AUTH)
        assert "SUPERSECRETVALUE" not in resp.text
        entry = next(d for d in resp.json()["data"] if d["id"] == ws_id.removeprefix("ws-"))
        names = entry["attributes"]["variable-names"]
        assert [v["key"] for v in names] == ["api_token"]
        assert all("value" not in v for v in names)

    async def test_a_restored_id_still_lists_because_the_source_is_untouched(self, app, client):
        """Restore copies rather than moves, so the original stays recoverable
        until its retention window expires."""
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "list-after-restore", [1], "lin-after")
        await client.post(f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH)

        resp = await client.get("/api/terrapod/v1/deleted-workspaces", headers=AUTH)
        assert any(d["id"] == old_id for d in resp.json()["data"])


class TestDeletedWorkspaceRBAC:
    """Admin-only on every route, reads included.

    A marker names a workspace, its labels and its variable names; a restore
    materialises its state — and therefore its secrets — into a workspace the
    caller can then read. The original workspace's ACL died with its rows, so
    there is nothing to delegate to and no way to check what the caller used
    to be allowed. It fails closed to platform admin.
    """

    async def test_non_admin_cannot_list(self, app, client):
        set_auth(app, admin_user())
        await _delete_with_state(client, "rbac-list", [1], "lin-rbac")

        set_auth(app, regular_user())
        resp = await client.get("/api/terrapod/v1/deleted-workspaces", headers=AUTH)
        assert resp.status_code == 403

    async def test_non_admin_cannot_read_one(self, app, client):
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "rbac-read", [1], "lin-rbac2")

        set_auth(app, regular_user())
        resp = await client.get(f"/api/terrapod/v1/deleted-workspaces/{old_id}", headers=AUTH)
        assert resp.status_code == 403

    async def test_non_admin_cannot_restore(self, app, client):
        set_auth(app, admin_user())
        old_id = await _delete_with_state(client, "rbac-restore", [1], "lin-rbac3")

        set_auth(app, regular_user())
        resp = await client.post(
            f"/api/terrapod/v1/deleted-workspaces/{old_id}/restore", headers=AUTH
        )
        assert resp.status_code == 403

        # And nothing was created as a side effect of the rejected call.
        set_auth(app, admin_user())
        listing = await client.get(
            WS_ENDPOINT, params={"search[name]": "rbac-restore"}, headers=AUTH
        )
        assert listing.json()["data"] == []
