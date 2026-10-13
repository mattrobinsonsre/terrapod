"""The listener endpoints' authorization boundary on a run (v1.9.2 security patch).

Four listener-facing handlers in `routers/runs.py` take a listener id and a run
id from the path, and the path is chosen by the caller. A listener certificate
authenticates *which listener* is calling; it says nothing about which run the
caller may touch. These tests pin the three boundaries that were missing or
wrong, each driven through the real handler:

- ``create_runner_token`` had no caller's-certificate check at all, so a valid
  listener certificate from ANY agent pool could mint a runner token for a run
  it does not own (GHSA-g4j5). The check is also pinned to come BEFORE the run
  ownership check, because the ownership refusal leaks whether a listener owns
  a run to a caller holding no certificate for it.

- ``update_run_status`` accepted any state-machine-valid transition, including
  ``planned -> confirmed`` -- the apply decision, which bypasses the apply
  capability check, the human confirmation, the staleness and plan-expiry
  guards and the mergeability gate (GHSA-3423). It is now pair-keyed, because
  ``confirmed`` is the vulnerability from ``planned`` and a legitimate unclaim
  from ``applying``.

- ``report_job_status`` and ``upload_log_stream`` had the certificate check but
  no run check whatsoever, so a certificate from any pool could drive
  ``set_job_status`` for ANY run id in the deployment -- which the reconciler
  reads back on its next cycle to decide ``complete_plan`` / ``complete_apply``
  -- and overwrite the live log a reviewer reads. The boundary here is the
  POOL, not the owning listener: the reconciler publishes ``check_job_status``
  and ``stream_logs`` to the pool's channel, so any listener in the pool may
  answer. ``test_a_sibling_listener_in_the_same_pool_may_report`` is the test
  that distinguishes the correct fix from the plausible one -- it is the only
  one that fails if the guard is keyed on ``run.listener_id`` instead.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import ListenerIdentity
from terrapod.api.routers import runs as runs_router


def _identity(listener_id=None, pool_id=None) -> ListenerIdentity:
    return ListenerIdentity(
        listener_id=listener_id or uuid.uuid4(),
        name="listener-1",
        pool_id=pool_id or uuid.uuid4(),
        certificate_fingerprint="dead" * 16,
        certificate_expires_at=None,
    )


def _run(*, listener_id=None, pool_id=None, status="planning", plan_only=True):
    """A run mock with every attribute these handlers read set explicitly.

    `pool_id` and `listener_id` are set deliberately rather than left as
    auto-MagicMocks: the pool guard compares `identity.pool_id` against
    `run.pool_id`, and two distinct auto-MagicMocks never compare equal, so an
    unset `pool_id` would 403 for a reason that has nothing to do with the
    property under test.
    """
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.listener_id = listener_id
    run.pool_id = pool_id
    run.status = status
    run.plan_only = plan_only
    run.job_name = "tprun-x-plan"
    run.job_namespace = "terrapod-runners"
    return run


# ── create_runner_token: the missing caller's-certificate check ────────────


class TestRunnerTokenChecksTheCallersCertificate:
    """A runner token authenticates the Job's API calls for one run.

    Minting one for a run the caller does not own hands an attacker that run's
    binary-cache, provider-mirror and artifact upload/download access.
    """

    async def _mint(self, *, cert_lid, path_lid, run):
        db = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.auth.runner_tokens.generate_runner_token",
                MagicMock(return_value="runtok-x"),
            ) as gen,
        ):
            with pytest.raises(HTTPException) as exc:
                await runs_router.create_runner_token(
                    listener_id=f"listener-{path_lid}",
                    run_id=f"run-{run.id}",
                    body={},
                    identity=_identity(listener_id=cert_lid),
                    db=db,
                )
        return exc.value, gen

    async def test_a_certificate_for_another_listener_cannot_mint(self):
        """The cert authenticates listener A; the path names listener B.

        Before the fix this reached `generate_runner_token` whenever the run
        happened to be assigned to B -- a token for someone else's run.
        """
        path_lid = uuid.uuid4()
        run = _run(listener_id=path_lid)  # the path's listener DOES own the run
        err, gen = await self._mint(cert_lid=uuid.uuid4(), path_lid=path_lid, run=run)
        assert err.status_code == 403
        assert err.detail == "Certificate does not match the listener id in the path"
        gen.assert_not_called()

    async def test_the_certificate_is_checked_before_the_run_ownership(self):
        """Three distinct ids, so BOTH checks would fail and only order decides.

        The ordering is the security property: answering "Run not assigned to
        this listener" to a caller with no certificate for that listener
        confirms whether that listener owns that run. Pinning this needs a
        third id -- with the run's owner equal to the path id the ownership
        check cannot fail, so the test could not see the order at all.
        """
        cert_lid, path_lid, owner_lid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert len({cert_lid, path_lid, owner_lid}) == 3
        run = _run(listener_id=owner_lid)
        err, _ = await self._mint(cert_lid=cert_lid, path_lid=path_lid, run=run)
        assert err.status_code == 403
        assert err.detail == "Certificate does not match the listener id in the path"

    async def test_the_owning_listener_still_mints(self):
        """The legitimate path: cert, path and run owner are all the same id."""
        lid = uuid.uuid4()
        run = _run(listener_id=lid)
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.auth.runner_tokens.generate_runner_token",
                MagicMock(return_value="runtok-x"),
            ),
        ):
            resp = await runs_router.create_runner_token(
                listener_id=f"listener-{lid}",
                run_id=f"run-{run.id}",
                body={},
                identity=_identity(listener_id=lid),
                db=AsyncMock(),
            )
        assert resp.status_code == 200

    async def test_a_matching_certificate_on_someone_elses_run_is_still_refused(self):
        """The cert check does not replace the ownership check."""
        lid = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4())
        err, gen = await self._mint(cert_lid=lid, path_lid=lid, run=run)
        assert err.status_code == 403
        assert err.detail == "Run not assigned to this listener"
        gen.assert_not_called()


# ── update_run_status: the (from, to) allowlist ────────────────────────────


_LEGITIMATE_PAIRS = sorted(runs_router._LISTENER_REPORTABLE)


class TestListenerStatusReportsArePairAllowlisted:
    async def _report(self, *, status, target, plan_only=True):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, status=status, plan_only=plan_only)
        db = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch.object(runs_router, "_run_json", MagicMock(return_value={"data": {}})),
            patch(
                "terrapod.services.run_service.transition_run",
                AsyncMock(return_value=run),
            ) as transition,
            patch(
                "terrapod.services.run_service.complete_plan",
                AsyncMock(return_value=run),
            ) as complete,
            patch("terrapod.services.run_service.blocked_by", AsyncMock(return_value=None)),
        ):
            try:
                resp = await runs_router.update_run_status(
                    listener_id=f"listener-{lid}",
                    run_id=f"run-{run.id}",
                    body={"status": target},
                    identity=_identity(listener_id=lid),
                    db=db,
                )
            except HTTPException as e:
                return e, transition, complete
            return resp, transition, complete

    async def test_planned_to_confirmed_is_refused(self):
        """The vulnerability. `confirmed` is the apply decision, and reaching
        it this way skips the apply capability check, the human confirmation,
        the staleness and plan-expiry guards and the mergeability gate.
        """
        err, transition, complete = await self._report(status="planned", target="confirmed")
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        assert err.detail == "Listeners may not report this status transition"
        transition.assert_not_awaited()
        complete.assert_not_awaited()

    @pytest.mark.parametrize(("status", "target"), _LEGITIMATE_PAIRS)
    async def test_every_allowlisted_pair_is_accepted(self, status, target):
        resp, transition, complete = await self._report(status=status, target=target)
        assert not isinstance(resp, HTTPException), getattr(resp, "detail", resp)
        assert transition.await_count + complete.await_count == 1

    async def test_an_identical_re_report_is_still_allowed(self):
        """These calls are retried (`idempotent=True` on the listener side), so
        a repeat of the status the run already holds must stay a no-op rather
        than a 403. `(planned, planned)` is not in the allowlist, so only the
        `target != run.status` guard lets it through.
        """
        assert ("planned", "planned") not in runs_router._LISTENER_REPORTABLE
        resp, transition, _ = await self._report(status="planned", target="planned")
        assert not isinstance(resp, HTTPException), getattr(resp, "detail", resp)
        transition.assert_awaited_once()

    @pytest.mark.parametrize(
        ("status", "target"),
        [
            ("planned", "confirmed"),  # the apply decision — the vulnerability
            ("planned", "applied"),  # the reconciler's no-op apply, not a report
            ("planned", "discarded"),  # a user action
            ("planning", "canceled"),  # a user action
            ("planning", "discarded"),  # the post-plan-gate hold, not a report
            ("confirmed", "applying"),  # the claim, which `claim_next_run` owns
            ("confirmed", "canceled"),  # a user action
            ("queued", "planning"),  # the claim
            ("pending", "queued"),  # queueing, which the API owns
            ("applying", "canceling"),  # a user action
        ],
    )
    async def test_transitions_a_listener_never_makes_are_refused(self, status, target):
        """Each of these is state-machine-VALID, which is why the old check
        passed it. None is something a listener reports: they are user actions,
        API-owned claims, or reconciler decisions.
        """
        err, transition, complete = await self._report(status=status, target=target)
        assert isinstance(err, HTTPException), f"{status} -> {target} was accepted"
        assert err.status_code == 403
        transition.assert_not_awaited()
        complete.assert_not_awaited()

    async def test_the_plan_completion_still_routes_through_the_guarded_path(self):
        """`(planning, planned)` on an apply-capable run must still reach
        `complete_plan`, not a bare transition — that is what applies the
        post-plan gates and the auto-apply staleness guards (#665).
        """
        resp, transition, complete = await self._report(
            status="planning", target="planned", plan_only=False
        )
        assert not isinstance(resp, HTTPException), getattr(resp, "detail", resp)
        complete.assert_awaited_once()
        transition.assert_not_awaited()

    def test_the_allowlist_holds_no_pair_the_state_machine_rejects(self):
        """A pair the state machine would refuse anyway is dead weight that
        reads as a granted permission. Keeps the two in step.
        """
        from terrapod.services.run_service import VALID_TRANSITIONS

        for src, dst in runs_router._LISTENER_REPORTABLE:
            assert dst in VALID_TRANSITIONS.get(src, set()), f"{src} -> {dst}"


# ── report_job_status / upload_log_stream: the POOL boundary ───────────────


class TestJobStatusIsBoundedByThePool:
    """`set_job_status` writes the key the reconciler reads back to decide
    `complete_plan` / `complete_apply` / `transition_run`. An unbounded write
    there lets any pool's certificate drive another pool's run to a terminal.
    """

    async def _report(self, *, identity, path_lid, run):
        db = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.set_job_status", AsyncMock()) as setter,
        ):
            try:
                resp = await runs_router.report_job_status(
                    listener_id=f"listener-{path_lid}",
                    run_id=f"run-{run.id}",
                    body={"status": "succeeded", "phase": "plan"},
                    identity=identity,
                    db=db,
                )
            except HTTPException as e:
                return e, setter, db
            return resp, setter, db

    async def test_a_sibling_listener_in_the_same_pool_may_report(self):
        """THE test that distinguishes the correct fix from the plausible one.

        The reconciler publishes `check_job_status` to the POOL's channel
        (`publish_listener_event(str(run.pool_id), ...)`), so every listener in
        the owning pool receives the request and any of them may answer --
        which one wins is a race, and listeners are stateless and
        interchangeable within a pool. Keying the guard on `run.listener_id`
        the way the sibling endpoints do would 403 this legitimate reply, and
        this is the only test here that would catch it.
        """
        pool = uuid.uuid4()
        sibling = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=pool)  # owned by ANOTHER listener
        assert run.listener_id != sibling
        resp, setter, db = await self._report(
            identity=_identity(listener_id=sibling, pool_id=pool),
            path_lid=sibling,
            run=run,
        )
        assert not isinstance(resp, HTTPException), getattr(resp, "detail", resp)
        assert resp.status_code == 200
        setter.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_a_listener_from_another_pool_is_refused_and_writes_nothing(self):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=uuid.uuid4())
        err, setter, db = await self._report(
            identity=_identity(listener_id=lid, pool_id=uuid.uuid4()),
            path_lid=lid,
            run=run,
        )
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        assert err.detail == "Listener does not belong to the agent pool this run is assigned to"
        # The guard must PRECEDE the side effect, not merely accompany it.
        setter.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_a_run_with_no_pool_is_refused_and_writes_nothing(self):
        """Never published to any listener channel, so the report is
        unsolicited.
        """
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)
        err, setter, db = await self._report(
            identity=_identity(listener_id=lid, pool_id=uuid.uuid4()),
            path_lid=lid,
            run=run,
        )
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        setter.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_a_poolless_run_is_refused_even_by_a_poolless_identity(self):
        """Pins the `run.pool_id is None` half specifically.

        The inequality alone cannot refuse this: `None != None` is False, so a
        bare `identity.pool_id != run.pool_id` would ACCEPT a report for a run
        that was never published to any channel. Today's
        `get_listener_identity` cannot produce a null pool -- it builds the
        field with `uuid.UUID(listener["pool_id"])`, which raises rather than
        yielding None -- so this is defence in depth against a future identity
        source, and it is the only test that fails if the `is None` half is
        dropped. Setting it to None deliberately bypasses the dataclass's
        non-optional annotation, which is the point.
        """
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)
        identity = _identity(listener_id=lid)
        identity.pool_id = None  # type: ignore[assignment]
        err, setter, db = await self._report(identity=identity, path_lid=lid, run=run)
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        setter.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_the_certificate_check_still_comes_first(self):
        """A pool match does not excuse a certificate that names another
        listener -- the path segment is caller-chosen either way.
        """
        pool = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=pool)
        err, setter, _ = await self._report(
            identity=_identity(listener_id=uuid.uuid4(), pool_id=pool),
            path_lid=uuid.uuid4(),
            run=run,
        )
        assert isinstance(err, HTTPException)
        assert err.detail == "Certificate does not match the listener id in the path"
        setter.assert_not_awaited()


class TestLogStreamIsBoundedByThePool:
    """The same shape against the live log a reviewer reads while deciding
    whether to apply.
    """

    async def _upload(self, *, identity, path_lid, run):
        request = MagicMock()
        request.body = AsyncMock(return_value=b"log line\n")
        redis = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.redis.client.get_redis_client", MagicMock(return_value=redis)
            ) as getter,
            patch("terrapod.redis.client.publish_event", AsyncMock()),
        ):
            try:
                resp = await runs_router.upload_log_stream(
                    listener_id=f"listener-{path_lid}",
                    run_id=f"run-{run.id}",
                    phase="plan",
                    request=request,
                    identity=identity,
                    db=AsyncMock(),
                )
            except HTTPException as e:
                return e, redis, getter
            return resp, redis, getter

    async def test_a_sibling_listener_in_the_same_pool_may_upload(self):
        """Same race as job-status: `stream_logs` goes to the pool's channel."""
        pool = uuid.uuid4()
        sibling = uuid.uuid4()
        run = _run(listener_id=uuid.uuid4(), pool_id=pool, status="planning")
        resp, redis, _ = await self._upload(
            identity=_identity(listener_id=sibling, pool_id=pool),
            path_lid=sibling,
            run=run,
        )
        assert not isinstance(resp, HTTPException), getattr(resp, "detail", resp)
        assert resp.status_code == 204
        redis.setex.assert_awaited_once()

    async def test_a_listener_from_another_pool_is_refused_and_writes_nothing(self):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=uuid.uuid4())
        err, redis, getter = await self._upload(
            identity=_identity(listener_id=lid, pool_id=uuid.uuid4()),
            path_lid=lid,
            run=run,
        )
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        assert err.detail == "Listener does not belong to the agent pool this run is assigned to"
        getter.assert_not_called()
        redis.setex.assert_not_awaited()

    async def test_a_run_with_no_pool_is_refused_and_writes_nothing(self):
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)
        err, redis, getter = await self._upload(
            identity=_identity(listener_id=lid, pool_id=uuid.uuid4()),
            path_lid=lid,
            run=run,
        )
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        getter.assert_not_called()
        redis.setex.assert_not_awaited()

    async def test_a_poolless_run_is_refused_even_by_a_poolless_identity(self):
        """The `run.pool_id is None` half, as for job-status: `None != None` is
        False, so the inequality alone would accept this.
        """
        lid = uuid.uuid4()
        run = _run(listener_id=lid, pool_id=None)
        identity = _identity(listener_id=lid)
        identity.pool_id = None  # type: ignore[assignment]
        err, redis, getter = await self._upload(identity=identity, path_lid=lid, run=run)
        assert isinstance(err, HTTPException)
        assert err.status_code == 403
        getter.assert_not_called()
        redis.setex.assert_not_awaited()
