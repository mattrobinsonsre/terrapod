"""Vault lease revocation covers every engine's runner Job (#1649).

Revocation waits for the phase's Job to end, so it depends on four things
agreeing for every engine: the Job's name, the listener's job-status report
(with its `terminal` flag), the record the claim and `job-launched` write, and
the reconcile cycle that acts on the report. None of them branch on the engine:
the Job is named by the engine-neutral builder (`runner/job_template`) with the
platform phase (`plan`/`apply`, never Pulumi's `preview`/`update`), and the
listener and the watch key on that name and phase. These tests pin it for
**every registered engine** (gated or not), read from the registry rather than
listed by hand, so an engine added later is covered the moment it is registered.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module
from terrapod import engines
from terrapod.config import VaultConfig, settings
from terrapod.services import run_reconciler, vault_lease_service
from terrapod.services.vault_client import VaultResponse
from tests.api.test_next_run_vault_leases import LEASE_ID, _claim, _config, _dyn, _lease, _reads
from tests.runner.test_engine_options_seam import ATTRS, _runner_config
from tests.services.test_listener import _make_listener
from tests.services.test_run_reconciler_vault_leases import _cycle, _db, _run
from tests.services.test_vault_lease_service import FakeRedis

#: Every engine this build contains, gated or not. A new strategy lands here.
ENGINES = sorted(engines._REGISTRY)
PHASES = ["plan", "apply"]


def test_the_table_covers_every_engine_this_line_runs():
    """Guards the parametrisation below from silently shrinking."""
    assert {"terraform", "pulumi"} <= set(ENGINES)


@pytest.fixture(autouse=True)
def _restore_vault():
    prior = settings.vault
    settings.vault = VaultConfig(
        enabled=True,
        instances=[{"name": "db", "address": "https://v", "revoke_leases": True}],
    )
    yield
    settings.vault = prior


@pytest.fixture(autouse=True)
def _shutdown():
    event = asyncio.Event()
    prior = listener_module._shutdown
    listener_module._shutdown = event
    yield event
    listener_module._shutdown = prior


def _job_name(engine: str, run_id: str, phase: str) -> str:
    """The Job name the engine's strategy gives a phase, called as the listener does."""
    s = engines._REGISTRY[engine]
    spec = s.build_job_spec(
        options=s.options_from_attrs(ATTRS, phase),
        run_id=run_id,
        phase=phase,
        runner_config=_runner_config(),
        auth_secret_name=f"tprun-{run_id[:16]}-{phase}-auth",
        vars_secret_name=f"tprun-{run_id[:16]}-{phase}-vars",
        env_vars=[],
        terraform_vars=[],
        execution_hooks=[],
        git_auth=[],
        vault_files=[],
        resource_cpu="1",
        resource_memory="2Gi",
        ca_secret_name="",
    )
    return spec["metadata"]["name"]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("engine", ENGINES)
def test_the_watch_fallback_names_each_engines_phase_job(engine, phase):
    """With no launch report, the watch asks about the Job the builder names.

    A mismatch would have the watch ask about a Job that never existed: the
    listener would answer `deleted`, and the leases would be revoked while the
    real Job was still using them.
    """
    rid = uuid.uuid4()
    run = MagicMock(
        id=rid, status="errored", job_name=None, job_namespace="runners", apply_started_at=None
    )
    job = vault_lease_service._job_for(run, phase, None)
    assert job is not None
    assert job["name"] == _job_name(engine, str(rid), phase)
    assert job["name"].endswith(f"-{phase}")  # the platform phase, never preview/update


@pytest.mark.parametrize("engine", ENGINES)
async def test_the_listener_reports_terminal_for_each_engines_job(engine, _shutdown):
    """`terminal` comes from the Job's own conditions, whichever engine built it."""
    name = _job_name(engine, str(uuid.uuid4()), "plan")
    bodies = []
    for finished in (True, False):
        listener = _make_listener(_shutdown)
        listener._http_client = MagicMock()
        post = AsyncMock()
        is_finished = AsyncMock(return_value=finished)
        with (
            patch("terrapod.runner.job_manager.get_job_status", AsyncMock(return_value="failed")),
            patch("terrapod.runner.job_manager.job_is_finished", is_finished),
            patch(
                "terrapod.runner.job_manager.get_pod_terminated_info",
                AsyncMock(return_value=None),
            ),
            patch("terrapod.runner.job_manager.get_job_failure_info", AsyncMock(return_value=None)),
            patch.object(listener_module, "arequest_with_retry", post),
        ):
            await listener._handle_check_job_status(
                {"job_name": name, "job_namespace": "ns", "run_id": "r1", "phase": "plan"}
            )
        assert is_finished.await_args.args == (name,)
        bodies.append(post.await_args.kwargs["json"])
    assert [b["terminal"] for b in bodies] == [True, False]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("engine", ENGINES)
async def test_a_claim_records_its_leases_under_the_platform_phase(engine, phase):
    """A Pulumi claim records under `plan`/`apply`, the phase its Job is named by."""
    settings.vault = _config(revoke=True)
    read = _reads(VaultResponse({"password": "p"}, _lease()))
    resp, run, transition, record, logs = await _claim(
        [_dyn("DB_PASSWORD")], read, phase=phase, engine=engine
    )
    assert resp.status_code == 200, resp.body
    transition.assert_not_awaited()
    run_id, recorded_phase, leases = record.await_args.args
    assert (run_id, recorded_phase) == (run.id, phase)
    assert [lease.lease_id for _, lease in leases] == [LEASE_ID]
    assert LEASE_ID not in logs


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("engine", ENGINES)
async def test_the_launched_job_is_recorded_watched_and_revoked(engine, phase):
    """record_leases → job-launched → the watch asks about that Job → a
    terminal report schedules revocation, for each engine's Job."""
    redis = FakeRedis()
    rid = uuid.uuid4()
    name = _job_name(engine, str(rid), phase)
    record = vault_lease_service.record_id(rid, phase)
    run = MagicMock(
        id=rid,
        status="planning" if phase == "plan" else "applying",
        pool_id=uuid.uuid4(),
        job_name=name,
        job_namespace="runners",
        apply_started_at=datetime.now(UTC) if phase == "apply" else None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=run)
    publish = AsyncMock()
    enqueue = AsyncMock()
    with (
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch("terrapod.redis.client.publish_listener_event", publish),
        patch("terrapod.services.scheduler.enqueue_trigger", enqueue),
    ):
        await vault_lease_service.record_leases(rid, phase, [("db", _lease())])
        await vault_lease_service.record_job(run, name, "runners")
        stored = await redis.hgetall(f"{vault_lease_service.LEASES_PREFIX}{record}")
        assert json.loads(stored["job"]) == {"name": name, "namespace": "runners"}

        # The run has left the phase, so the ordinary reconcile path no longer
        # asks; the watch asks about exactly this Job.
        run.status = "errored"
        await vault_lease_service.watch_pending(db)
        event = publish.await_args.args[1]
        assert (event["event"], event["job_name"], event["job_namespace"], event["phase"]) == (
            "check_job_status",
            name,
            "runners",
            phase,
        )
        enqueue.assert_not_awaited()

        # The listener's answer lands; the next cycle schedules revocation.
        await redis.setex(
            f"tp:job_status:{rid}:{phase}", 120, json.dumps({"status": "failed", "terminal": True})
        )
        await vault_lease_service.watch_pending(db)
    assert enqueue.await_args.args == (vault_lease_service.TRIGGER, {"record": record})


@pytest.mark.parametrize("engine", ENGINES)
async def test_a_reconcile_cycle_revokes_after_each_engines_job_ends(engine):
    """The cycle carries out the engine's terminal decision and then the watch
    schedules revocation — the engine is resolved per run, the watch is not."""
    run = _run()
    redis = FakeRedis()
    await redis.hset(
        f"tp:vault:leases:{run.id}:plan",
        mapping={"lease:1": json.dumps({"instance": "db", "lease_id": "L"})},
    )
    await redis.sadd(vault_lease_service.PENDING_SET, f"{run.id}:plan")
    await redis.setex(
        f"tp:job_status:{run.id}:plan", 120, json.dumps({"status": "failed", "terminal": True})
    )

    async def to_status(_db, r, target, **_kw):
        r.status = target
        return r

    db = _db([run], engine=engine)
    enqueue = AsyncMock()
    async with _cycle(db):
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch("terrapod.redis.client.publish_listener_event", AsyncMock()),
            patch(
                "terrapod.services.run_service.transition_run", AsyncMock(side_effect=to_status)
            ) as transition,
            patch("terrapod.services.scheduler.enqueue_trigger", enqueue),
        ):
            await run_reconciler.reconcile_runs()

    assert transition.await_args.args[2] == "errored"
    assert enqueue.await_args.args == (vault_lease_service.TRIGGER, {"record": f"{run.id}:plan"})
