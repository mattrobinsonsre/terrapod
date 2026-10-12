"""A listener may only report on runs in its OWN agent pool (GHSA-g4j5, cont.).

`PATCH /listeners/{lid}/runs/{rid}` and `create_runner_token` were closed by
keying on the run's owning listener. Two endpoints next to them were not:

* `POST   /listeners/{lid}/runs/{rid}/job-status`
* `PUT    /listeners/{lid}/runs/{rid}/logs`

Both verified the certificate against the listener id in the PATH -- which only
proves the caller is the listener it claims to be -- and then took the run from
the path with no check that the run was anything to do with that listener. So a
certificate for any listener in any pool could drive `set_job_status` for any
run id in the deployment, and the reconciler reads that key back on its next
cycle to decide `complete_plan` / `complete_apply` / `transition_run`. The log
endpoint is the same shape against the live log a reviewer reads.

**The boundary is the POOL, not the listener, and that is the whole subtlety.**
The obvious fix -- the `run.listener_id != l_uuid` check its siblings carry --
is wrong here and would break a legitimate path. `run_reconciler` publishes
`check_job_status` and `stream_logs` to `listener:events:{run.pool_id}`, the
POOL's channel (`publish_listener_event(pool_id, ...)`), so every listener in
the owning pool receives the request and any of them may answer it. Listeners
are deliberately stateless and interchangeable within a pool; binding these two
endpoints to `run.listener_id` would 403 a sibling listener doing exactly what
it was asked to do, and which one answers is a race.

So `test_a_sibling_listener_in_the_same_pool_is_allowed` is the test that
distinguishes the right fix from the plausible one, and it fails under it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers import runs as router


def _run(pool_id, *, listener_id=None, status="planning"):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.pool_id = pool_id
    run.listener_id = listener_id if listener_id is not None else uuid.uuid4()
    run.status = status
    return run


async def _job_status(*, run, caller_listener_id, caller_pool_id):
    """Drive the real handler; returns the `set_job_status` mock."""
    setter = AsyncMock()
    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch("terrapod.redis.client.set_job_status", setter),
    ):
        await router.report_job_status(
            listener_id=f"listener-{caller_listener_id}",
            run_id=f"run-{run.id}",
            body={"status": "succeeded", "phase": "plan"},
            identity=MagicMock(listener_id=caller_listener_id, pool_id=caller_pool_id),
            db=AsyncMock(),
        )
    return setter


async def _upload_logs(*, run, caller_listener_id, caller_pool_id):
    """Drive the real handler; returns the redis mock."""
    request = MagicMock()
    request.body = AsyncMock(return_value=b"some log bytes")
    redis = MagicMock()
    redis.setex = AsyncMock()
    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch("terrapod.redis.client.get_redis_client", MagicMock(return_value=redis)),
        patch("terrapod.redis.client.publish_event", AsyncMock()),
    ):
        await router.upload_log_stream(
            listener_id=f"listener-{caller_listener_id}",
            run_id=f"run-{run.id}",
            phase="plan",
            request=request,
            identity=MagicMock(listener_id=caller_listener_id, pool_id=caller_pool_id),
            db=AsyncMock(),
        )
    return redis


class TestTheOwningPoolMayReport:
    async def test_the_launching_listener_is_allowed(self):
        pool, lid = uuid.uuid4(), uuid.uuid4()
        run = _run(pool, listener_id=lid)
        setter = await _job_status(run=run, caller_listener_id=lid, caller_pool_id=pool)
        setter.assert_awaited_once()

    async def test_a_sibling_listener_in_the_same_pool_is_allowed(self):
        """The test that rejects the plausible fix.

        A DIFFERENT listener in the run's pool, which is the ordinary case:
        `check_job_status` goes to the pool's channel, so whichever listener
        reads it first answers, and that is usually not the one that launched
        the Job. A `run.listener_id` check -- the one every sibling endpoint
        carries -- 403s exactly here.
        """
        pool = uuid.uuid4()
        launched_by, answered_by = uuid.uuid4(), uuid.uuid4()
        run = _run(pool, listener_id=launched_by)

        setter = await _job_status(run=run, caller_listener_id=answered_by, caller_pool_id=pool)
        setter.assert_awaited_once()

        redis = await _upload_logs(run=run, caller_listener_id=answered_by, caller_pool_id=pool)
        redis.setex.assert_awaited_once()


class TestAnotherPoolMayNot:
    @pytest.mark.parametrize("drive", [_job_status, _upload_logs])
    async def test_a_listener_from_a_different_pool_is_refused(self, drive):
        run = _run(uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await drive(run=run, caller_listener_id=uuid.uuid4(), caller_pool_id=uuid.uuid4())
        assert exc.value.status_code == 403
        assert "agent pool" in str(exc.value.detail)

    @pytest.mark.parametrize("drive", [_job_status, _upload_logs])
    async def test_a_run_with_no_pool_is_refused(self, drive):
        """Unsolicited by construction, so it fails closed.

        `run_reconciler` guards `if run.pool_id:` before publishing either
        event, so nothing ever asked about a pool-less run and no listener has
        any business reporting on one.
        """
        run = _run(None)
        with pytest.raises(HTTPException) as exc:
            await drive(run=run, caller_listener_id=uuid.uuid4(), caller_pool_id=uuid.uuid4())
        assert exc.value.status_code == 403

    async def test_the_job_status_key_is_not_set_on_refusal(self):
        run = _run(uuid.uuid4())
        setter = AsyncMock()
        with (
            patch.object(router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.set_job_status", setter),
            pytest.raises(HTTPException),
        ):
            await router.report_job_status(
                listener_id=f"listener-{uuid.uuid4()}",
                run_id=f"run-{run.id}",
                body={"status": "succeeded", "phase": "plan"},
                identity=MagicMock(listener_id=uuid.uuid4(), pool_id=uuid.uuid4()),
                db=AsyncMock(),
            )
        setter.assert_not_awaited()

    async def test_the_log_key_is_not_set_on_refusal(self):
        run = _run(uuid.uuid4())
        request = MagicMock()
        request.body = AsyncMock(return_value=b"x")
        redis = MagicMock()
        redis.setex = AsyncMock()
        with (
            patch.object(router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.get_redis_client", MagicMock(return_value=redis)),
            pytest.raises(HTTPException),
        ):
            await router.upload_log_stream(
                listener_id=f"listener-{uuid.uuid4()}",
                run_id=f"run-{run.id}",
                phase="plan",
                request=request,
                identity=MagicMock(listener_id=uuid.uuid4(), pool_id=uuid.uuid4()),
                db=AsyncMock(),
            )
        redis.setex.assert_not_awaited()


class TestTheCertificateCheckStillComesFirst:
    async def test_a_mismatched_certificate_is_refused_even_with_the_right_pool(self):
        """Certificate first, so a correct pool cannot buy a forged path.

        The identity's pool matches the run's; only the listener id disagrees
        with the path. That must still be refused, and refused as a
        certificate failure rather than a pool one.
        """
        pool = uuid.uuid4()
        run = _run(pool)
        setter = AsyncMock()
        with (
            patch.object(router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.set_job_status", setter),
            pytest.raises(HTTPException) as exc,
        ):
            await router.report_job_status(
                listener_id=f"listener-{uuid.uuid4()}",
                run_id=f"run-{run.id}",
                body={"status": "succeeded", "phase": "plan"},
                identity=MagicMock(listener_id=uuid.uuid4(), pool_id=pool),
                db=AsyncMock(),
            )
        assert exc.value.status_code == 403
        assert "Certificate" in str(exc.value.detail)
        setter.assert_not_awaited()
