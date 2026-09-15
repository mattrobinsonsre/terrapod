"""An errored run's Vault leases are revoked (#1649), against real Postgres and Redis.

The whole path runs for real except Vault, which is stubbed at the HTTP layer
(``httpx.MockTransport``) so the revoke request itself is built and classified
by the real client:

1. a listener claims the run through ``runs/next`` — the dynamic read returns
   a lease, which the claim records in Redis;
2. the listener reports the plan Job launched, then failed (and finished);
3. the reconciler errors the run and, in the same cycle, schedules revocation
   through the real scheduler queue;
4. the triggered task revokes the lease with ``PUT sys/leases/revoke`` and
   deletes the record.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from terrapod.config import VaultInstanceConfig
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service, vault_client, vault_lease_service
from terrapod.services.vault_client import VaultLease, VaultResponse
from tests.integration.test_vault_value_source import (
    _pool_with_listener,
    _workspace_with_vault_var,
)

pytestmark = pytest.mark.integration

LEASE_ID = "database/creds/ro/INTEGRATION-LEASE"
_REF = {
    "source": "vault",
    "engine": "dynamic",
    "mount": "database",
    "path": "creds/ro",
    "field": "password",
}


class _Vault:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(204)


async def test_an_errored_runs_leases_are_revoked(app, client, monkeypatch):
    from terrapod.config import settings
    from terrapod.redis.client import get_redis_client
    from terrapod.services import run_reconciler
    from tests.integration.conftest import admin_user, set_auth, set_listener_auth

    set_auth(app, admin_user())
    pool_id, listener_id = await _pool_with_listener(client, uuid.uuid4().hex[:8])
    ws_id, cv_id = await _workspace_with_vault_var(_REF, key="DB_PASSWORD")
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
        VaultInstanceConfig(
            name="default",
            default=True,
            address="https://vault.test:8200",
            auth={"method": "token", "mount": "token", "role": "n/a"},
            revoke_leases=True,
        )
    ]
    monkeypatch.setenv("TERRAPOD_VAULT_DEFAULT_SECRET", "static-token")
    try:
        # 1. The claim: the dynamic read mints a lease, and the claim records it.
        lease = VaultLease(
            duration=1800, renewable=True, received_at=datetime.now(UTC), lease_id=LEASE_ID
        )
        read = AsyncMock(return_value=VaultResponse({"password": "p"}, lease))
        with patch("terrapod.services.vault_source_service.read_secret_response", new=read):
            resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        assert resp.status_code == 200, resp.text

        redis = get_redis_client()
        record = vault_lease_service.record_id(run_id, "plan")
        stored = await redis.hgetall(f"{vault_lease_service.LEASES_PREFIX}{record}")
        assert [json.loads(v)["lease_id"] for k, v in stored.items() if k.startswith("lease:")] == [
            LEASE_ID
        ]

        # 2. The listener reports the Job launched, then failed and finished.
        base = f"/api/terrapod/v1/listeners/{listener_id}/runs/run-{run_id}"
        resp = await client.post(
            f"{base}/job-launched",
            json={"job_name": "tprun-int-plan", "job_namespace": "runners"},
        )
        assert resp.status_code == 200, resp.text
        resp = await client.post(
            f"{base}/job-status", json={"status": "failed", "phase": "plan", "terminal": True}
        )
        assert resp.status_code == 200, resp.text

        # 3. One reconcile of this run, then the lease watch, as a cycle does.
        async with get_db_session() as db:
            row = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
            await run_reconciler._reconcile_one(db, row, "terraform")
            await db.commit()
            await vault_lease_service.watch_pending(db)

        async with get_db_session() as db:
            final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert final.status == "errored"

        queued = [json.loads(i) for i in await redis.lrange("tp:sched:triggers", 0, -1)]
        items = [i for i in queued if i["type"] == vault_lease_service.TRIGGER]
        assert [i["payload"] for i in items] == [{"record": record}]
        assert LEASE_ID not in json.dumps(queued)

        # 4. The triggered task revokes the lease and deletes the record.
        vault = _Vault()
        real = httpx.AsyncClient
        vault_client.reset_token_cache()
        with patch.object(
            vault_client.httpx,
            "AsyncClient",
            lambda *a, **kw: real(transport=httpx.MockTransport(vault)),
        ):
            await vault_lease_service.handle_lease_revoke(items[0]["payload"])
    finally:
        settings.vault.enabled, settings.vault.instances = prior
        vault_client.reset_token_cache()

    (req,) = vault.requests
    assert req.method == "PUT"
    assert req.url.path == "/v1/sys/leases/revoke"
    assert json.loads(req.read()) == {"lease_id": LEASE_ID}
    assert req.headers["X-Vault-Token"] == "static-token"
    assert await redis.exists(f"{vault_lease_service.LEASES_PREFIX}{record}") == 0
    assert record not in await redis.smembers(vault_lease_service.PENDING_SET)
