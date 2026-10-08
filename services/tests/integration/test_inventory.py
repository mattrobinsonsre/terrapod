"""The eight inventory structures against a real engine (#1967, #1968).

The integration tier because the properties here are the engine's, not the
code's. A mocked session answers from its fixture, which is how a missing
constraint ships looking tested.

Four that matter most, and all four are reasons the schema is shaped this way:

* **a cross-workspace link is refused BY THE DATABASE.** Every link is a
  composite foreign key `(workspace_id, <parent>_id)` against a
  `UNIQUE (workspace_id, id)`, so putting one workspace's host in another's
  group is impossible rather than merely unlikely. The service deliberately does
  not check first -- a `SELECT` before the insert is check-then-act, and two
  concurrent applies could pass it together.
* **a duplicate is a 409 and a dangling parent is a 422.** Both read SQLSTATE
  off the driver exception, so they only mean anything when a real constraint
  raises. The 409 is what tells a practitioner to import the row that exists;
  a 409 for the dangling case would send them looking for a collision.
* **the cascades reach the right things and stop at the right places.** Deleting
  a group ungroups its hosts rather than deleting them; deleting a host takes
  its memberships and variables with it.
* **a variable value is encrypted at rest.** Read back through the API it is the
  value that was written; read straight out of the table it is ciphertext.
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


async def _host(client, ws_id: str, name: str):
    return await client.post(
        f"{V1}/workspaces/{ws_id}/inventory/hosts",
        json={"data": {"type": "inventory-hosts", "attributes": {"name": name}}},
        headers=AUTH,
    )


async def _mk_host(client, ws_id: str, name: str) -> str:
    resp = await _host(client, ws_id, name)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _group(client, ws_id: str, name: str):
    return await client.post(
        f"{V1}/workspaces/{ws_id}/inventory/groups",
        json={"data": {"type": "inventory-groups", "attributes": {"name": name}}},
        headers=AUTH,
    )


async def _mk_group(client, ws_id: str, name: str) -> str:
    resp = await _group(client, ws_id, name)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _member(client, group_id: str, host_id: str):
    return await client.post(
        f"{V1}/inventory-groups/{group_id}/hosts",
        json={
            "data": {
                "type": "inventory-host-groups",
                "relationships": {"host": {"data": {"id": host_id, "type": "inventory-hosts"}}},
            }
        },
        headers=AUTH,
    )


async def _child(client, parent_id: str, child_id: str):
    return await client.post(
        f"{V1}/inventory-groups/{parent_id}/children",
        json={
            "data": {
                "type": "inventory-group-children",
                "relationships": {
                    "child-group": {"data": {"id": child_id, "type": "inventory-groups"}}
                },
            }
        },
        headers=AUTH,
    )


async def _host_var(client, host_id: str, key: str, value: str = "v", **attrs):
    return await client.post(
        f"{V1}/inventory-hosts/{host_id}/vars",
        json={
            "data": {
                "type": "inventory-host-vars",
                "attributes": {"key": key, "value": value, **attrs},
            }
        },
        headers=AUTH,
    )


def _no_ansible() -> bool:
    """Whether a WORKING ansible-inventory is absent.

    Probed rather than located: a stale shim whose module is gone answers
    `--version` and dies on everything else.
    """
    import shutil
    import subprocess

    found = shutil.which("ansible-inventory")
    if found is None:
        return True
    try:
        probe = subprocess.run([found, "--version"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    return probe.returncode != 0


class TestTheCompositeKeysRefuseACrossWorkspaceLink:
    """The property the whole schema is shaped around.

    Not "the service checks" -- the service deliberately does not, because a
    check before the insert is a race two concurrent applies can win together.
    These assert the DATABASE refuses it.
    """

    async def test_a_host_cannot_join_a_group_in_another_workspace(self, client, app):
        set_auth(app, admin_user())
        ws_a = await _workspace(client)
        ws_b = await _workspace(client)
        host = await _mk_host(client, ws_a, "web-1")
        foreign_group = await _mk_group(client, ws_b, "web")

        resp = await _member(client, foreign_group, host)

        # 422, not 409: nothing collided. The group exists, just not here, and a
        # 409 would send the caller looking for a duplicate that is not there.
        assert resp.status_code == 422, resp.text
        assert "does not exist in this workspace" in resp.json()["detail"]

    async def test_a_group_cannot_nest_a_group_from_another_workspace(self, client, app):
        set_auth(app, admin_user())
        ws_a = await _workspace(client)
        ws_b = await _workspace(client)
        parent = await _mk_group(client, ws_a, "prod")
        foreign_child = await _mk_group(client, ws_b, "web")

        resp = await _child(client, parent, foreign_child)
        assert resp.status_code == 422, resp.text

    async def test_a_variable_cannot_attach_to_a_host_in_another_workspace(self, client, app):
        """Reached through the host's own route, so the path's workspace is the
        one the row gets -- which is exactly the case the composite key exists
        to catch, because the host id alone looks perfectly valid."""
        set_auth(app, admin_user())
        ws_a = await _workspace(client)
        ws_b = await _workspace(client)
        await _mk_host(client, ws_a, "web-1")
        foreign_host = await _mk_host(client, ws_b, "web-1")

        # The host exists and is addressable; it is simply not in ws_a. The
        # route resolves the workspace FROM the host, so this one succeeds --
        # and that is the point: there is no way to express the broken row.
        resp = await _host_var(client, foreign_host, "ansible_user")
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["relationships"]["workspace"]["data"]["id"] == ws_b


class TestTheUniqueConstraints:
    async def test_a_duplicate_host_name_is_a_409_not_a_500(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        await _mk_host(client, ws, "web-1")

        resp = await _host(client, ws, "web-1")
        assert resp.status_code == 409, resp.text
        assert "already declared" in resp.json()["detail"]

    async def test_the_same_host_name_in_another_workspace_is_fine(self, client, app):
        """The constraint is per workspace, which is what makes two deployments
        of the same stack able to use the same host names."""
        set_auth(app, admin_user())
        ws_a = await _workspace(client)
        ws_b = await _workspace(client)
        await _mk_host(client, ws_a, "web-1")
        resp = await _host(client, ws_b, "web-1")
        assert resp.status_code == 201, resp.text

    async def test_a_duplicate_membership_is_a_409(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        group = await _mk_group(client, ws, "web")
        assert (await _member(client, group, host)).status_code == 201

        resp = await _member(client, group, host)
        assert resp.status_code == 409, resp.text

    async def test_a_duplicate_variable_key_on_one_host_is_a_409(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        assert (await _host_var(client, host, "ansible_user")).status_code == 201

        resp = await _host_var(client, host, "ansible_user")
        assert resp.status_code == 409, resp.text

    async def test_the_same_variable_key_on_two_hosts_is_fine(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        a = await _mk_host(client, ws, "web-1")
        b = await _mk_host(client, ws, "web-2")
        assert (await _host_var(client, a, "ansible_user")).status_code == 201
        assert (await _host_var(client, b, "ansible_user")).status_code == 201


class TestTheCascades:
    """What a delete reaches, and -- as much to the point -- what it does not."""

    async def test_deleting_a_host_takes_its_memberships_and_variables(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        group = await _mk_group(client, ws, "web")
        link = (await _member(client, group, host)).json()["data"]["id"]
        var = (await _host_var(client, host, "ansible_user")).json()["data"]["id"]

        assert (
            await client.delete(f"{V1}/inventory-hosts/{host}", headers=AUTH)
        ).status_code == 204

        assert (
            await client.get(f"{V1}/inventory-host-groups/{link}", headers=AUTH)
        ).status_code == 404
        assert (
            await client.get(f"{V1}/inventory-host-vars/{var}", headers=AUTH)
        ).status_code == 404
        # The group survives: it is not owned by its members.
        assert (await client.get(f"{V1}/inventory-groups/{group}", headers=AUTH)).status_code == 200

    async def test_deleting_a_group_ungroups_its_hosts_rather_than_deleting_them(self, client, app):
        """The hazard this guards is quiet. A host belongs to the workspace, so
        CASCADE from a group would let removing one grouping delete machines."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        group = await _mk_group(client, ws, "web")
        await _member(client, group, host)

        assert (
            await client.delete(f"{V1}/inventory-groups/{group}", headers=AUTH)
        ).status_code == 204

        still = await client.get(f"{V1}/inventory-hosts/{host}", headers=AUTH)
        assert still.status_code == 200, still.text
        assert still.json()["data"]["attributes"]["group-count"] == 0

    async def test_deleting_a_group_takes_its_nestings_from_both_sides(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        parent = await _mk_group(client, ws, "prod")
        middle = await _mk_group(client, ws, "web")
        leaf = await _mk_group(client, ws, "frontend")
        up = (await _child(client, parent, middle)).json()["data"]["id"]
        down = (await _child(client, middle, leaf)).json()["data"]["id"]

        await client.delete(f"{V1}/inventory-groups/{middle}", headers=AUTH)

        # Both the nesting it was a child in and the one it was a parent of.
        for link in (up, down):
            gone = await client.get(f"{V1}/inventory-group-children/{link}", headers=AUTH)
            assert gone.status_code == 404, f"{link} survived"
        assert (
            await client.get(f"{V1}/inventory-groups/{parent}", headers=AUTH)
        ).status_code == 200
        assert (await client.get(f"{V1}/inventory-groups/{leaf}", headers=AUTH)).status_code == 200

    async def test_deleting_the_workspace_removes_every_inventory_row(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        group = await _mk_group(client, ws, "web")
        await _member(client, group, host)
        await _child(client, group, await _mk_group(client, ws, "frontend"))
        await _host_var(client, host, "ansible_user")
        await client.post(
            f"{V1}/workspaces/{ws}/inventory/vars",
            json={"data": {"attributes": {"key": "ansible_python_interpreter", "value": "/x"}}},
            headers=AUTH,
        )

        gone = await client.delete(f"{V1}/workspaces/{ws}", headers=AUTH)
        assert gone.status_code in (200, 204), gone.text

        from sqlalchemy import func, select

        from terrapod.db.models import (
            InventoryGlobalVar,
            InventoryGroup,
            InventoryGroupChild,
            InventoryGroupVar,
            InventoryHost,
            InventoryHostGroup,
            InventoryHostVar,
            InventorySettings,
        )
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            for model in (
                InventorySettings,
                InventoryHost,
                InventoryGroup,
                InventoryHostGroup,
                InventoryGroupChild,
                InventoryHostVar,
                InventoryGroupVar,
                InventoryGlobalVar,
            ):
                count = await db.execute(select(func.count()).select_from(model))
                assert count.scalar() == 0, f"{model.__tablename__} survived"


class TestTheCycleGuard:
    """Refused for the operator's sake, not to protect a traversal of ours.

    Terrapod does not walk the group graph -- ansible does -- so this exists
    because ansible's behaviour on a cyclic inventory is not something an
    operator should have to discover, and the write is the only place we can say
    so with both group names in hand.
    """

    async def test_a_group_cannot_be_its_own_child(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        group = await _mk_group(client, ws, "web")
        resp = await _child(client, group, group)
        assert resp.status_code in (409, 422), resp.text

    async def test_a_longer_cycle_is_refused_too(self, client, app):
        """The CHECK constraint only catches the one-step case, because a CHECK
        cannot walk a graph. This is the service's half."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        a = await _mk_group(client, ws, "a")
        b = await _mk_group(client, ws, "b")
        c = await _mk_group(client, ws, "c")
        assert (await _child(client, a, b)).status_code == 201
        assert (await _child(client, b, c)).status_code == 201

        # c -> a would close a -> b -> c -> a.
        resp = await _child(client, c, a)
        assert resp.status_code == 422, resp.text
        assert "cycle" in resp.json()["detail"]

    async def test_the_check_constraint_is_a_4xx_backstop_not_a_500(self, client, app):
        """With the service guard bypassed, the CHECK still answers 4xx.

        The two layers are deliberate -- the service refuses with a message
        naming the groups, the CHECK refuses whatever reaches the table -- and
        a backstop that answers 500 blames us for the caller's input. This
        found a real gap: the CHECK fired correctly and SQLSTATE 23514 was not
        translated, so it was a 500.
        """
        from unittest.mock import AsyncMock, patch

        set_auth(app, admin_user())
        ws = await _workspace(client)
        group = await _mk_group(client, ws, "web")

        with patch(
            "terrapod.services.inventory_service._would_cycle",
            AsyncMock(return_value=False),
        ):
            resp = await _child(client, group, group)

        assert resp.status_code == 422, resp.text

    async def test_a_diamond_is_not_a_cycle(self, client, app):
        """Two parents reaching one child is ordinary ansible, so the guard must
        not refuse it -- which a naive "is it already reachable" check would."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        left = await _mk_group(client, ws, "left")
        right = await _mk_group(client, ws, "right")
        shared = await _mk_group(client, ws, "shared")
        assert (await _child(client, left, shared)).status_code == 201
        assert (await _child(client, right, shared)).status_code == 201


class TestVariableValuesAreEncryptedAtRest:
    """The composition, proven end to end against the real column and engine.

    Two gates already cover the halves -- `tests/db/test_credential_columns_
    encrypted.py` asserts these three columns are `EncryptedText` and are
    registered in `ENCRYPTED_COLUMNS`, and `tests/crypto/` asserts the type
    encrypts when encryption is on. This asserts they compose: a value written
    through the model lands as ciphertext in the table and reads back intact.

    **Encryption is OFF by default, so the test has to turn it on.** The first
    version of this asserted ciphertext without doing that and passed for the
    wrong reason -- `EncryptedText` is a pure passthrough when disabled, so it
    was asserting the ambient configuration rather than the column.
    """

    async def test_a_value_is_ciphertext_in_the_table_and_intact_through_the_model(
        self, client, app, monkeypatch
    ):
        from terrapod.crypto import envelope
        from terrapod.crypto import service as svc_mod

        class _StaticProvider:
            """Wraps and unwraps one DEK, which is all the column needs."""

            id = "static"

            def __init__(self):
                self._dek = envelope.new_dek()

            async def wrap(self, dek):
                self._dek = dek
                return "wrapped"

            async def unwrap(self, wrapped):
                return self._dek

        set_auth(app, admin_user())
        ws_id = await _workspace(client)
        host_id = await _mk_host(client, ws_id, "web-1")

        from sqlalchemy import text

        from terrapod.db.session import get_db_session

        secret = "s3cret-" + uuid.uuid4().hex
        monkeypatch.setattr(svc_mod.settings.encryption, "enabled", True)
        monkeypatch.setattr(svc_mod, "build_provider", lambda: _StaticProvider())
        svc_mod.reset_encryption_for_tests()
        try:
            async with get_db_session() as db:
                await svc_mod.init_encryption(db)
                assert svc_mod.get_encryption().enabled is True

                from terrapod.api.ids import strip_id_prefix
                from terrapod.services import inventory_service as inv

                var = await inv.create_host_var(
                    db,
                    workspace_id=uuid.UUID(strip_id_prefix(ws_id, "ws-")),
                    host_id=uuid.UUID(strip_id_prefix(host_id, "invhost-")),
                    key="ansible_become_password",
                    value=secret,
                )
                await db.commit()
                var_id = var.id

            # Straight out of the column, bypassing the type.
            async with get_db_session() as db:
                raw = await db.execute(
                    text("SELECT value FROM inventory_host_vars WHERE id = :i"), {"i": var_id}
                )
                stored = raw.scalar_one()

            assert stored != secret, "the value is plaintext in the table"
            assert secret not in stored

            # And back through the type, so the encryption is real rather than
            # merely destructive.
            async with get_db_session() as db:
                back = await inv.get_host_var(db, var_id)
                assert back is not None
                assert back.value == secret
        finally:
            # The singleton is process-wide, so leaving it enabled would make
            # every later test in this process encrypt -- and the next one to
            # read a row written before this test would fail on a value it
            # cannot decrypt.
            svc_mod.reset_encryption_for_tests()

    async def test_a_sensitive_value_is_masked_on_read(self, client, app):
        """`sensitive` is a DISPLAY flag and nothing more.

        A column cannot be conditionally encrypted, so every value is encrypted
        and this flag decides only what a reader sees.
        """
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        created = await _host_var(client, host, "ansible_password", "hunter2", sensitive=True)
        assert created.status_code == 201, created.text
        attrs = created.json()["data"]["attributes"]
        assert attrs["value"] == "***"
        assert attrs["sensitive"] is True


class TestTheOptionsAreSymmetric:
    """Both sides of a link can create it, and both shapes of settings write
    exist. Pinned because the narrower version of each was the first thing
    written, and a surface that only works one way reads as an oversight to
    whoever is iterating the other."""

    async def test_a_membership_can_be_created_from_the_host_side(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        group = await _mk_group(client, ws, "web")

        resp = await client.post(
            f"{V1}/inventory-hosts/{host}/groups",
            json={
                "data": {
                    "relationships": {"group": {"data": {"id": group, "type": "inventory-groups"}}}
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        rels = resp.json()["data"]["relationships"]
        assert rels["host"]["data"]["id"] == host
        assert rels["group"]["data"]["id"] == group

    async def test_a_nesting_can_be_created_from_the_child_side(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        parent = await _mk_group(client, ws, "prod")
        child = await _mk_group(client, ws, "web")

        resp = await client.post(
            f"{V1}/inventory-groups/{child}/parents",
            json={
                "data": {
                    "relationships": {
                        "parent-group": {"data": {"id": parent, "type": "inventory-groups"}}
                    }
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        rels = resp.json()["data"]["relationships"]
        assert rels["parent-group"]["data"]["id"] == parent
        assert rels["child-group"]["data"]["id"] == child

    async def test_settings_can_be_patched_as_well_as_replaced(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)

        put = await client.put(
            f"{V1}/workspaces/{ws}/inventory/settings",
            json={"data": {"attributes": {"include-platform": True, "branch": "main"}}},
            headers=AUTH,
        )
        assert put.status_code == 200, put.text

        # One field, without resending the rest.
        patch = await client.patch(
            f"{V1}/workspaces/{ws}/inventory/settings",
            json={"data": {"attributes": {"include-platform": False}}},
            headers=AUTH,
        )
        assert patch.status_code == 200, patch.text
        attrs = patch.json()["data"]["attributes"]
        assert attrs["include-platform"] is False
        assert attrs["branch"] == "main", "a patch must not clobber what it did not mention"

    async def test_a_variable_key_can_be_renamed(self, client, app):
        """Refusing this would only make a caller delete and recreate to get the
        same result, losing the row id for nothing."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        var_id = (await _host_var(client, host, "old_name", "v")).json()["data"]["id"]

        resp = await client.patch(
            f"{V1}/inventory-host-vars/{var_id}",
            json={"data": {"attributes": {"key": "new_name"}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["key"] == "new_name"
        # Same row, so the id survives -- which is the whole gain over
        # delete-and-recreate.
        assert resp.json()["data"]["id"] == var_id

    async def test_renaming_onto_an_existing_key_is_a_409(self, client, app):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        await _host_var(client, host, "taken", "v")
        var_id = (await _host_var(client, host, "free", "v")).json()["data"]["id"]

        resp = await client.patch(
            f"{V1}/inventory-host-vars/{var_id}",
            json={"data": {"attributes": {"key": "taken"}}},
            headers=AUTH,
        )
        assert resp.status_code == 409, resp.text


class TestDerivedGroupNamesAreRefused:
    @pytest.mark.parametrize("name", ["all", "ungrouped"])
    async def test_a_derived_name_cannot_be_declared(self, client, app, name):
        set_auth(app, admin_user())
        ws = await _workspace(client)
        resp = await _group(client, ws, name)
        assert resp.status_code == 422, resp.text
        assert "derived by ansible" in resp.json()["detail"]

    async def test_inventory_wide_variables_have_their_own_route_instead(self, client, app):
        """What the refusal above points at. `group_vars/all` is a real ansible
        structure, so it gets a real surface rather than being unavailable."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        resp = await client.post(
            f"{V1}/workspaces/{ws}/inventory/vars",
            json={
                "data": {
                    "attributes": {"key": "ansible_python_interpreter", "value": "/usr/bin/python3"}
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        listed = await client.get(f"{V1}/workspaces/{ws}/inventory/vars", headers=AUTH)
        assert [v["attributes"]["key"] for v in listed.json()["data"]] == [
            "ansible_python_interpreter"
        ]


class TestTheSettingsCheckConstraint:
    async def test_a_repo_without_a_connection_is_refused(self, client, app):
        """The CHECK says this too; the service refuses first so the message
        names the field rather than surfacing a constraint name."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        resp = await client.put(
            f"{V1}/workspaces/{ws}/inventory/settings",
            json={"data": {"attributes": {"repo-url": "https://example.com/r.git"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text
        assert "VCS connection" in resp.json()["detail"]

    async def test_settings_are_absent_until_written(self, client, app):
        """A 404 rather than a defaults object, so a client can tell "unset"
        from "set to the defaults" -- which is the distinction the provider
        needs to decide whether it owns the row."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        resp = await client.get(f"{V1}/workspaces/{ws}/inventory/settings", headers=AUTH)
        assert resp.status_code == 404, resp.text


class TestTheResolvedRead:
    """End to end through the real router, the real database and real ansible.

    The services tier drives `ansible-inventory` over a rendered fixture; this
    drives the whole path -- rows written through the API, rendered, resolved,
    cached in Redis and served -- because the rendering is the half that can
    disagree with the rows.
    """

    async def test_declared_rows_resolve_to_hosts_and_groups(self, client, app):
        if _no_ansible():
            pytest.skip("no working ansible-inventory; the test image provides one")
        set_auth(app, admin_user())
        ws = await _workspace(client)
        web = await _mk_host(client, ws, "web-1")
        bare = await _mk_host(client, ws, "switch-1")
        group = await _mk_group(client, ws, "web")
        await _member(client, group, web)
        await _host_var(client, web, "ansible_host", "10.0.0.4")

        resp = await client.get(f"{V1}/workspaces/{ws}/inventory/resolved", headers=AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]

        # The var-less host survives, which is the whole reason the host set is
        # not read off `_meta.hostvars`.
        assert sorted(attrs["hosts"]) == ["switch-1", "web-1"]
        assert attrs["hosts"]["web-1"]["ansible_host"] == "10.0.0.4"
        assert attrs["groups"]["web"] == ["web-1"]
        assert attrs["host-count"] == 2
        assert bare  # the id is not otherwise used; naming it documents the fixture

    async def test_an_inventory_wide_variable_reaches_every_host(self, client, app):
        """`group_vars/all`, which is what refusing `all` as a group name buys."""
        if _no_ansible():
            pytest.skip("no working ansible-inventory")
        set_auth(app, admin_user())
        ws = await _workspace(client)
        await _mk_host(client, ws, "a")
        await _mk_host(client, ws, "b")
        await client.post(
            f"{V1}/workspaces/{ws}/inventory/vars",
            json={"data": {"attributes": {"key": "ansible_user", "value": "deploy"}}},
            headers=AUTH,
        )

        attrs = (await client.get(f"{V1}/workspaces/{ws}/inventory/resolved", headers=AUTH)).json()[
            "data"
        ]["attributes"]
        for host in ("a", "b"):
            assert attrs["hosts"][host]["ansible_user"] == "deploy"

    async def test_a_structured_variable_arrives_as_its_type(self, client, app):
        """`structured` is the same question `Variable.structured` answers: a
        list or a number rather than a string, which is what ansible's own
        `group_vars` carries natively."""
        if _no_ansible():
            pytest.skip("no working ansible-inventory")
        set_auth(app, admin_user())
        ws = await _workspace(client)
        host = await _mk_host(client, ws, "web-1")
        await _host_var(client, host, "ports", "[80, 443]", structured=True)

        attrs = (await client.get(f"{V1}/workspaces/{ws}/inventory/resolved", headers=AUTH)).json()[
            "data"
        ]["attributes"]
        assert attrs["hosts"]["web-1"]["ports"] == [80, 443]

    async def test_a_limit_narrows_the_host_set(self, client, app):
        """The safety surface. Auto-configure is deliberately broad, so
        visibility is the control -- "what would this target" has to be
        answerable before anything runs."""
        if _no_ansible():
            pytest.skip("no working ansible-inventory")
        set_auth(app, admin_user())
        ws = await _workspace(client)
        for name in ("web-1", "web-2", "db-1"):
            host = await _mk_host(client, ws, name)
            if name.startswith("web"):
                pass
        group = await _mk_group(client, ws, "web")
        for name in ("web-1", "web-2"):
            hosts = (
                await client.get(f"{V1}/workspaces/{ws}/inventory/hosts", headers=AUTH)
            ).json()["data"]
            hid = next(h["id"] for h in hosts if h["attributes"]["name"] == name)
            await _member(client, group, hid)

        narrowed = await client.get(
            f"{V1}/workspaces/{ws}/inventory/resolved?limit=web:!web-2", headers=AUTH
        )
        assert narrowed.status_code == 200, narrowed.text
        attrs = narrowed.json()["data"]["attributes"]
        assert sorted(attrs["hosts"]) == ["web-1"]
        assert attrs["limit"] == "web:!web-2"
        assert host  # fixture

    async def test_a_workspace_with_nothing_resolves_to_the_empty_set(self, client, app):
        """Not a 404 and not an error: the empty set is a real resolution, and
        it is the answer at exactly the moment an operator is checking what they
        have just declared."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        resp = await client.get(f"{V1}/workspaces/{ws}/inventory/resolved", headers=AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs == {
            "hosts": {},
            "groups": {},
            "group-children": {},
            "host-count": 0,
            "group-count": 0,
        }

    async def test_the_read_carries_no_freshness_field(self, client, app):
        """Pinned, because the field it replaced was read as staleness. The read
        resolved the rows to answer itself, so a date would only invite a reader
        to ask a question the read has already answered."""
        set_auth(app, admin_user())
        ws = await _workspace(client)
        attrs = (await client.get(f"{V1}/workspaces/{ws}/inventory/resolved", headers=AUTH)).json()[
            "data"
        ]["attributes"]
        for gone in ("taken-at", "produced-by", "api-resolvable"):
            assert gone not in attrs
