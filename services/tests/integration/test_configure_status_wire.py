"""The runner protocol carries `configuring`; the frozen surface folds it (#2425 §2.2).

`_reported_run_status` folds `configuring` to `applying` for the TFE
compatibility surface, because `go-tfe` has no such status and a client that
cannot switch on it waits forever. The **runner protocol** must receive the
native one: the listener acts on the status it is handed, and the folded value
describes a phase that is not running.

Two gates already stand over this and neither can see what reaches the wire:

- `tests/api/test_run_spine.py` walks the AST and asserts that any hardcoded
  `tfe=` argument in the runs router is `False` and that only the two
  runner-protocol endpoints hardcode it. Source introspection -- it proves the
  argument is written, not that the response carries the status the argument
  selects.
- `tests/api/test_run_status_contract.py` drives `_reported_run_status`
  directly. It pins the fold as a function and says nothing about which
  endpoint asks for which surface.

So this is route-level, through the real HTTP route, against real rows -- and it
asserts the CONTRAST. A test that only read the runner protocol would pass
whether or not the fold exists anywhere, and one that only read the TFE surface
would say nothing about the runner; the property is a relationship between the
two surfaces, so both are instantiated for the SAME run.

The integration tier because getting a run into `configuring` means a real
claim: `claim_next_run` moves a standalone configure `queued -> configuring` as
it hands it over, which is the only way a `configuring` run exists at all.

A note on the wire-contract snapshot, because its silence here is easy to
misread: `api_wire_contract.json`'s `runs_next_attributes` has no `status`
entry, and that is correct rather than blind. That list is extracted from the
CONSUMER side -- `attrs.get("...")` in `runner/listener.py` and `engines/*.py`
-- so it records what the bundled listener reads, and the bundled listener does
not read `status` off the claim. The attribute is nevertheless on the wire:
`_run_json` emits it first, for both runner-protocol endpoints, and a custom
listener or an operator reading the claim gets whatever it says. That is what
this test pins.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import inventory_resolve, pool_set, run_service
from tests.integration.conftest import AUTH, admin_user, set_auth, set_listener_auth

pytestmark = pytest.mark.integration

#: The runner protocol, on the canonical native prefix.
CLAIM = "/api/v1/listeners/{listener_id}/runs/next"
#: The frozen compatibility surface, canonical prefix.
TFE_RUN = "/api/tfe/v2/runs/run-{run_id}"


async def _pool_with_listener(client, tag: str) -> tuple[str, str]:
    """A real pool + join token + joined listener, through the real endpoints.

    Registering a listener only in the auth override is not enough: `next_run`
    looks it up in Redis, so a hand-faked identity gets a 404 and the test
    would be asserting on the wrong failure.

    Returns `(pool_uuid, listener_id)` -- the pool id already stripped of its
    `apool-` prefix, which is the form `set_listener_auth` parses.
    """
    resp = await client.post(
        "/api/v1/agent-pools",
        json={"data": {"type": "agent-pools", "attributes": {"name": f"cfgwire-pool-{tag}"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    pool_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"/api/v1/agent-pools/{pool_id}/tokens",
        json={"data": {"attributes": {"description": "test"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    raw = resp.json()["data"]["attributes"]["token"]

    resp = await client.post(
        f"/api/v1/agent-pools/{pool_id}/listeners/join",
        json={"join_token": raw, "name": f"cfgwire-listener-{tag}"},
    )
    assert resp.status_code == 201, resp.text
    return pool_id.removeprefix("apool-"), resp.json()["data"]["listener_id"]


async def _queued_standalone_configure(pool_uuid: str, tag: str) -> uuid.UUID:
    """A standalone configure sitting in `queued`, ready to be claimed.

    Seeded through the ORM and through `run_service.create_run`, not
    hand-written SQL: `runs` carries ~70 columns, so an INSERT here would break
    on whichever `NOT NULL` is added next, with a driver error naming a column
    that has nothing to do with this test.

    `configure_enabled` is declared on the workspace rather than flipped on the
    row afterwards, so `create_run` runs its declaration guard in full: without
    the setting the request for a configure is refused, which is the refusal
    that makes `is_configure` below mean something.

    The inventory artifact is written at run creation (#2021) and raises when it
    cannot resolve, so a configure cannot be created without a real archive
    holding a real inventory. That is #2021's property and has its own tests;
    this file is about what the claim puts on the wire, so the write is stubbed
    at the boundary BELOW what is under test -- `create_run` still runs in full.
    """
    async with get_db_session() as db:
        ws = Workspace(
            name=f"configure-wire-{tag}",
            execution_mode="agent",
            config_directory="ansible",
            default_playbook="site.yml",
            configure_enabled=True,
        )
        pool_set.set_workspace_pools(ws, [uuid.UUID(pool_uuid)])
        db.add(ws)
        await db.flush()

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()

        with patch.object(
            inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))
        ):
            run = await run_service.create_run(
                db, ws, configuration_version_id=cv.id, is_configure=True
            )
        assert run.is_configure, "the workspace did not resolve to a configure"
        assert not run.configure_after_apply, "wanted a STANDALONE configure, got a composite"

        run = await run_service.transition_run(db, run, "queued")
        await db.commit()
        return run.id


class TestAConfiguringRunReachesEachSurfaceInItsOwnVocabulary:
    """The contrast, driven through both routes for one run."""

    async def test_the_runner_protocol_gets_configuring_and_tfe_gets_applying(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_uuid, listener_id = await _pool_with_listener(client, tag)
        run_id = await _queued_standalone_configure(pool_uuid, tag)

        set_listener_auth(app, listener_id, pool_uuid)
        resp = await client.get(CLAIM.format(listener_id=listener_id))
        assert resp.status_code == 200, resp.text
        claimed = resp.json()["data"]
        assert claimed["id"] == f"run-{run_id}", "claimed some other run"
        # The claim is what put the run in `configuring`; without the configure
        # phase the status below would be `planning` and the assertion would be
        # measuring a plan.
        assert claimed["attributes"]["phase"] == "configure"

        # (1) The runner protocol is native by definition. A listener is not a
        #     `go-tfe` client and cannot act on a folded phase.
        assert claimed["attributes"]["status"] == "configuring", (
            "the runner protocol served the TFE-folded status; a listener handed "
            "`applying` for a run whose configure is what is actually running "
            "cannot act on it"
        )

        # The same row, unchanged in between -- so the difference below is the
        # surface and nothing else.
        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert stored.status == "configuring"

        # (2) The frozen surface cannot represent it, so it folds.
        resp = await client.get(TFE_RUN.format(run_id=run_id), headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["status"] == "applying", (
            "the frozen surface leaked the native `configuring`, which `go-tfe` "
            "does not define -- a CLI waiting on this run waits forever"
        )
