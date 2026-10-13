"""`runner-token` must check the CALLER's certificate, not just the path.

The endpoint compared only ``run.listener_id`` against the path's listener id
and never asked whether the caller's certificate named that listener. The path
segment is chosen by the caller, so a valid listener certificate from ANY agent
pool could name the owning listener in the path and mint a runner token for a
run it has nothing to do with (GHSA-g4j5).

The ORDER of the two checks is part of the fix: answering "Run not assigned to
this listener" to a caller holding no certificate for that listener would
confirm whether that listener owns that run to someone who should not have got
that far. ``test_the_certificate_check_precedes_the_ownership_check`` is what
pins it, and it needs THREE distinct listener ids -- the caller's certificate,
the path, and the run's owner -- so that BOTH checks would fail and only their
order decides the message.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import ListenerIdentity, get_listener_identity
from terrapod.api.routers import runs as runs_router
from terrapod.db.session import get_db

_BASE = "http://test"
_CERT = {"X-Terrapod-Client-Cert": "irrelevant-mocked"}


def _identity(listener_id, pool_id):
    return ListenerIdentity(
        listener_id=listener_id,
        name="listener-1",
        pool_id=pool_id,
        certificate_fingerprint="dead" * 16,
        certificate_expires_at=None,
    )


def _run(*, status="planning", listener_id=None, pool_id=None, plan_only=True):
    """A run mock with EVERY field the guards read set explicitly.

    An auto-MagicMock attribute is a distinct object per access, so leaving
    `pool_id` to the mock would make `identity.pool_id != run.pool_id` true by
    construction and every pool test would "pass" without the guard being the
    reason.
    """
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.status = status
    run.listener_id = listener_id
    run.pool_id = pool_id
    run.plan_only = plan_only
    run.job_name = "tprun-abc-plan"
    return run


def _app(identity, run):
    app = create_app()
    app.dependency_overrides[get_listener_identity] = lambda: identity
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


# ── runner-token: the caller's certificate (GHSA-g4j5) ────────────────────


class TestRunnerTokenChecksTheCallersCertificate:
    """`runner-token` mints a credential for a run; both checks must be live.

    The ordering test needs THREE distinct listener ids -- the caller's
    certificate, the path, and the run's owner -- so that BOTH checks would
    fail and only their order decides which message comes back. With the run's
    owner pinned to the path id the ordering cannot be observed at all.
    """

    @staticmethod
    async def _post(identity, run, path_listener_id):
        app = _app(identity, run)
        with patch.object(runs_router, "_get_run", AsyncMock(return_value=run)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                return await client.post(
                    f"/api/terrapod/v1/listeners/listener-{path_listener_id}"
                    f"/runs/run-{run.id}/runner-token",
                    json={},
                    headers=_CERT,
                )

    async def test_a_foreign_certificate_cannot_mint_for_the_owning_listener(self):
        """The hole: a cert for pool B names pool A's listener in the path."""
        owner = uuid.uuid4()
        attacker = uuid.uuid4()
        run = _run(listener_id=owner, pool_id=uuid.uuid4())

        res = await self._post(_identity(attacker, uuid.uuid4()), run, owner)

        assert res.status_code == 403
        assert res.json()["detail"] == "Certificate does not match the listener id in the path"

    async def test_the_certificate_check_precedes_the_ownership_check(self):
        """Three distinct ids, so both checks would fail.

        Answering "Run not assigned to this listener" here would confirm
        whether that listener owns that run to a caller holding no certificate
        for it.
        """
        cert_lid, path_lid, owner_lid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        run = _run(listener_id=owner_lid, pool_id=uuid.uuid4())

        res = await self._post(_identity(cert_lid, uuid.uuid4()), run, path_lid)

        assert res.status_code == 403
        assert res.json()["detail"] == "Certificate does not match the listener id in the path"

    async def test_a_matching_certificate_on_a_run_it_does_not_own_is_still_refused(self):
        """The pre-existing ownership check stays live behind the new one."""
        lid = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=uuid.uuid4())

        res = await self._post(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 403
        assert res.json()["detail"] == "Run not assigned to this listener"

    async def test_the_owning_listener_still_gets_a_token(self):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=uuid.uuid4())

        res = await self._post(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 200, res.text
        assert res.json()["token"]
