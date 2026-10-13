"""The listener protocol's authorization boundary on the run endpoints.

Three holes closed on this line, all reachable by anyone holding a VALID
listener certificate for ANY agent pool -- the certificate authenticates a
listener, it does not say which runs that listener may touch:

- ``runner-token`` compared only ``run.listener_id`` against the path, never
  the caller's certificate. The path segment is chosen by the caller, so any
  listener could name the owning listener in the path and mint a runner token
  for a run in another pool (GHSA-g4j5).
- ``PATCH /listeners/{id}/runs/{run_id}`` accepted any transition the state
  machine allowed, including ``planned -> confirmed`` -- the confirm. That
  skips the apply capability check, the human confirmation, the staleness and
  plan-expiry guards and the mergeability gate (GHSA-3423).
- ``job-status`` and ``log-stream`` checked the certificate against the path
  and then never asked whether the run had anything to do with the caller, so
  a certificate from any pool could drive ``set_job_status`` for ANY run id in
  the deployment -- which the run reconciler reads back on its next cycle to
  decide ``complete_plan`` / ``complete_apply`` / ``transition_run`` -- and
  overwrite the live log a reviewer reads.

The boundary on those last two is the POOL, not the owning listener: the
reconciler publishes ``check_job_status`` and ``stream_logs`` to the pool's
channel, so every listener in the owning pool is sent the request and any of
them may answer. ``test_a_sibling_listener_in_the_same_pool_is_allowed`` is
what distinguishes that fix from the plausible, wrong one.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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


def _app(identity):
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
        app = _app(identity)
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


# ── status PATCH: the (from, to) allowlist (GHSA-3423) ────────────────────


class TestListenerReportableTransitions:
    @staticmethod
    async def _patch(identity, run, body, path_listener_id):
        app = _app(identity)
        transition = AsyncMock(return_value=run)
        complete_plan = AsyncMock(return_value=run)
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch.object(runs_router.run_service, "transition_run", transition),
            patch.object(runs_router.run_service, "complete_plan", complete_plan),
            patch.object(runs_router.run_service, "blocked_by", AsyncMock(return_value=None)),
            patch.object(runs_router, "_run_json", return_value={"data": {"id": "run-x"}}),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.patch(
                    f"/api/terrapod/v1/listeners/listener-{path_listener_id}/runs/run-{run.id}",
                    json=body,
                    headers=_CERT,
                )
        return res, transition, complete_plan

    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            ("planning", "planned"),
            ("planning", "errored"),
            ("planning", "queued"),
            ("applying", "applied"),
            ("applying", "errored"),
            ("applying", "confirmed"),
            ("canceling", "canceled"),
            ("canceling", "applied"),
            ("canceling", "errored"),
        ],
    )
    async def test_every_legitimate_pair_is_allowed(self, from_status, to_status):
        lid = uuid.uuid4()
        run = _run(status=from_status, listener_id=lid, pool_id=uuid.uuid4())

        res, transition, _ = await self._patch(
            _identity(lid, uuid.uuid4()), run, {"status": to_status}, lid
        )

        assert res.status_code == 200, res.text
        transition.assert_awaited_once()

    async def test_planned_to_confirmed_is_refused(self):
        """The vulnerability: the confirm, driven by a listener certificate.

        A listener has no business confirming an apply -- that is the gate the
        capability check, the human confirmation, the staleness/plan-expiry
        guards and the mergeability check all hang off.
        """
        lid = uuid.uuid4()
        run = _run(status="planned", listener_id=lid, pool_id=uuid.uuid4())

        res, transition, complete_plan = await self._patch(
            _identity(lid, uuid.uuid4()), run, {"status": "confirmed"}, lid
        )

        assert res.status_code == 403
        assert res.json()["detail"] == "A listener may not report 'planned' -> 'confirmed'"
        transition.assert_not_awaited()
        complete_plan.assert_not_awaited()

    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            # Every other pair the state machine allows but a listener has no
            # first-hand knowledge of -- the user-driven routes own these.
            ("planned", "applied"),
            ("planned", "discarded"),
            ("planning", "discarded"),
            ("planning", "canceled"),
            ("confirmed", "applying"),
            ("confirmed", "canceling"),
            ("queued", "planning"),
            ("pending", "queued"),
        ],
    )
    async def test_other_state_machine_valid_pairs_are_refused(self, from_status, to_status):
        lid = uuid.uuid4()
        run = _run(status=from_status, listener_id=lid, pool_id=uuid.uuid4())

        res, transition, complete_plan = await self._patch(
            _identity(lid, uuid.uuid4()), run, {"status": to_status}, lid
        )

        assert res.status_code == 403
        transition.assert_not_awaited()
        complete_plan.assert_not_awaited()

    @pytest.mark.parametrize("status", ["planned", "errored", "applied", "planning"])
    async def test_an_identical_re_report_is_allowed(self, status):
        """The listener retries this PATCH on timeout/5xx (`idempotent=True`).

        A retry that lands after the first attempt committed sees
        `run.status == target_status`, which is not in the pair allowlist --
        refusing it would turn a successful-but-unacknowledged report into a
        hard 403 on the retry.
        """
        lid = uuid.uuid4()
        run = _run(status=status, listener_id=lid, pool_id=uuid.uuid4())

        res, _, _ = await self._patch(_identity(lid, uuid.uuid4()), run, {"status": status}, lid)

        assert res.status_code == 200, res.text

    async def test_a_plan_completion_on_an_auto_apply_run_still_routes_through_complete_plan(self):
        """`planning -> planned` is allowed, and still takes the GUARDED path.

        The allowlist must not accidentally reroute the auto-apply plan
        completion away from `complete_plan`, which is where the #646/#647
        staleness and manual-lock guards live.
        """
        lid = uuid.uuid4()
        run = _run(status="planning", listener_id=lid, pool_id=uuid.uuid4(), plan_only=False)

        res, transition, complete_plan = await self._patch(
            _identity(lid, uuid.uuid4()), run, {"status": "planned", "has_changes": True}, lid
        )

        assert res.status_code == 200, res.text
        complete_plan.assert_awaited_once()
        transition.assert_not_awaited()


# ── job-status + log-stream: the POOL boundary ────────────────────────────


class TestJobStatusPoolBoundary:
    @staticmethod
    async def _post(identity, run, path_listener_id):
        app = _app(identity)
        setter = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.set_job_status", setter),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.post(
                    f"/api/terrapod/v1/listeners/listener-{path_listener_id}"
                    f"/runs/run-{run.id}/job-status",
                    json={"status": "succeeded", "phase": "plan", "terminal": True},
                    headers=_CERT,
                )
        return res, setter

    async def test_a_sibling_listener_in_the_same_pool_is_allowed(self):
        """THE test that distinguishes the right fix from the plausible one.

        The reconciler publishes `check_job_status` to the POOL's channel, so
        any listener in the owning pool may answer -- which one gets there
        first is a race. Keying this guard on `run.listener_id` (the form the
        claim/launch endpoints carry) would 403 a sibling answering a request
        it was legitimately sent, and this is the ONLY test that would fail.
        """
        pool = uuid.uuid4()
        owner, sibling = uuid.uuid4(), uuid.uuid4()
        run = _run(listener_id=owner, pool_id=pool)

        res, setter = await self._post(_identity(sibling, pool), run, sibling)

        assert res.status_code == 200, res.text
        setter.assert_awaited_once()

    async def test_the_owning_listener_is_allowed(self):
        pool = uuid.uuid4()
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=pool)

        res, setter = await self._post(_identity(lid, pool), run, lid)

        assert res.status_code == 200, res.text
        setter.assert_awaited_once()

    async def test_a_listener_from_another_pool_is_refused_and_writes_nothing(self):
        """The hole: any cert could drive set_job_status for ANY run id.

        The reconciler reads that Redis key back on its next cycle to decide
        complete_plan / complete_apply / transition_run, so the write is the
        damage -- it must not happen at all, which is why the guard has to
        PRECEDE the side effect rather than merely accompany it.
        """
        lid = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=uuid.uuid4())

        res, setter = await self._post(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 403
        assert (
            res.json()["detail"]
            == "Listener does not belong to the agent pool this run is assigned to"
        )
        setter.assert_not_awaited()

    async def test_a_run_with_no_pool_is_refused(self):
        """A run with no pool was never published to any listener channel, so
        a report about it is unsolicited."""
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)

        res, setter = await self._post(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 403
        setter.assert_not_awaited()


class TestLogStreamPoolBoundary:
    @staticmethod
    async def _put(identity, run, path_listener_id):
        app = _app(identity)
        redis = MagicMock()
        redis.setex = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch("terrapod.redis.client.publish_event", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.put(
                    f"/api/terrapod/v1/listeners/listener-{path_listener_id}"
                    f"/runs/run-{run.id}/log-stream?phase=plan",
                    content=b"some log bytes",
                    headers=_CERT,
                )
        return res, redis.setex

    async def test_a_sibling_listener_in_the_same_pool_is_allowed(self):
        """Same race as job-status: `stream_logs` goes to the pool's channel."""
        pool = uuid.uuid4()
        sibling = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=pool)

        res, setex = await self._put(_identity(sibling, pool), run, sibling)

        assert res.status_code == 204, res.text
        setex.assert_awaited_once()

    async def test_a_listener_from_another_pool_is_refused_and_writes_nothing(self):
        """The live log a reviewer reads must not be writable cross-pool."""
        lid = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=uuid.uuid4())

        res, setex = await self._put(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 403
        assert (
            res.json()["detail"]
            == "Listener does not belong to the agent pool this run is assigned to"
        )
        setex.assert_not_awaited()

    async def test_a_run_with_no_pool_is_refused(self):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)

        res, setex = await self._put(_identity(lid, uuid.uuid4()), run, lid)

        assert res.status_code == 403
        setex.assert_not_awaited()
