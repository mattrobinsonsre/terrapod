"""Vault value source, end to end against a real database (#1439).

What needs a real engine here is the *run* behaviour: an unresolvable reference
must leave a run in `errored` carrying the cause, not a 500 to the listener with
the run stuck claimed. That is a state-machine outcome, so it is asserted
against real rows rather than a mocked session.

Vault itself is stubbed — these tests are about Terrapod's handling. The client
is covered by unit tests, and by a live Kubernetes-auth run against a real Vault
in-cluster.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import ConfigurationVersion, Run, Variable, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service, variable_service
from terrapod.services.vault_client import VaultError

pytestmark = pytest.mark.integration


async def _workspace_with_vault_var(ref: dict, *, key: str = "API_TOKEN"):
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        ws = Workspace(name=f"vault-{tag}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        db.add(
            Variable(
                workspace_id=ws.id,
                key=key,
                value=json.dumps(ref),
                category="env",
                sensitive=True,
                value_source="vault",
            )
        )
        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws.id, cv.id


async def _pool_with_listener(client, tag: str):
    """A real pool + join token + joined listener, via the actual endpoints.

    Registering a listener only in the auth override is not enough — dispatch
    looks it up in Redis, so a hand-faked identity gets a 404 and the test would
    be asserting on the wrong failure.
    """
    from tests.integration.conftest import AUTH

    resp = await client.post(
        "/api/terrapod/v1/agent-pools",
        json={"data": {"type": "agent-pools", "attributes": {"name": f"vault-pool-{tag}"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    pool_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"/api/terrapod/v1/agent-pools/{pool_id}/tokens",
        json={"data": {"attributes": {"description": "test"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    raw = resp.json()["data"]["attributes"]["token"]

    resp = await client.post(
        f"/api/terrapod/v1/agent-pools/{pool_id}/listeners/join",
        json={"join_token": raw, "name": f"listener-{tag}"},
    )
    assert resp.status_code == 201, resp.text
    return pool_id, resp.json()["data"]["listener_id"]


_REF = {"source": "vault", "mount": "kvv2", "path": "apps/x", "field": "token"}


class TestPersistence:
    async def test_value_source_round_trips(self, app):
        """The column exists, defaults to static, and holds vault when set."""
        ws_id, _ = await _workspace_with_vault_var(_REF)
        async with get_db_session() as db:
            var = (
                await db.execute(select(Variable).where(Variable.workspace_id == ws_id))
            ).scalar_one()
            assert var.value_source == "vault"
            assert json.loads(var.value)["path"] == "apps/x"

    async def test_an_ordinary_variable_defaults_to_static(self, app):
        """Every pre-existing row keeps its behaviour — the migration is expand
        only, and a static value is still the literal."""
        async with get_db_session() as db:
            ws = Workspace(name=f"plain-{uuid.uuid4().hex[:8]}")
            db.add(ws)
            await db.flush()
            var = await variable_service.create_variable(
                db, workspace_id=ws.id, key="PLAIN", value="literal", category="env"
            )
            await db.commit()
            assert var.value_source == "static"

    async def test_a_vault_variable_is_forced_sensitive(self, app):
        """What the reference resolves to is a secret however the request
        described it."""
        async with get_db_session() as db:
            ws = Workspace(name=f"force-{uuid.uuid4().hex[:8]}")
            db.add(ws)
            await db.flush()
            var = await variable_service.create_variable(
                db,
                workspace_id=ws.id,
                key="T",
                value=json.dumps(_REF),
                category="env",
                sensitive=False,
                value_source="vault",
            )
            await db.commit()
            assert var.sensitive is True


class TestResolutionCarriesThrough:
    async def test_a_vault_variable_reaches_variable_resolution(self, app):
        """It must arrive at the resolver marked as a reference — precedence and
        set membership are unchanged, only the value source differs."""
        ws_id, _ = await _workspace_with_vault_var(_REF)
        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(db, ws_id)
        by_key = {v.key: v for v in resolved}
        assert by_key["API_TOKEN"].value_source == "vault"
        assert json.loads(by_key["API_TOKEN"].value)["field"] == "token"


class TestAnUnresolvableReferenceErrorsTheRun:
    """The point of failing closed: the operator sees a failed run naming the
    cause, not a run wedged in `planning` after a 500 to the listener.

    Driven through the real `runs/next` endpoint. Asserting on the resolver
    alone would prove the exception is raised, not that the endpoint catches it
    and errors the run — which is the behaviour anyone actually experiences.
    """

    async def test_the_endpoint_errors_the_run_instead_of_500ing(self, app, client):
        from terrapod.config import settings
        from tests.integration.conftest import admin_user, set_auth, set_listener_auth

        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_id, listener_id = await _pool_with_listener(client, tag)
        ws_id, cv_id = await _workspace_with_vault_var(_REF)

        async with get_db_session() as db:
            ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
            pool_set.set_workspace_pools(ws, [uuid.UUID(pool_id.removeprefix("apool-"))])
            run = await run_service.create_run(db, ws, configuration_version_id=cv_id)
            run = await run_service.transition_run(db, run, "queued")
            await db.commit()
            run_id = run.id

        set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))

        prior = settings.vault.enabled
        settings.vault.enabled = True
        try:
            with patch(
                "terrapod.services.vault_client.read_secret",
                new=AsyncMock(side_effect=VaultError("Vault denied 'kvv2/apps/x'")),
            ):
                resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        finally:
            settings.vault.enabled = prior

        # 204, not 500: the listener is told there is nothing to run rather than
        # handed an error with the run left claimed and going nowhere.
        assert resp.status_code == 204, resp.text

        async with get_db_session() as db:
            final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert final.status == "errored", "an unresolvable reference must fail the run"
        assert "API_TOKEN" in final.error_message, "the message must name the variable"

    async def test_a_static_only_workspace_is_unaffected(self, app, client):
        """The guard must not disturb the ordinary path — a workspace with no
        vault variables dispatches exactly as before."""
        from tests.integration.conftest import admin_user, set_auth, set_listener_auth

        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_id, listener_id = await _pool_with_listener(client, tag)

        async with get_db_session() as db:
            ws = Workspace(name=f"plain-ws-{tag}", execution_mode="agent")
            pool_set.set_workspace_pools(ws, [uuid.UUID(pool_id.removeprefix("apool-"))])
            db.add(ws)
            await db.flush()
            db.add(Variable(workspace_id=ws.id, key="PLAIN", value="literal", category="env"))
            cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
            db.add(cv)
            await db.flush()
            run = await run_service.create_run(db, ws, configuration_version_id=cv.id)
            run = await run_service.transition_run(db, run, "queued")
            await db.commit()
            run_id = run.id

        set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))
        resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")

        assert resp.status_code == 200, resp.text
        env = {v["key"]: v["value"] for v in resp.json()["data"]["attributes"]["env-vars"]}
        assert env["PLAIN"] == "literal"
        async with get_db_session() as db:
            final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert final.status == "planning"
