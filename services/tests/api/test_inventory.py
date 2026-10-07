"""The inventory routers: authorization, phase binding and request validation.

#1967 (the inventory object and its snapshots) and #1968 (declared items).

The interesting half is authorization, because this router has **two** kinds of
caller and one of them carries a new implicit grant. The cases that matter:

* a runner token may manage its own run's workspace and **nothing else** -- the
  cross-workspace case is the one that would be a vulnerability rather than a
  bug, so it is pinned directly;
* writes are bound to the **apply** phase while reads are unphased, because a
  plan reads inventory to diff it and never writes;
* a token carrying **no** phase claim passes any phase, matching how
  `require_runner_for_run` treats a listener older than the claim -- refusing it
  would break every run on a lagging listener image.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db
from terrapod.services.inventory_resolution import InventoryValidationError

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_R = "terrapod.api.routers.inventory"


def _user(
    *,
    email="test@example.com",
    roles=None,
    auth_method="session",
    run_id=None,
    run_phase=None,
):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method=auth_method,
        run_id=run_id,
        run_phase=run_phase,
    )


def _mock_ws(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    return ws


def _mock_item(*, workspace_id, name="host1", address="10.0.0.4", groups=None, host_vars=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.workspace_id = workspace_id
    item.name = name
    item.address = address
    item.groups = groups if groups is not None else ["web"]
    item.vars = host_vars if host_vars is not None else {}
    item.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    item.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return item


def _mock_inventory(*, workspace_id, name="default"):
    inventory = MagicMock()
    inventory.id = uuid.uuid4()
    inventory.workspace_id = workspace_id
    inventory.name = name
    inventory.description = ""
    inventory.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    inventory.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return inventory


def _mock_version(*, inventory_id, hosts=None, groups=None, produced_by="api"):
    version = MagicMock()
    version.id = uuid.uuid4()
    version.inventory_id = inventory_id
    version.hosts = hosts if hosts is not None else {"host1": {}}
    version.groups = groups if groups is not None else {"web": ["host1"]}
    version.host_count = len(version.hosts)
    version.group_count = len(version.groups)
    version.produced_by = produced_by
    version.produced_by_ref = ""
    version.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return version


def _mock_source(kind="terraform", position=0):
    source = MagicMock()
    source.id = uuid.uuid4()
    source.kind = kind
    source.position = position
    source.config = {}
    source.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return source


def _make_app(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


# ── Authorization: capability-based callers ──────────────────────────────────


class TestCapabilityAuthorization:
    async def test_read_caps_can_list_items(self):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.list_items", AsyncMock(return_value=[_mock_item(workspace_id=ws.id)])),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventory-items", headers=_AUTH)
        assert res.status_code == 200
        assert res.json()["data"][0]["attributes"]["name"] == "host1"

    async def test_no_caps_cannot_list_items(self):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}.resolve_workspace_capabilities_for", AsyncMock(return_value=frozenset())),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventory-items", headers=_AUTH)
        assert res.status_code == 403
        assert cap.INVENTORY_READ in res.json()["detail"]

    async def test_read_is_not_enough_to_declare_a_host(self):
        """The write capability is a separate grant from the read one."""
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403

    async def test_write_caps_can_declare_a_host(self):
        ws = _mock_ws()
        app, db = _make_app(_user())
        item = _mock_item(workspace_id=ws.id)
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(f"{_R}.inv.create_item", AsyncMock(return_value=item)) as create,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={
                        "data": {
                            "attributes": {
                                "name": "host1",
                                "address": "10.0.0.4",
                                "groups": ["web"],
                                "vars": {"ansible_user": "ec2-user"},
                            }
                        }
                    },
                    headers=_AUTH,
                )
        assert res.status_code == 201
        assert create.await_args.kwargs["name"] == "host1"
        assert create.await_args.kwargs["host_vars"] == {"ansible_user": "ec2-user"}
        db.commit.assert_awaited()

    async def test_the_write_capability_is_in_the_write_tier_not_admin(self):
        """A declared host arrives by an apply, so requiring admin to write one
        through the API would be stricter than the path every item takes."""
        assert cap.INVENTORY_WRITE in caps_for_level("write")
        assert cap.INVENTORY_READ in caps_for_level("read")


# ── Authorization: the runner-token implicit grant ───────────────────────────


class TestRunnerTokenGrant:
    async def test_a_runner_token_may_declare_on_its_own_workspace(self):
        ws = _mock_ws()
        run_id = uuid.uuid4()
        app, _ = _make_app(_user(auth_method="runner_token", run_id=str(run_id), run_phase="apply"))
        item = _mock_item(workspace_id=ws.id)
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(f"{_R}.inv.create_item", AsyncMock(return_value=item)),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 201

    async def test_a_runner_token_may_NOT_declare_on_another_workspace(self):
        """The grant is scoped to the run's own workspace.

        This is the case that would be a vulnerability rather than a bug: a
        token from any run could otherwise rewrite the target set of a configure
        on a workspace it has nothing to do with.
        """
        ws = _mock_ws()
        other = uuid.uuid4()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="apply")
        )
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=other)),
            patch(f"{_R}.inv.create_item", AsyncMock()) as create,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403
        assert "not scoped to a run on this workspace" in res.json()["detail"]
        create.assert_not_awaited()

    async def test_a_runner_token_naming_no_run_is_refused(self):
        ws = _mock_ws()
        app, _ = _make_app(_user(auth_method="runner_token", run_id=None))
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=None)),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventory-items", headers=_AUTH)
        assert res.status_code == 403

    async def test_the_runner_grant_does_not_consult_workspace_capabilities(self):
        """A runner token carries `everyone` only, so if the grant fell through
        to the capability check it would never work."""
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="apply")
        )
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=frozenset()),
            ) as resolve,
            patch(f"{_R}.inv.list_items", AsyncMock(return_value=[])),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventory-items", headers=_AUTH)
        assert res.status_code == 200
        resolve.assert_not_awaited()


class TestPhaseBinding:
    async def test_a_plan_phase_token_cannot_declare_a_host(self):
        """Writes are apply-phase. A speculative pull-request plan's own token
        must not be able to rewrite the inventory a later configure targets."""
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="plan")
        )
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.create_item", AsyncMock()) as create,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403
        assert "apply phase" in res.json()["detail"]
        create.assert_not_awaited()

    async def test_a_plan_phase_token_CAN_read_inventory(self):
        """A plan has to read inventory to diff it, so reads are unphased."""
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="plan")
        )
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.list_items", AsyncMock(return_value=[])),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventory-items", headers=_AUTH)
        assert res.status_code == 200

    async def test_a_token_with_no_phase_claim_passes_any_phase(self):
        """A listener older than the phase claim. Refusing it would break every
        run on a lagging listener image for a defence in depth; the run-scoping
        still holds."""
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase=None)
        )
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(
                f"{_R}.inv.create_item",
                AsyncMock(return_value=_mock_item(workspace_id=ws.id)),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 201


# ── Request validation ───────────────────────────────────────────────────────


class TestValidation:
    @pytest.fixture
    def ws(self):
        return _mock_ws()

    def _writer(self, ws):
        app, _ = _make_app(_user())
        return app, (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
        )

    async def test_a_missing_name_is_422(self, ws):
        app, patches = self._writer(ws)
        with patches[0], patches[1]:
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"address": "10.0.0.4"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422

    async def test_groups_must_be_a_list_of_strings(self, ws):
        app, patches = self._writer(ws)
        with patches[0], patches[1], patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "h", "groups": [{"nope": 1}]}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422
        assert "groups" in res.json()["detail"]

    async def test_vars_must_be_an_object(self, ws):
        app, patches = self._writer(ws)
        with patches[0], patches[1], patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "h", "vars": ["nope"]}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422

    async def test_a_name_ansible_cannot_target_is_422_not_500(self, ws):
        """The service raises `InventoryValidationError`; the router owes a 422."""
        app, patches = self._writer(ws)
        with (
            patches[0],
            patches[1],
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(
                f"{_R}.inv.create_item",
                AsyncMock(side_effect=InventoryValidationError("host name '!web' contains")),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "!web"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422

    async def test_a_duplicate_host_is_409_not_500(self, ws):
        """Two applies racing, or a name already declared. Answered 500 before
        the handler existed."""
        app, patches = self._writer(ws)
        orig = MagicMock()
        orig.sqlstate = "23505"
        with (
            patches[0],
            patches[1],
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(
                f"{_R}.inv.create_item",
                AsyncMock(side_effect=IntegrityError("stmt", {}, orig)),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 409
        assert "already declared" in res.json()["detail"]

    async def test_the_per_workspace_ceiling_is_enforced(self, ws):
        from terrapod.api.routers.inventory import MAX_ITEMS_PER_WORKSPACE

        app, patches = self._writer(ws)
        with (
            patches[0],
            patches[1],
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=MAX_ITEMS_PER_WORKSPACE)),
            patch(f"{_R}.inv.create_item", AsyncMock()) as create,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "host1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422
        create.assert_not_awaited()

    async def test_a_bad_workspace_id_is_404_not_500(self):
        """`parse_id_for`, not a bare `uuid.UUID` on request input."""
        app, _ = _make_app(_user())
        async with await _client(app) as c:
            res = await c.get("/api/v1/workspaces/ws-not-a-uuid/inventory-items", headers=_AUTH)
        assert res.status_code == 404


class TestPartialUpdate:
    async def test_an_absent_attribute_is_left_alone_and_an_empty_list_clears(self):
        """Omitting `groups` and sending `groups: []` are different requests.

        Collapsing them would make a cleared group list impossible to express,
        which the provider needs in order to remove a host from every group.
        """
        ws = _mock_ws()
        item = _mock_item(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_item", AsyncMock(return_value=item)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
            patch(f"{_R}.inv.update_item", AsyncMock(return_value=item)) as update,
        ):
            async with await _client(app) as c:
                await c.patch(
                    f"/api/v1/inventory-items/invitem-{item.id}",
                    json={"data": {"attributes": {"address": "10.0.0.9"}}},
                    headers=_AUTH,
                )
                omitted = update.await_args.kwargs
                await c.patch(
                    f"/api/v1/inventory-items/invitem-{item.id}",
                    json={"data": {"attributes": {"groups": []}}},
                    headers=_AUTH,
                )
                cleared = update.await_args.kwargs

        assert omitted["groups"] is None, "an omitted list must not be touched"
        assert omitted["address"] == "10.0.0.9"
        assert cleared["groups"] == [], "an empty list must clear, not be ignored"
        assert cleared["address"] is None


# ── Resolution and snapshots ─────────────────────────────────────────────────


class TestResolvedView:
    async def test_it_serves_the_latest_snapshot_with_its_age(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        version = _mock_version(inventory_id=inventory.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.latest_version", AsyncMock(return_value=version)),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}/resolved", headers=_AUTH)
        assert res.status_code == 200
        attrs = res.json()["data"]["attributes"]
        # The freshness surface: a preview is as fresh as the last resolve, and
        # saying so beats implying it is live.
        assert attrs["taken-at"] == "2026-01-01T00:00:00Z"
        assert attrs["host-count"] == 1
        # Ansible's own shape, rendered rather than stored twice.
        assert attrs["ansible-inventory"]["all"]["children"] == ["web"]

    async def test_it_resolves_on_first_read_when_every_source_is_api_owned(self):
        """A workspace that has just declared its hosts can see them without
        waiting for a configure to exist."""
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        version = _mock_version(inventory_id=inventory.id)
        app, db = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.latest_version", AsyncMock(return_value=None)),
            patch(f"{_R}.inv.list_sources", AsyncMock(return_value=[_mock_source()])),
            patch(
                f"{_R}.inv.resolve_and_snapshot", AsyncMock(return_value=(None, version))
            ) as snap,
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}/resolved", headers=_AUTH)
        assert res.status_code == 200
        snap.assert_awaited_once()
        db.commit.assert_awaited()

    async def test_it_refuses_rather_than_partially_resolving(self):
        """A source needing ansible means the API answers 409.

        Resolving the rest would show a target set that is silently too small,
        which is the failure the snapshot exists to prevent.
        """
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.latest_version", AsyncMock(return_value=None)),
            patch(f"{_R}.inv.list_sources", AsyncMock(return_value=[_mock_source(kind="git")])),
            patch(f"{_R}.inv.resolve_and_snapshot", AsyncMock()) as snap,
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}/resolved", headers=_AUTH)
        assert res.status_code == 409
        snap.assert_not_awaited()
        # And it names the offending kind. The read and the resolve action
        # refuse for the same reason, and they had drifted: this one withheld
        # the kinds, which is the actionable half, while being the refusal the
        # UI actually hits. Both now compose one shared message.
        assert "git" in res.json()["detail"], res.json()["detail"]

    async def test_the_resolve_action_names_the_offending_source_kinds(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
            patch(
                f"{_R}.inv.list_sources",
                AsyncMock(return_value=[_mock_source(), _mock_source(kind="git", position=1)]),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/actions/resolve", headers=_AUTH
                )
        assert res.status_code == 409
        assert "git" in res.json()["detail"]


class TestSnapshotUpload:
    async def test_only_a_runner_token_may_post_a_snapshot(self):
        """A snapshot records what a resolve actually found, so it is posted by
        the thing that ran it -- not hand-written by a person with write."""
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}.inv.record_snapshot", AsyncMock()) as record,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/versions",
                    json={"data": {"attributes": {"hosts": {}, "groups": {}}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403
        assert "Runner token required" in res.json()["detail"]
        record.assert_not_awaited()

    async def test_a_runner_snapshot_is_recorded_against_its_run(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        run_id = uuid.uuid4()
        version = _mock_version(inventory_id=inventory.id, produced_by="runner")
        app, _ = _make_app(_user(auth_method="runner_token", run_id=f"run-{run_id}"))
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.record_snapshot", AsyncMock(return_value=version)) as record,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/versions",
                    json={
                        "data": {
                            "attributes": {
                                "hosts": {"h1": {"ansible_host": "10.0.0.1"}, "h2": {}},
                                "groups": {"web": ["h1", "h2"]},
                            }
                        }
                    },
                    headers=_AUTH,
                )
        assert res.status_code == 201
        kwargs = record.await_args.kwargs
        assert kwargs["produced_by"] == "runner"
        # The bare uuid, whichever spelling the token carried.
        assert kwargs["produced_by_ref"] == str(run_id)

    async def test_a_malformed_hosts_map_is_422(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, _ = _make_app(_user(auth_method="runner_token", run_id=str(uuid.uuid4())))
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(f"{_R}._runner_run_workspace", AsyncMock(return_value=ws.id)),
            patch(f"{_R}.inv.record_snapshot", AsyncMock()) as record,
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/versions",
                    json={"data": {"attributes": {"hosts": {"h1": "not-an-object"}}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422
        record.assert_not_awaited()


class TestLimitPreview:
    async def _preview(self, limit: str, *, version=None):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        version = version or _mock_version(
            inventory_id=inventory.id,
            hosts={"host1": {}, "host2": {}, "switch1": {}},
            groups={"web": ["host1", "host2"], "net": ["host2", "switch1"]},
        )
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.latest_version", AsyncMock(return_value=version)),
        ):
            async with await _client(app) as c:
                return await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/actions/preview-limit",
                    json={"data": {"attributes": {"limit": limit}}},
                    headers=_AUTH,
                )

    async def test_it_answers_what_a_limit_would_target(self):
        """Visibility is the control here rather than prevention, because
        auto-configure is deliberately broad (#1974)."""
        res = await self._preview("web:!host2")
        assert res.status_code == 200
        attrs = res.json()["data"]["attributes"]
        assert attrs["hosts"] == ["host1"]
        assert attrs["of-host-count"] == 3

    async def test_an_empty_limit_is_the_whole_inventory(self):
        res = await self._preview("")
        assert res.json()["data"]["attributes"]["host-count"] == 3

    async def test_a_regex_limit_is_refused_rather_than_matching_nothing(self):
        """An empty target set for a pattern ansible would expand is the wrong
        answer dressed as an answer."""
        res = await self._preview("~web.*")
        assert res.status_code == 422
        assert "regular expression" in res.json()["detail"]

    async def test_with_no_snapshot_there_is_nothing_to_limit_against(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.latest_version", AsyncMock(return_value=None)),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/inventories/inv-{inventory.id}/actions/preview-limit",
                    json={"data": {"attributes": {"limit": "web"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 409


class TestPayNothing:
    async def test_a_workspace_that_declares_nothing_has_no_inventories(self):
        """Keyed on data, not on a flag (#1986): nothing is created until
        something uses it, so a terraform/tofu-only deployment sees an empty
        list rather than a surface it has to turn off.
        """
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}.inv.list_inventories", AsyncMock(return_value=[])),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/workspaces/ws-{ws.id}/inventories", headers=_AUTH)
        assert res.status_code == 200
        assert res.json()["data"] == []


class TestTheUnresolvableRefusalIsQuotedInTheDocs:
    """Both leads are quoted verbatim in `docs/ansible-inventory.md` and
    `docs/api-reference.md`, so they are pinned here.

    A doc quoting an error message is a claim that goes stale silently: the code
    changes, the page keeps showing output nothing produces, and an operator
    searching for the text they were given finds nothing. This file already
    asserts that the read names the offending kinds; these pin the opening
    clause the pages reproduce.
    """

    def test_both_leads_exist_and_differ(self):
        from terrapod.api.routers import inventory as mod

        read = mod._unresolvable_error([], "This inventory has never been resolved.")
        action = mod._unresolvable_error([], "This inventory was not refreshed.")

        assert read.detail.startswith("This inventory has never been resolved.")
        assert action.detail.startswith("This inventory was not refreshed.")
        assert read.status_code == action.status_code == 409
        # Same body after the lead: one message, two openings. If they diverge
        # again, the pages describing them as one message become false.
        assert read.detail.split(".", 1)[1] == action.detail.split(".", 1)[1]

    def test_it_names_every_offending_kind_and_only_those(self):
        from terrapod.api.routers import inventory as mod

        sources = [_mock_source("terraform", 0), _mock_source("git", 1), _mock_source("ini", 2)]
        detail = mod._unresolvable_error(sources, "lead.").detail

        assert "'git'" in detail and "'ini'" in detail
        assert "terraform" not in detail, (
            "naming the source the API CAN resolve would send an operator after the wrong one"
        )


class TestTheInventoryWireShape:
    """Sources ride in `attributes.sources`, and that is what the SDK decodes.

    This is here because the shape drifted once and nothing noticed: the server
    emitted the list under a top-level `included-sources` key it had invented,
    go-terrapod declared `json:"sources"`, and the field was permanently nil.
    No fixture on either side held a source, so both halves passed.
    """

    async def test_sources_are_an_attribute_not_an_invented_top_level_key(self):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        sources = [_mock_source("terraform", 0), _mock_source("ini", 1)]
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}.inv.list_sources", AsyncMock(return_value=sources)),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}", headers=_AUTH)

        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert "sources" in data["attributes"], "the SDK reads attributes.sources"
        assert "included-sources" not in data, "an invented top-level key is not the contract"
        assert "sources" not in data.get("relationships", {}), (
            "a source has no route of its own, so a relationship would link to nowhere"
        )

    async def test_a_source_entry_is_flat_and_carries_its_position_and_kind(self):
        """Flat, because it is part of the inventory's composition rather than a
        nested resource object -- and position is the `-i` order that decides
        which source wins a conflicting host variable."""
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        sources = [_mock_source("terraform", 0), _mock_source("ini", 1)]
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}.inv.list_sources", AsyncMock(return_value=sources)),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}", headers=_AUTH)

        entries = res.json()["data"]["attributes"]["sources"]
        assert [e["position"] for e in entries] == [0, 1]
        assert [e["kind"] for e in entries] == ["terraform", "ini"]
        assert all(e["id"].startswith("invsrc-") for e in entries)
        # Flat: no nested resource envelope inside an attribute value.
        assert "attributes" not in entries[0]

    async def test_api_resolvable_is_reported_per_source_as_well_as_rolled_up(self):
        """The rolled-up value says a runner is needed; the per-source value says
        which source needs one. The resolve refusal names the kinds, so a reader
        with only the rollup would have less than the error message does."""
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        sources = [_mock_source("terraform", 0), _mock_source("ini", 1)]
        app, _ = _make_app(_user())
        with (
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}.inv.list_sources", AsyncMock(return_value=sources)),
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}", headers=_AUTH)

        attrs = res.json()["data"]["attributes"]
        assert attrs["api-resolvable"] is False, "one source needs ansible"
        assert [e["api-resolvable"] for e in attrs["sources"]] == [True, False]
