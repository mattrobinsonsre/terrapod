"""The inventory router: authorization, phase binding and request validation.

#1967 (the eight structures) and #1968 (declared by the workspace's own
Terraform).

The interesting half is authorization, because this router has **two** kinds of
caller and one of them carries an implicit grant. The cases that matter:

* a runner token may manage its own run's workspace and **nothing else** -- the
  cross-workspace case is the one that would be a vulnerability rather than a
  bug, so it is pinned directly;
* writes are bound to the **apply** phase while reads are unphased, because a
  plan reads inventory to diff it and never writes;
* a token carrying **no** phase claim passes any phase, matching how
  `require_runner_for_run` treats a listener older than the claim -- refusing it
  would break every run on a lagging listener image.

The structural properties -- the composite keys, the cascades, the cycle guard,
encryption at rest -- are the engine's, so they live in the integration tier
where a real constraint can raise. A mocked session would answer from its
fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_R = "terrapod.api.routers.inventory"
V1 = "/api/v1"


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


def _ts(obj):
    obj.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    obj.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return obj


def _mock_host(*, workspace_id, name="web-1"):
    host = _ts(MagicMock())
    host.id = uuid.uuid4()
    host.workspace_id = workspace_id
    host.name = name
    return host


def _mock_group(*, workspace_id, name="web"):
    group = _ts(MagicMock())
    group.id = uuid.uuid4()
    group.workspace_id = workspace_id
    group.name = name
    return group


def _mock_var(*, workspace_id, parent_attr, parent_id, key="ansible_user", sensitive=False):
    var = _ts(MagicMock())
    var.id = uuid.uuid4()
    var.workspace_id = workspace_id
    setattr(var, parent_attr, parent_id)
    var.key = key
    var.value = "deploy"
    var.structured = False
    var.sensitive = sensitive
    return var


def _mock_settings(*, workspace_id, vcs_connection_id=None):
    s = _ts(MagicMock())
    s.workspace_id = workspace_id
    s.include_platform = True
    s.vcs_connection_id = vcs_connection_id
    s.repo_url = ""
    s.branch = ""
    s.working_directory = ""
    s.ignore_paths = []
    return s


def _make_app(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)


def _zero_counts():
    """Every count query returns nothing, which is the common fixture.

    The serializers take counts as keyword arguments defaulting to 0, so a
    missing count renders as 0 rather than raising -- which is what lets these
    tests say nothing about counts unless they are the point.
    """
    return {
        f"{_R}.inv.host_group_counts": AsyncMock(return_value={}),
        f"{_R}.inv.host_var_counts": AsyncMock(return_value={}),
        f"{_R}.inv.group_member_counts": AsyncMock(return_value={}),
        f"{_R}.inv.group_child_counts": AsyncMock(return_value={}),
        f"{_R}.inv.group_var_counts": AsyncMock(return_value={}),
    }


class _Patches:
    """Apply a dict of patches as one context manager."""

    def __init__(self, mapping):
        self._ctx = [patch(target, value) for target, value in mapping.items()]

    def __enter__(self):
        for c in self._ctx:
            c.__enter__()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctx):
            c.__exit__(*exc)
        return False


# ── Authorization: capability-based callers ──────────────────────────────────


class TestCapabilityAuthorization:
    """`inventory:read` to read and `inventory:write` to change.

    `write` rather than `admin` is deliberate: the Terraform that declares hosts
    runs under an apply, so an API stricter than the path every row arrives by
    would be incoherent.
    """

    @pytest.mark.parametrize(
        ("method", "path_for", "level", "expect"),
        [
            ("get", lambda ws, _: f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", "read", 200),
            ("get", lambda ws, _: f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", "none", 403),
            ("get", lambda ws, _: f"{V1}/workspaces/ws-{ws.id}/inventory/groups", "read", 200),
            ("get", lambda ws, _: f"{V1}/workspaces/ws-{ws.id}/inventory/vars", "read", 200),
            ("get", lambda ws, _: f"{V1}/workspaces/ws-{ws.id}/inventory/vars", "none", 403),
        ],
    )
    async def test_reads_need_read(self, method, path_for, level, expect):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level(level) if level != "none" else []
                ),
                f"{_R}.inv.list_hosts": AsyncMock(return_value=[]),
                f"{_R}.inv.list_groups": AsyncMock(return_value=[]),
                f"{_R}.inv.list_global_vars": AsyncMock(return_value=[]),
                **_zero_counts(),
            }
        ):
            async with await _client(app) as c:
                res = await getattr(c, method)(path_for(ws, None), headers=_AUTH)
        assert res.status_code == expect, res.text

    async def test_a_read_capability_cannot_declare_a_host(self):
        """The one that would matter: read must not be able to write."""
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403, res.text
        assert cap.INVENTORY_WRITE in res.json()["detail"]

    async def test_write_can_declare_a_host(self):
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        app, db = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
                f"{_R}.inv.create_host": AsyncMock(return_value=host),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 201, res.text
        assert res.json()["data"]["attributes"]["name"] == "web-1"
        db.commit.assert_awaited()


# ── Authorization: the runner-token grant ────────────────────────────────────


class TestRunnerTokenGrant:
    """A runner token may manage its OWN run's workspace and nothing else.

    The same shape as the implicit registry read runner tokens already carry,
    and for the same reason: `terraform apply` cannot work without it.
    """

    async def test_a_runner_token_may_write_its_own_workspace(self):
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        run_id = uuid.uuid4()
        app, _ = _make_app(_user(auth_method="runner_token", run_id=str(run_id), run_phase="apply"))
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}._runner_run_workspace": AsyncMock(return_value=ws.id),
                f"{_R}.inv.create_host": AsyncMock(return_value=host),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 201, res.text

    async def test_a_runner_token_cannot_reach_another_workspace(self):
        """The vulnerability case, pinned directly rather than inferred."""
        ws = _mock_ws()
        other = uuid.uuid4()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="apply")
        )
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                # Its run belongs to a DIFFERENT workspace.
                f"{_R}._runner_run_workspace": AsyncMock(return_value=other),
                f"{_R}.inv.create_host": AsyncMock(),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403, res.text
        assert "not scoped to a run on this workspace" in res.json()["detail"]

    async def test_a_runner_token_naming_no_run_is_refused(self):
        ws = _mock_ws()
        app, _ = _make_app(_user(auth_method="runner_token", run_id=None))
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.inv.list_hosts": AsyncMock(return_value=[]),
                **_zero_counts(),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", headers=_AUTH)
        assert res.status_code == 403, res.text


class TestPhaseBinding:
    """Writes are bound to the apply phase; reads are unphased.

    A plan reads inventory to diff it and never writes, so this follows the
    phase claim without needing a new concept (GHSA-xmrf-hxq9-m59m).
    """

    async def test_a_plan_phase_token_cannot_write(self):
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="plan")
        )
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}._runner_run_workspace": AsyncMock(return_value=ws.id),
                f"{_R}.inv.create_host": AsyncMock(),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 403, res.text
        assert "apply phase" in res.json()["detail"]

    async def test_a_plan_phase_token_can_read(self):
        """Unphased on purpose: a plan diffs the inventory."""
        ws = _mock_ws()
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase="plan")
        )
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}._runner_run_workspace": AsyncMock(return_value=ws.id),
                f"{_R}.inv.list_hosts": AsyncMock(return_value=[]),
                **_zero_counts(),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", headers=_AUTH)
        assert res.status_code == 200, res.text

    async def test_a_token_with_no_phase_claim_passes_any_phase(self):
        """A listener older than the claim. Refusing it would break every run on
        a lagging listener image; the run-scoping still holds."""
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        app, _ = _make_app(
            _user(auth_method="runner_token", run_id=str(uuid.uuid4()), run_phase=None)
        )
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}._runner_run_workspace": AsyncMock(return_value=ws.id),
                f"{_R}.inv.create_host": AsyncMock(return_value=host),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": "web-1"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 201, res.text


# ── Request validation ───────────────────────────────────────────────────────


class TestValidation:
    async def test_a_host_needs_a_name(self):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text

    @pytest.mark.parametrize("name", ["web 1", "!web", "web,db", "a:b", "x&y", "~re"])
    async def test_a_host_name_that_breaks_limit_is_refused(self, name):
        """Every refused character means something to `--limit`, so a host named
        with one is unselectable -- and a leading `!` silently excludes the host
        it names from any pattern mentioning it."""
        from terrapod.services.inventory_resolution import validate_host_name

        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
                # The real validator, reached through the real service call.
                f"{_R}.inv.create_host": AsyncMock(
                    side_effect=lambda *a, **k: validate_host_name(k["name"])
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/hosts",
                    json={"data": {"attributes": {"name": name}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text

    @pytest.mark.parametrize("name", ["all", "ungrouped"])
    async def test_a_derived_group_name_is_refused(self, name):
        from terrapod.services.inventory_resolution import validate_group_name

        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
                f"{_R}.inv.create_group": AsyncMock(
                    side_effect=lambda *a, **k: validate_group_name(k["name"])
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/workspaces/ws-{ws.id}/inventory/groups",
                    json={"data": {"attributes": {"name": name}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text
        assert "derived by ansible" in res.json()["detail"]

    async def test_a_membership_needs_a_host_relationship(self):
        """And the refusal shows the shape, because a caller that got the
        nesting wrong cannot guess it from "host is required"."""
        ws = _mock_ws()
        group = _mock_group(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._group": AsyncMock(return_value=group),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/inventory-groups/invgroup-{group.id}/hosts",
                    json={"data": {"attributes": {}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert "relationships" in detail and "inventory-hosts" in detail

    async def test_a_non_string_variable_value_is_refused_with_the_alternative(self):
        """A list or a number is expressed with `structured`, the same way a
        structured workspace variable is -- so the refusal says so rather than
        leaving the caller with no route for it."""
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host": AsyncMock(return_value=host),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/inventory-hosts/invhost-{host.id}/vars",
                    json={"data": {"attributes": {"key": "ports", "value": [80, 443]}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text
        assert "structured" in res.json()["detail"]


# ── The wire shape ───────────────────────────────────────────────────────────


class TestTheWireShape:
    """Links are relationships, and a sensitive value never leaves the server.

    The first because the house style says a link is a relationship and there
    is no legacy `*-id` attribute to keep compatible -- none of this exists on
    any release, so the canonical form is the only form.
    """

    async def test_a_membership_carries_both_sides_as_relationships(self):
        ws = _mock_ws()
        link = MagicMock()
        link.id = uuid.uuid4()
        link.workspace_id = ws.id
        link.host_id = uuid.uuid4()
        link.group_id = uuid.uuid4()
        link.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        group = _mock_group(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._group": AsyncMock(return_value=group),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.list_host_groups": AsyncMock(return_value=[link]),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/inventory-groups/invgroup-{group.id}/hosts", headers=_AUTH)
        entry = res.json()["data"][0]
        rels = entry["relationships"]
        assert rels["host"]["data"]["id"] == f"invhost-{link.host_id}"
        assert rels["group"]["data"]["id"] == f"invgroup-{link.group_id}"
        # Not an attribute: a link is a relationship in this house style.
        assert "host-id" not in entry["attributes"]
        assert "group-id" not in entry["attributes"]

    async def test_a_sensitive_value_is_masked_and_the_real_one_is_absent(self):
        ws = _mock_ws()
        host_id = uuid.uuid4()
        var = _mock_var(
            workspace_id=ws.id, parent_attr="host_id", parent_id=host_id, sensitive=True
        )
        var.value = "hunter2"
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}.inv.get_host_var": AsyncMock(return_value=var),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/inventory-host-vars/invhvar-{var.id}", headers=_AUTH)

        body = res.text
        attrs = res.json()["data"]["attributes"]
        assert attrs["value"] == "***"
        assert attrs["sensitive"] is True
        # The whole response, not just the field: a masked value that leaks
        # through some other key is the same disclosure.
        assert "hunter2" not in body

    async def test_a_host_carries_its_counts_rather_than_its_rows(self):
        """A list shows "3 groups, 2 variables"; embedding either would make one
        request grow with the whole inventory."""
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.list_hosts": AsyncMock(return_value=[host]),
                f"{_R}.inv.host_group_counts": AsyncMock(return_value={host.id: 3}),
                f"{_R}.inv.host_var_counts": AsyncMock(return_value={host.id: 2}),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", headers=_AUTH)
        attrs = res.json()["data"][0]["attributes"]
        assert attrs["group-count"] == 3
        assert attrs["variable-count"] == 2
        assert "groups" not in attrs and "vars" not in attrs

    async def test_the_settings_are_identified_by_the_workspace(self):
        """One inventory per workspace, so there is no surrogate id to carry and
        the workspace id is the only honest identifier."""
        ws = _mock_ws()
        settings = _mock_settings(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.get_settings": AsyncMock(return_value=settings),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/settings", headers=_AUTH)
        data = res.json()["data"]
        assert data["id"] == f"ws-{ws.id}"
        assert data["relationships"]["vcs-connection"]["data"] is None


class TestPayNothing:
    """A terraform/tofu-only workspace sees nothing, keyed on DATA not a flag.

    #1986 withdrew the engine on/off switch, so "those users pay nothing" is
    delivered by there being no rows rather than by a setting an operator could
    get wrong.
    """

    async def test_a_workspace_with_no_inventory_has_no_settings(self):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.get_settings": AsyncMock(return_value=None),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/settings", headers=_AUTH)
        assert res.status_code == 404, res.text
        # And it says the absence is the default, so a reader does not go
        # looking for what they did wrong.
        assert "default" in res.json()["detail"]

    async def test_an_empty_host_list_is_an_empty_list_not_an_error(self):
        ws = _mock_ws()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.list_hosts": AsyncMock(return_value=[]),
                **_zero_counts(),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/hosts", headers=_AUTH)
        assert res.status_code == 200
        assert res.json()["data"] == []
        assert res.json()["meta"]["pagination"]["total-count"] == 0


class TestTheRoutesAreNativeOnly:
    async def test_nothing_is_mounted_on_a_tfe_prefix(self):
        """No `terraform`, `tofu` or `tfci` invocation consumes any of this, so
        by the rule in `docs/tfe-cli-surface.md` it is native-only."""
        app = create_app()
        offenders = [
            r.path
            for r in app.routes
            if "inventory" in getattr(r, "path", "")
            and ("/api/v2" in r.path or "/api/tfe/" in r.path)
        ]
        assert offenders == [], offenders

    async def test_every_route_is_on_the_alias_too(self):
        """`include_terrapod` mounts both. A route on the canonical prefix alone
        is a removal for a runner or listener that lags the server."""
        app = create_app()

        def paths(prefix):
            return {
                f"{m} {r.path.replace(prefix, '/api/v1/')}"
                for r in app.routes
                if getattr(r, "methods", None)
                for m in r.methods
                if "inventory" in r.path and r.path.startswith(prefix)
            }

        canonical = paths("/api/v1/")
        alias = paths("/api/terrapod/v1/")
        assert canonical, "no canonical inventory routes at all"
        assert alias == canonical
