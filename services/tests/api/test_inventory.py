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


def _pubkey(name="ssh-ed25519", fill=b"\x01"):
    """A blob of the real shape: a length-prefixed algorithm name, then key bytes.

    Built rather than hardcoded so the embedded name and the declared `key_type`
    agree. The validator refuses a disagreement, so a hardcoded blob would make
    every fixture here depend on my having typed base64 correctly.
    """
    import base64
    import struct

    return base64.b64encode(struct.pack(">I", len(name)) + name.encode() + fill * 32).decode()


def _mock_host_key(*, workspace_id, host_id, key_type="ssh-ed25519", marker=None):
    row = _ts(MagicMock())
    row.id = uuid.uuid4()
    row.workspace_id = workspace_id
    row.host_id = host_id
    row.key_type = key_type
    row.public_key = _pubkey(key_type if key_type.startswith("ssh-ed") else "ssh-ed25519")
    row.marker = marker
    return row


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
        """The vulnerability case, pinned directly rather than inferred.

        It is also what makes TERRAPOD_WORKSPACE safe to leave plainly
        overridable (#2036). The runner exports it so the Terrapod provider
        defaults to the run's own workspace, and an operator may set it to
        anything — because the server does NOT trust it. A wrong value reaches
        this refusal, not another workspace's inventory.

        So do not later fold TERRAPOD_WORKSPACE into `provider_credentials`'
        all-or-nothing rule by analogy with the hostname and token. That rule
        exists because the token is a credential and the hostname decides where
        it is sent; a workspace id is a request parameter this test authorises.
        Hardening it would cost clarity and buy nothing.
        """
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


class TestPayNothing:
    """A terraform/tofu-only workspace sees nothing, keyed on DATA not a flag.

    #1986 withdrew the engine on/off switch, so "those users pay nothing" is
    delivered by there being no rows rather than by a setting an operator could
    get wrong.
    """

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


# ── Approved SSH host keys (#2592) ───────────────────────────────────────────


class TestApprovedHostKeys:
    """The platform's own host-key model, which hands off to OpenSSH.

    Ansible is not modelled on here: it has no inventory-level notion of an
    approved key, its `ansible_ssh_host_key_*` facts describe what a host REPORTED
    rather than what anyone approves, and its `known_hosts` module runs during a
    play -- after the connection it would have protected.
    """

    async def test_declaring_a_key_returns_the_wire_shape(self):
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        row = _mock_host_key(workspace_id=ws.id, host_id=host.id)
        app, db = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host": AsyncMock(return_value=host),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
                f"{_R}.inv.create_host_key": AsyncMock(return_value=row),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/inventory-hosts/invhost-{host.id}/keys",
                    json={
                        "data": {
                            "attributes": {
                                "key-type": "ssh-ed25519",
                                "public-key": row.public_key,
                            }
                        }
                    },
                    headers=_AUTH,
                )
        assert res.status_code == 201, res.text
        data = res.json()["data"]
        assert data["type"] == "inventory-host-keys"
        assert data["id"] == f"invhkey-{row.id}"
        assert data["attributes"]["key-type"] == "ssh-ed25519"
        # NOT masked. A host's public key is public, and hiding it would hide the
        # one value an operator reads when a host presents something else.
        assert data["attributes"]["public-key"] == row.public_key
        assert data["attributes"]["marker"] is None
        assert data["relationships"]["host"]["data"]["id"] == f"invhost-{host.id}"

    async def test_every_exposed_type_is_listed_not_one_per_family(self):
        """The property the whole model exists for.

        An ideal host configuration carries every key type the host exposes. A
        shape that held one key per algorithm family -- which is what ansible's
        facts are -- would make an ordinary three-key host unrepresentable, and
        would have nowhere to put the second key of a type during a rotation.
        """
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        rows = [
            _mock_host_key(workspace_id=ws.id, host_id=host.id, key_type="ssh-ed25519"),
            _mock_host_key(workspace_id=ws.id, host_id=host.id, key_type="ecdsa-sha2-nistp256"),
            _mock_host_key(workspace_id=ws.id, host_id=host.id, key_type="rsa-sha2-512"),
            _mock_host_key(
                workspace_id=ws.id, host_id=host.id, key_type="ssh-rsa", marker="revoked"
            ),
        ]
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host": AsyncMock(return_value=host),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.list_host_keys": AsyncMock(return_value=rows),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/inventory-hosts/invhost-{host.id}/keys", headers=_AUTH)
        assert res.status_code == 200, res.text
        body = res.json()
        assert [d["attributes"]["key-type"] for d in body["data"]] == [
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "rsa-sha2-512",
            "ssh-rsa",
        ]
        # The revoked one is carried rather than filtered. It is the case the
        # feature exists to catch, so a reader must be able to see it.
        assert [d["attributes"]["marker"] for d in body["data"]] == [
            None,
            None,
            None,
            "revoked",
        ]
        assert body["meta"]["pagination"]["total-count"] == 4

    @pytest.mark.parametrize("frozen", ["key-type", "public-key"])
    async def test_the_key_itself_cannot_be_edited_in_place(self, frozen):
        """A PATCH changing the algorithm or the blob would rewrite what a host is
        trusted to present while keeping the row's id -- and that id is the audit
        trail. It is a delete and a declare, so the route refuses."""
        ws = _mock_ws()
        row = _mock_host_key(workspace_id=ws.id, host_id=uuid.uuid4())
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host_key": AsyncMock(return_value=row),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.patch(
                    f"{V1}/inventory-host-keys/invhkey-{row.id}",
                    json={"data": {"attributes": {frozen: "ssh-rsa"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, res.text
        assert frozen in res.text
        assert "identity" in res.text

    async def test_the_marker_is_editable(self):
        """The pair to the test above: refusing the key must not refuse a
        revocation, which is the one change this row exists to accept."""
        ws = _mock_ws()
        row = _mock_host_key(workspace_id=ws.id, host_id=uuid.uuid4())
        marked = _mock_host_key(workspace_id=ws.id, host_id=row.host_id, marker="revoked")
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host_key": AsyncMock(return_value=row),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("write")
                ),
                f"{_R}.inv.update_host_key": AsyncMock(return_value=marked),
            }
        ):
            async with await _client(app) as c:
                res = await c.patch(
                    f"{V1}/inventory-host-keys/invhkey-{row.id}",
                    json={"data": {"attributes": {"marker": "revoked"}}},
                    headers=_AUTH,
                )
        assert res.status_code == 200, res.text
        assert res.json()["data"]["attributes"]["marker"] == "revoked"

    @pytest.mark.parametrize(
        ("key_type", "public_key", "why"),
        [
            ("ssh-dss", None, "an algorithm outside the closed set"),
            ("ssh-ed25519", "not base64 at all!!", "a blob that is not base64"),
            ("ssh-ed25519", "WHOLE LINE", "a whole known_hosts line pasted in"),
            ("ssh-rsa", "MISMATCH", "a blob whose embedded name is a different algorithm"),
        ],
    )
    async def test_a_malformed_declaration_is_refused(self, key_type, public_key, why):
        """Driven through the REAL validators, via the real service call, so these
        say something about what the API accepts rather than about a mock."""
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        if public_key is None:
            public_key = _pubkey()
        elif public_key == "WHOLE LINE":
            public_key = f"web-1 ssh-ed25519 {_pubkey()}"
        elif public_key == "MISMATCH":
            public_key = _pubkey("ssh-ed25519")  # declared ssh-rsa, carries ed25519

        app, db = _make_app(_user())
        db.flush = AsyncMock()
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
                    f"{V1}/inventory-hosts/invhost-{host.id}/keys",
                    json={"data": {"attributes": {"key-type": key_type, "public-key": public_key}}},
                    headers=_AUTH,
                )
        assert res.status_code == 422, f"{why}: {res.status_code} {res.text}"

    async def test_the_workspace_wide_read_is_keyed_on_the_host_name(self):
        """It is an extension beside `inventory/resolved`, meant to be read with
        it -- so it is keyed the way that document is keyed, on the name, not on
        the host's id."""
        ws = _mock_ws()
        host_id = uuid.uuid4()
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
                f"{_R}.inv.host_keys_by_host_name": AsyncMock(
                    return_value={
                        "web-1": [
                            _mock_host_key(workspace_id=ws.id, host_id=host_id),
                            _mock_host_key(
                                workspace_id=ws.id,
                                host_id=host_id,
                                key_type="ecdsa-sha2-nistp256",
                            ),
                        ],
                        "db-1": [_mock_host_key(workspace_id=ws.id, host_id=uuid.uuid4())],
                    }
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.get(f"{V1}/workspaces/ws-{ws.id}/inventory/host-keys", headers=_AUTH)
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert [d["id"] for d in data] == ["db-1", "web-1"]
        by_host = {d["id"]: d for d in data}
        assert len(by_host["web-1"]["attributes"]["keys"]) == 2
        assert len(by_host["db-1"]["attributes"]["keys"]) == 1

    async def test_a_reader_cannot_declare_a_key(self):
        """Declaring an approved key is granting trust, so it is a write."""
        ws = _mock_ws()
        host = _mock_host(workspace_id=ws.id)
        app, _ = _make_app(_user())
        with _Patches(
            {
                f"{_R}._host": AsyncMock(return_value=host),
                f"{_R}._get_workspace": AsyncMock(return_value=ws),
                f"{_R}.resolve_workspace_capabilities_for": AsyncMock(
                    return_value=caps_for_level("read")
                ),
            }
        ):
            async with await _client(app) as c:
                res = await c.post(
                    f"{V1}/inventory-hosts/invhost-{host.id}/keys",
                    json={
                        "data": {
                            "attributes": {
                                "key-type": "ssh-ed25519",
                                "public-key": _pubkey(),
                            }
                        }
                    },
                    headers=_AUTH,
                )
        assert res.status_code == 403, res.text
