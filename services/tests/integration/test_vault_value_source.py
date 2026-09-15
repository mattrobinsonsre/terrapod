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

from terrapod.config import VaultInstanceConfig
from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    Variable,
    VariableSet,
    VariableSetVariable,
    VariableSetWorkspace,
    Workspace,
)
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

        # An instance must be configured, or resolution errors on instance
        # SELECTION and never reaches the read — which is how this test used to
        # pass without exercising the failure path it names.
        prior = (settings.vault.enabled, settings.vault.instances)
        settings.vault.enabled = True
        settings.vault.instances = [
            VaultInstanceConfig(name="default", default=True, address="https://vault.test:8200")
        ]
        try:
            # Patch where it is USED, not where it is defined: the resolver does
            # `from ... import read_secret`, binding the name at import, so
            # patching the client module was a no-op and this test passed on an
            # unrelated instance-selection error.
            with patch(
                "terrapod.services.vault_source_service.read_secret_data",
                new=AsyncMock(side_effect=VaultError("Vault denied 'kvv2/apps/x'")),
            ):
                resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        finally:
            settings.vault.enabled, settings.vault.instances = prior

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


# ── File delivery (#1619) ─────────────────────────────────────────────

_FILE_SECRET = "S3CR3T-integration-file-content"


async def _workspace_with(ws_vars: list[dict], set_vars: list[dict] | None = None):
    """A workspace with its own variables and, optionally, an assigned set."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        ws = Workspace(name=f"vfile-{tag}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        for kw in ws_vars:
            db.add(Variable(workspace_id=ws.id, sensitive=True, **kw))
        if set_vars:
            vs = VariableSet(name=f"vfile-set-{tag}")
            db.add(vs)
            await db.flush()
            for kw in set_vars:
                db.add(VariableSetVariable(variable_set_id=vs.id, sensitive=True, **kw))
            db.add(VariableSetWorkspace(variable_set_id=vs.id, workspace_id=ws.id))
        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws.id, cv.id


def _vref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/gcp", "field": "sa"}
    base.update(kw)
    return json.dumps(base)


async def _claim_with_vault(app, client, ws_id, cv_id, read):
    """Queue a run on the workspace and claim it through the real endpoint."""
    from terrapod.config import settings
    from tests.integration.conftest import admin_user, set_auth, set_listener_auth

    set_auth(app, admin_user())
    pool_id, listener_id = await _pool_with_listener(client, uuid.uuid4().hex[:8])
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        pool_set.set_workspace_pools(ws, [uuid.UUID(pool_id.removeprefix("apool-"))])
        run = await run_service.create_run(db, ws, configuration_version_id=cv_id)
        run = await run_service.transition_run(db, run, "queued")
        await db.commit()
        run_id = run.id

    set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))
    prior = (settings.vault.enabled, settings.vault.instances)
    settings.vault.enabled = True
    settings.vault.instances = [
        VaultInstanceConfig(name="default", default=True, address="https://vault.test:8200")
    ]
    try:
        with patch("terrapod.services.vault_source_service.read_secret_data", new=read):
            resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
    finally:
        settings.vault.enabled, settings.vault.instances = prior

    async with get_db_session() as db:
        final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    return resp, final


class TestFileDelivery:
    """Through the real `runs/next`, real precedence, real rows."""

    async def test_kv2_file_mode_for_env_and_terraform(self, app, client):
        ws_id, cv_id = await _workspace_with(
            [
                {
                    "key": "GOOGLE_APPLICATION_CREDENTIALS",
                    "value": _vref(file={"name": "gcp/adc.json"}),
                    "category": "env",
                    "value_source": "vault",
                },
                {
                    "key": "sa_file",
                    "value": _vref(file={}),
                    "category": "terraform",
                    "value_source": "vault",
                },
            ]
        )
        read = AsyncMock(return_value={"sa": _FILE_SECRET})
        resp, final = await _claim_with_vault(app, client, ws_id, cv_id, read)

        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        env = {v["key"]: v["value"] for v in attrs["env-vars"]}
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/var/run/terrapod/files/gcp/adc.json"
        tf = {v["key"]: v["value"] for v in attrs["terraform-vars"]}
        assert tf["sa_file"] == "/var/run/terrapod/files/sa_file"
        assert sorted(f["name"] for f in attrs["vault-files"]) == ["gcp/adc.json", "sa_file"]
        assert all(f["value"] == _FILE_SECRET for f in attrs["vault-files"])
        # The same secret is read once, and the content is nowhere else.
        assert read.await_count == 1
        rest = {k: v for k, v in attrs.items() if k != "vault-files"}
        assert _FILE_SECRET not in json.dumps(rest)
        assert final.status == "planning"

    async def test_a_dynamic_engine_in_file_mode(self, app, client):
        ref = _vref(
            engine="dynamic",
            method="POST",
            mount="pki",
            path="issue/web",
            field="private_key",
            data={"common_name": "a.example.test"},
            file={"name": "~/tls/key.pem"},
        )
        ws_id, cv_id = await _workspace_with(
            [{"key": "TLS_KEY", "value": ref, "category": "env", "value_source": "vault"}]
        )
        read = AsyncMock(return_value={"private_key": _FILE_SECRET, "certificate": "C"})
        resp, final = await _claim_with_vault(app, client, ws_id, cv_id, read)

        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert {v["key"]: v["value"] for v in attrs["env-vars"]}["TLS_KEY"] == (
            "/home/runner/tls/key.pem"
        )
        assert attrs["vault-files"] == [
            {"key": "TLS_KEY", "name": "~/tls/key.pem", "value": _FILE_SECRET}
        ]
        kw = read.await_args.kwargs
        assert (kw["engine"], kw["method"], kw["data"]) == (
            "dynamic",
            "POST",
            {"common_name": "a.example.test"},
        )
        assert final.status == "planning"

    async def test_a_workspace_variable_overrides_a_set_variable_of_the_same_key(self, app, client):
        """Precedence runs first: one key, one file — the workspace's — so the
        set's file name is not a collision."""
        ws_id, cv_id = await _workspace_with(
            [
                {
                    "key": "CREDS",
                    "value": _vref(path="apps/ws", file={"name": "creds.json"}),
                    "category": "env",
                    "value_source": "vault",
                }
            ],
            set_vars=[
                {
                    "key": "CREDS",
                    "value": _vref(path="apps/set", file={"name": "creds.json"}),
                    "category": "env",
                    "value_source": "vault",
                }
            ],
        )

        async def read(inst, **kw):
            return {"sa": f"from-{kw['path']}"}

        resp, final = await _claim_with_vault(
            app, client, ws_id, cv_id, AsyncMock(side_effect=read)
        )
        assert resp.status_code == 200, resp.text
        files = resp.json()["data"]["attributes"]["vault-files"]
        assert files == [{"key": "CREDS", "name": "creds.json", "value": "from-apps/ws"}]
        assert final.status == "planning"

    async def test_a_set_and_a_workspace_variable_at_one_path_error_the_run(self, app, client):
        ws_id, cv_id = await _workspace_with(
            [
                {
                    "key": "WS_CREDS",
                    "value": _vref(file={"name": "creds.json"}),
                    "category": "env",
                    "value_source": "vault",
                }
            ],
            set_vars=[
                {
                    "key": "SET_CREDS",
                    "value": _vref(file={"name": "creds.json"}),
                    "category": "env",
                    "value_source": "vault",
                }
            ],
        )
        read = AsyncMock(return_value={"sa": _FILE_SECRET})
        resp, final = await _claim_with_vault(app, client, ws_id, cv_id, read)
        assert resp.status_code == 204
        assert final.status == "errored"
        assert "'SET_CREDS'" in final.error_message and "'WS_CREDS'" in final.error_message
        assert "/var/run/terrapod/files/creds.json" in final.error_message
        assert _FILE_SECRET not in final.error_message
        read.assert_not_awaited()

    async def test_an_unavailable_vault_puts_a_file_mode_run_back_in_the_queue(self, app, client):
        from terrapod.services.vault_client import VaultUnavailable

        ws_id, cv_id = await _workspace_with(
            [{"key": "F", "value": _vref(file={}), "category": "env", "value_source": "vault"}]
        )
        read = AsyncMock(side_effect=VaultUnavailable("HTTP 503"))
        resp, final = await _claim_with_vault(app, client, ws_id, cv_id, read)
        assert resp.status_code == 204
        assert final.status == "queued"
