"""Confirm-time cloud-identity staleness, against real rows (#1901).

`run_service._cloud_identity_moved_since_plan` gates the planned -> apply
transition, and it was covered only at the services tier against `AsyncMock`.
The routing rule puts a run state-machine transition in the integration tier,
and this is why: everything the guard is about is a property of the stored rows
rather than of the code's control flow — the snapshot column written at run
creation, the minted-targets column appended by the mint endpoint, the
workspace's own override as it stands now — and the composite reason is chosen
by `_staleness_reason` ahead of a state-serial query and an expiry check that
both read the database too.

The consequence of getting it wrong is the apply running against real
infrastructure under a different cloud identity from the one its plan was
reviewed under. The runner's mint path refuses that too, per target, but only
inside a Job after it has been scheduled and after `init` — so what this buys
is failing before anything exists, and naming what moved.

Three properties, and the middle one is the reason `oidc_minted_targets` is
recorded at all:

* a minted target whose audiences moved refuses the apply, and discards the
  plan so the operator cannot retry into the same wall;
* a target the run never minted for does NOT refuse, because the snapshot is
  the MERGED map and carries deployment-wide catalogue entries a workspace may
  never use — scoping the check to those would mean one catalogue edit refusing
  every pending apply in the fleet;
* an unchanged configuration confirms, which is what stops the other two
  passing against a guard that refuses everything.
"""

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"

AWS = "sts.amazonaws.example"
MOVED = "sts.moved.example"
VAULT = "https://vault.example/"


async def _workspace(client, name, audiences):
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {"name": name, "oidc-audiences": audiences},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"]

    cv = await client.post(
        f"/api/v2/workspaces/{ws_id}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert cv.status_code == 201, cv.text
    up = await client.put(
        cv.json()["data"]["attributes"]["upload-url"],
        content=b"placeholder-tarball-for-tests",
        headers={"Content-Type": "application/x-tar"},
    )
    assert up.status_code in (200, 204), up.text
    return ws_id


async def _planned_run(client, ws_id, *, minted):
    """A real apply-capable run, driven to `planned` in the database.

    The plan phase itself needs a listener, a Job and a runner, so the run is
    created through the API — which is what writes the `oidc_audiences`
    snapshot from the live configuration, the thing under test — and then moved
    to the state a completed plan leaves behind. `oidc_minted_targets` is what
    the mint endpoint appends as it serves each target.
    """
    resp = await client.post(
        "/api/v2/runs",
        json={
            "data": {
                "type": "runs",
                "attributes": {"message": "identity staleness"},
                "relationships": {"workspace": {"data": {"id": ws_id, "type": "workspaces"}}},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["attributes"]["plan-only"] is False, (
        "the guard only applies to apply-capable runs, so a plan-only run here "
        "would make every assertion below vacuous"
    )

    async with get_db_session() as session:
        run = (
            await session.execute(
                select(Run).where(Run.id == uuid.UUID(run_id.removeprefix("run-")))
            )
        ).scalar_one()
        snapshot = dict(run.oidc_audiences or {})
        run.status = "planned"
        run.has_changes = True
        run.plan_finished_at = now_utc()
        run.oidc_minted_targets = list(minted)
        await session.commit()
    return run_id, snapshot


async def _move_the_workspace_override(ws_id, audiences):
    async with get_db_session() as session:
        ws = (
            await session.execute(
                select(Workspace).where(Workspace.id == uuid.UUID(ws_id.removeprefix("ws-")))
            )
        ).scalar_one()
        ws.oidc_audiences = audiences
        await session.commit()


async def _status(client, run_id):
    resp = await client.get(f"/api/v2/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["attributes"]["status"]


class TestTheSnapshotIsWrittenAtRunCreation:
    async def test_the_run_carries_the_resolved_map_from_the_workspace(self, app, client):
        """The snapshot column is the whole basis of the check, and it is
        written by run creation rather than by anything the test controls — so
        if it ever stopped being written, every scenario below would silently
        become "nothing was minted, nothing to check"."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-snapshot", {"aws": [AWS]})
        _run_id, snapshot = await _planned_run(client, ws_id, minted=["aws"])
        assert snapshot == {"aws": [AWS]}


class TestAMintedTargetThatMoved:
    async def test_the_apply_is_refused_with_409_naming_the_target(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-moved", {"aws": [AWS]})
        run_id, _snapshot = await _planned_run(client, ws_id, minted=["aws"])

        await _move_the_workspace_override(ws_id, {"aws": [MOVED]})

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "cloud identity configuration changed since plan" in detail
        assert "aws" in detail
        # The operator's action, in the message, because "stale" alone does not
        # say what to do.
        assert "re-plan required" in detail

    async def test_the_plan_is_discarded_so_a_retry_cannot_hit_the_same_wall(self, app, client):
        """`confirm_run` discards and COMMITS before raising, deliberately: the
        409 goes through the router's error path, which would otherwise roll the
        session back and leave the run sitting `planned` for an operator to
        retry forever. That ordering is only observable against a real
        transaction."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-discard", {"aws": [AWS]})
        run_id, _snapshot = await _planned_run(client, ws_id, minted=["aws"])

        await _move_the_workspace_override(ws_id, {"aws": [MOVED]})
        assert (
            await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        ).status_code == 409

        assert await _status(client, run_id) == "discarded"
        async with get_db_session() as session:
            run = (
                await session.execute(
                    select(Run).where(Run.id == uuid.UUID(run_id.removeprefix("run-")))
                )
            ).scalar_one()
            assert "cloud identity" in (run.discard_reason or "")

    async def test_removing_a_minted_target_altogether_also_refuses(self, app, client):
        """The apply would present no identity where the plan presented one."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-removed", {"aws": [AWS]})
        run_id, _snapshot = await _planned_run(client, ws_id, minted=["aws"])

        await _move_the_workspace_override(ws_id, {})

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 409, resp.text
        assert "aws" in resp.json()["detail"]


class TestTheCheckIsScopedToWhatTheRunMinted:
    async def test_a_target_the_run_never_minted_for_does_not_refuse(self, app, client):
        """Why `oidc_minted_targets` is recorded at all.

        The snapshot is the merged map, so it carries entries a workspace may
        never use. Checking against the configured set instead would let one
        edit to an unrelated provider refuse every pending apply in the fleet.
        """
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-unminted", {"aws": [AWS], "vault": [VAULT]})
        run_id, snapshot = await _planned_run(client, ws_id, minted=["aws"])
        assert set(snapshot) == {"aws", "vault"}, snapshot

        # `vault` moves; this run never presented it.
        await _move_the_workspace_override(ws_id, {"aws": [AWS], "vault": ["https://moved/"]})

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert await _status(client, run_id) == "confirmed"

    async def test_a_run_that_minted_nothing_is_never_identity_stale(self, app, client):
        """The overwhelming majority of runs. A workspace holding no identity
        must not be able to fail an apply over this check at all."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-nonminted", {})
        run_id, snapshot = await _planned_run(client, ws_id, minted=[])
        assert snapshot == {}

        await _move_the_workspace_override(ws_id, {"aws": [AWS]})

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert await _status(client, run_id) == "confirmed"


class TestAnUnchangedConfigurationConfirms:
    async def test_the_apply_proceeds_when_nothing_moved(self, app, client):
        """The control. Without it every refusal above would pass against a
        guard that refused unconditionally — and this is the case that runs on
        every federated apply in a healthy deployment, so it is the one that
        must not become a 409."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-unchanged", {"aws": [AWS]})
        run_id, _snapshot = await _planned_run(client, ws_id, minted=["aws"])

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert await _status(client, run_id) == "confirmed"

    async def test_a_newly_added_target_is_not_a_staleness_cause(self, app, client):
        """The mint reads the run's SNAPSHOT, so a target added since the plan
        yields no token at apply exactly as it yielded none at plan — the
        identity the apply presents is unchanged."""
        set_auth(app, admin_user())
        ws_id = await _workspace(client, "ident-added", {"aws": [AWS]})
        run_id, _snapshot = await _planned_run(client, ws_id, minted=["aws"])

        await _move_the_workspace_override(ws_id, {"aws": [AWS], "azurerm": ["api://exchange"]})

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert await _status(client, run_id) == "confirmed"
