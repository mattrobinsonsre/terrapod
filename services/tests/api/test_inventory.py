"""The inventory routers: authorization, phase binding and request validation.

#1967 (the inventory object and what it resolves to) and #1968 (declared items).

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
from terrapod.db.models import InventorySource
from terrapod.db.session import get_db
from terrapod.services.inventory_resolution import (
    InventoryValidationError,
    ResolvedInventory,
)

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


def _resolved(hosts=None, groups=None) -> ResolvedInventory:
    """A real `ResolvedInventory`, not a mock.

    The router renders ansible's shape from it, so a MagicMock standing in for
    one would make every assertion about that shape a statement about the mock.
    """
    return ResolvedInventory(
        hosts=hosts if hosts is not None else {"host1": {}},
        groups=groups if groups is not None else {"web": ["host1"]},
    )


def _mock_source(kind=InventorySource.KIND_PLATFORM, position=0):
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

    async def test_a_non_string_var_value_is_422_through_the_route(self, ws):
        """The validator is tested directly elsewhere; this proves the router
        turns it into a 422 rather than a 500, and that the message names the
        key the caller has to fix.

        Worth driving through the route because the alternative is the failure
        this rule exists to remove: accepted at the write, then invisible to
        every client that reads vars as a string map.
        """
        app, patches = self._writer(ws)
        with (
            patches[0],
            patches[1],
            patch(f"{_R}.inv.count_items", AsyncMock(return_value=0)),
            patch(
                f"{_R}.inv.create_item",
                AsyncMock(
                    side_effect=InventoryValidationError(
                        "the declared value for host variable 'port' must be a string; got int"
                    )
                ),
            ),
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"/api/v1/workspaces/ws-{ws.id}/inventory-items",
                    json={"data": {"attributes": {"name": "web-1", "vars": {"port": 8080}}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text
        assert "port" in res.json()["detail"]

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


# ── Resolution ───────────────────────────────────────────────────────────────


class TestResolvedView:
    """What the inventory resolves to, resolved to answer the request."""

    async def _get(self, *, resolved=None, level="read"):
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        app, db = _make_app(_user())
        with (
            patch(f"{_R}._get_inventory", AsyncMock(return_value=inventory)),
            patch(f"{_R}._get_workspace", AsyncMock(return_value=ws)),
            patch(
                f"{_R}.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level(level)),
            ),
            patch(f"{_R}.inv.resolve", AsyncMock(return_value=resolved or _resolved())) as resolve,
        ):
            async with await _client(app) as c:
                res = await c.get(f"/api/v1/inventories/inv-{inventory.id}/resolved", headers=_AUTH)
        return res, resolve, db

    async def test_it_resolves_the_rows_to_answer_the_read(self):
        res, resolve, _ = await self._get()
        assert res.status_code == 200
        resolve.assert_awaited_once()

        attrs = res.json()["data"]["attributes"]
        assert attrs["host-count"] == 1
        # Ansible's own shape, rendered rather than stored twice.
        assert attrs["ansible-inventory"]["all"]["children"] == ["web"]

    async def test_it_writes_nothing(self):
        """The property, not the absence of a route.

        A read that resolves live has nothing to record, and recording one
        anyway is what would let a dashboard left open evict a target set a
        configure is pinned to. Asserting the route is gone proves only that
        the route is gone; asserting the commit proves the read is read-only.
        """
        _, _, db = await self._get()
        db.commit.assert_not_awaited()

    async def test_an_empty_inventory_resolves_to_the_empty_set(self):
        """Not a refusal, and not a 404.

        The empty set is a real resolution, and it is the answer at exactly the
        moment an operator is checking what they have just declared.
        """
        res, _, _ = await self._get(resolved=_resolved(hosts={}, groups={}))
        assert res.status_code == 200
        attrs = res.json()["data"]["attributes"]
        assert attrs["host-count"] == 0
        assert attrs["group-count"] == 0

    async def test_it_carries_no_freshness_field(self):
        """Pinned, because the field it replaced was read as staleness.

        `taken-at` on a live answer invites a reader to ask whether it is
        current, which is a question the read has already answered by
        resolving. A revert that reintroduces it fails here.
        """
        res, _, _ = await self._get()
        attrs = res.json()["data"]["attributes"]
        for gone in ("taken-at", "produced-by", "produced-by-ref", "api-resolvable"):
            assert gone not in attrs, f"{gone} came back"


class TestLimitPreview:
    async def _preview(self, limit: str, *, resolved=None):
        """Drive the preview against a live resolution."""
        ws = _mock_ws()
        inventory = _mock_inventory(workspace_id=ws.id)
        resolved = resolved or _resolved(
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
            patch(f"{_R}.inv.resolve", AsyncMock(return_value=resolved)),
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

    async def test_an_empty_inventory_always_has_something_to_limit_against(self):
        """Including the empty set, rather than the old "no snapshot yet"
        refusal that fired at exactly the wrong moment."""
        res = await self._preview("all", resolved=_resolved(hosts={}, groups={}))
        assert res.status_code == 200, res.text

    async def test_the_preview_carries_no_freshness_field(self):
        res = await self._preview("all")
        assert "taken-at" not in res.json()["data"]["attributes"]


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
        sources = [_mock_source(InventorySource.KIND_PLATFORM, 0), _mock_source("ini", 1)]
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
        sources = [_mock_source(InventorySource.KIND_PLATFORM, 0), _mock_source("ini", 1)]
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
        assert [e["kind"] for e in entries] == ["platform", "ini"]
        assert all(e["id"].startswith("invsrc-") for e in entries)
        # Flat: no nested resource envelope inside an attribute value.
        assert "attributes" not in entries[0]
