"""Recording, watching and revoking a run phase's Vault leases (#1649).

Redis is an in-memory fake with the handful of commands the service uses, so
the real key layout, TTLs and pending-set bookkeeping run. Vault is mocked at
``revoke_lease``; the HTTP layer is covered in ``test_vault_client_revoke``.

The properties pinned here, each a hard requirement:

- **terminal only**: revocation is scheduled once the phase's Job is reported
  succeeded, failed or deleted — and not while it is still running, not while
  Kubernetes is retrying a pod within the Job, not at a plan result, and not
  for an apply claim that was handed back (#1646);
- **option off means no calls**: nothing records, enqueues or touches Redis or
  Vault when no instance has ``revoke_leases`` on;
- **never raises into a run**: a Redis or Vault failure is logged and the
  leases expire at their TTL;
- **no lease id** in any log line or trigger payload;
- **idempotent**: a missing record is a no-op, and the record is deleted after.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import VaultConfig, settings
from terrapod.services import vault_lease_service as svc
from terrapod.services.vault_client import GONE, REVOKED, VaultDenied, VaultLease

LEASE_A = "database/creds/ro/LEASE-A-MUST-NOT-LEAK"
LEASE_B = "database/creds/ro/LEASE-B-MUST-NOT-LEAK"


class FakeRedis:
    """The subset of redis.asyncio (decode_responses=True) the service uses."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        if field is not None:
            h[field] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def exists(self, key):
        return int(key in self.hashes or key in self.strings)

    async def ttl(self, key):
        if key not in self.hashes and key not in self.strings:
            return -2
        return self.ttls.get(key, -1)

    async def expire(self, key, seconds):
        self.ttls[key] = seconds

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        self.sets.get(key, set()).difference_update(members)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def sscan(self, key, cursor=0, count=100):
        """Enough of SSCAN to iterate in bounded batches and wrap at the end.

        The real cursor is opaque and may repeat a member; what the service
        relies on is only that a batch is bounded and that successive calls
        eventually reach every member, which an index satisfies.
        """
        members = sorted(self.sets.get(key, set()))
        start = int(cursor) if int(cursor) < len(members) else 0
        chunk = members[start : start + count]
        nxt = start + count
        return (0 if nxt >= len(members) else nxt), chunk

    async def delete(self, *keys):
        for k in keys:
            self.hashes.pop(k, None)
            self.strings.pop(k, None)
            self.ttls.pop(k, None)

    async def get(self, key):
        return self.strings.get(key)

    async def setex(self, key, ttl, value):
        self.strings[key] = value
        self.ttls[key] = ttl


def _watched_redis():
    """Patch the Redis getter so a test can assert it was never called.

    Asserted by call count, not by making it raise: every path here is
    best-effort and swallows exceptions, so a raise would pass unnoticed.
    """
    return patch("terrapod.redis.client.get_redis_client")


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        yield fake


@pytest.fixture(autouse=True)
def _vault_config():
    prior = settings.vault
    settings.vault = VaultConfig(
        enabled=True,
        instances=[
            {
                "name": "db",
                "address": "https://vault.test:8200",
                "revoke_leases": True,
                "auth": {"method": "token"},
            },
            {"name": "kv", "address": "https://vault2.test:8200"},
        ],
    )
    yield
    settings.vault = prior


@pytest.fixture
def option_off():
    settings.vault = VaultConfig(
        enabled=True, instances=[{"name": "db", "address": "https://vault.test:8200"}]
    )


@pytest.fixture
def enqueue():
    with patch("terrapod.services.scheduler.enqueue_trigger", AsyncMock(return_value=True)) as m:
        yield m


@pytest.fixture
def publish():
    with patch("terrapod.redis.client.publish_listener_event", AsyncMock()) as m:
        yield m


def _lease(lease_id=LEASE_A, duration=1800) -> VaultLease:
    return VaultLease(
        duration=duration, renewable=True, received_at=datetime.now(UTC), lease_id=lease_id
    )


def _run(**kw):
    run = MagicMock()
    run.id = kw.get("id", uuid.uuid4())
    run.status = kw.get("status", "planning")
    run.pool_id = kw.get("pool_id", uuid.uuid4())
    run.job_name = kw.get("job_name")
    run.job_namespace = kw.get("job_namespace", "runners")
    run.apply_started_at = kw.get("apply_started_at")
    return run


def _db(run):
    db = AsyncMock()
    db.get = AsyncMock(return_value=run)
    return db


async def _recorded(redis, run, phase="plan", *, job=None, leases=None):
    """A record as a successful claim (and, with ``job``, a launch) leaves it."""
    await svc.record_leases(run.id, phase, leases or [("db", _lease())])
    if job:
        await redis.hset(
            svc._key(svc.record_id(run.id, phase)),
            "job",
            json.dumps({"name": job, "namespace": "runners"}),
        )
    return svc.record_id(run.id, phase)


async def _report(redis, run, phase, status, **extra):
    await redis.setex(
        f"tp:job_status:{run.id}:{phase}", 120, json.dumps({"status": status, **extra})
    )


# ── Recording ─────────────────────────────────────────────────────────────


class TestRecording:
    async def test_records_each_lease_under_the_run_and_phase(self, redis):
        run_id = uuid.uuid4()
        await svc.record_leases(run_id, "plan", [("db", _lease(LEASE_A)), ("db", _lease(LEASE_B))])

        key = f"tp:vault:leases:{run_id}:plan"
        entries = [json.loads(v) for v in redis.hashes[key].values()]
        assert sorted(e["lease_id"] for e in entries) == [LEASE_A, LEASE_B]
        assert {e["instance"] for e in entries} == {"db"}
        assert redis.sets[svc.PENDING_SET] == {f"{run_id}:plan"}

    async def test_the_record_outlives_the_longest_lease_by_an_hour(self, redis):
        run_id = uuid.uuid4()
        await svc.record_leases(
            run_id, "apply", [("db", _lease(duration=600)), ("db", _lease(LEASE_B, 7200))]
        )
        assert redis.ttls[f"tp:vault:leases:{run_id}:apply"] == 7200 + 3600

    async def test_a_later_claim_extends_the_ttl_and_never_shortens_it(self, redis):
        run_id = uuid.uuid4()
        key = f"tp:vault:leases:{run_id}:plan"
        await svc.record_leases(run_id, "plan", [("db", _lease(duration=600))])
        assert redis.ttls[key] == 4200
        await svc.record_leases(run_id, "plan", [("db", _lease(LEASE_B, 9000))])
        assert redis.ttls[key] == 12600
        await svc.record_leases(run_id, "plan", [("db", _lease("x", 60))])
        assert redis.ttls[key] == 12600
        assert len(redis.hashes[key]) == 3

    async def test_a_redis_failure_never_raises_and_logs_no_lease_id(self):
        broken = MagicMock()
        broken.hset = AsyncMock(side_effect=ConnectionError(f"boom {LEASE_A}"))
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=broken),
            patch.object(svc, "logger") as log,
        ):
            await svc.record_leases(uuid.uuid4(), "plan", [("db", _lease())])
        log.warning.assert_called_once()
        assert LEASE_A not in str(log.mock_calls)

    async def test_redis_unreachable_at_all_never_raises(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            await svc.record_leases(uuid.uuid4(), "plan", [("db", _lease())])

    async def test_nothing_to_record_touches_no_redis(self):
        with _watched_redis() as get_redis:
            await svc.record_leases(uuid.uuid4(), "plan", [])
        get_redis.assert_not_called()


class TestRecordJob:
    async def test_a_plan_job_is_attached_to_the_plan_record(self, redis):
        run = _run()
        rec = await _recorded(redis, run, "plan")
        await svc.record_job(run, "tprun-x-plan", "runners")
        assert json.loads(redis.hashes[svc._key(rec)]["job"]) == {
            "name": "tprun-x-plan",
            "namespace": "runners",
        }

    async def test_an_apply_job_is_attached_to_the_apply_record(self, redis):
        run = _run(status="applying", apply_started_at=datetime.now(UTC))
        rec = await _recorded(redis, run, "apply")
        await svc.record_job(run, "tprun-x-apply", "runners")
        assert "job" in redis.hashes[svc._key(rec)]
        assert svc._key(svc.record_id(run.id, "plan")) not in redis.hashes

    async def test_a_phase_with_nothing_recorded_gets_no_write(self, redis):
        await svc.record_job(_run(), "tprun-x-plan", "runners")
        assert redis.hashes == {}

    async def test_a_redis_failure_never_raises(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            await svc.record_job(_run(), "tprun-x-plan", "runners")


# ── The option off: no calls at all ───────────────────────────────────────


class TestOptionOff:
    async def test_no_redis_and_no_vault_anywhere(self, option_off, enqueue):
        run = _run()
        with (
            _watched_redis() as get_redis,
            patch.object(svc, "revoke_lease", AsyncMock()) as revoke,
        ):
            await svc.record_leases(run.id, "plan", [("db", _lease())])
            await svc.record_job(run, "tprun-x-plan", "runners")
            await svc.watch_pending(_db(run))
            await svc.handle_lease_revoke({"record": svc.record_id(run.id, "plan")})
        get_redis.assert_not_called()
        revoke.assert_not_awaited()
        enqueue.assert_not_awaited()

    async def test_vault_disabled_counts_as_off(self):
        settings.vault = VaultConfig(enabled=False)
        assert settings.vault.revocation_enabled is False


# ── Watching: terminal only ───────────────────────────────────────────────


class TestWatchSchedulesOnlyOnATerminalJob:
    @pytest.mark.parametrize("status", ["succeeded", "failed", "deleted"])
    async def test_a_terminal_job_schedules_revocation(self, redis, enqueue, publish, status):
        run = _run(status="planned", job_name="tprun-x-plan")
        rec = await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", status, terminal=True)

        await svc.watch_pending(_db(run))

        enqueue.assert_awaited_once_with(
            svc.TRIGGER, {"record": rec}, dedup_key=f"vault_revoke:{rec}", dedup_ttl=300
        )

    async def test_a_job_retrying_a_pod_is_not_terminal(self, redis, enqueue, publish):
        # The pod counters say failed, but the Job has no Failed condition:
        # Kubernetes is starting a new pod, which will use the credentials.
        run = _run(status="planning", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", "failed", terminal=False)

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()

    async def test_a_listener_that_predates_the_terminal_flag_is_taken_at_its_word(
        self, redis, enqueue, publish
    ):
        run = _run(status="errored", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", "failed")

        await svc.watch_pending(_db(run))

        enqueue.assert_awaited_once()

    @pytest.mark.parametrize("status", ["running", "unschedulable"])
    async def test_a_live_job_is_not_terminal(self, redis, enqueue, publish, status):
        run = _run(status="planning", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", status)

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()

    async def test_a_plan_result_alone_never_revokes(self, redis, enqueue, publish):
        # The runner POSTed plan-result, so the run is `planned` — but its pod
        # is still alive (log upload, cost, scans). No Job report yet.
        run = _run(status="planned", job_name="tprun-x-plan")
        rec = await _recorded(redis, run, job="tprun-x-plan")

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()
        # Instead it asks about the Job, which the reconciler no longer does.
        publish.assert_awaited_once()
        pool_id, event = publish.await_args.args
        assert pool_id == str(run.pool_id)
        assert event["event"] == "check_job_status"
        assert event["job_name"] == "tprun-x-plan"
        assert event["phase"] == "plan"
        assert event["run_id"] == str(run.id)
        assert redis.sets[svc.PENDING_SET] == {rec}

    async def test_no_duplicate_question_while_the_reconciler_is_asking(
        self, redis, enqueue, publish
    ):
        run = _run(status="planning", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")

        await svc.watch_pending(_db(run))

        publish.assert_not_awaited()
        enqueue.assert_not_awaited()

    async def test_the_plan_job_is_still_watched_after_the_apply_started(
        self, redis, enqueue, publish
    ):
        # Auto-apply: the apply Job has replaced `job_name` on the run, but the
        # plan Job recorded at its launch is the one this record waits for.
        run = _run(status="applying", job_name="tprun-x-apply", apply_started_at=datetime.now(UTC))
        await _recorded(redis, run, "plan", job="tprun-x-plan")

        await svc.watch_pending(_db(run))

        assert publish.await_args.args[1]["job_name"] == "tprun-x-plan"
        enqueue.assert_not_awaited()

    async def test_an_apply_claim_handed_back_revokes_nothing(self, redis, enqueue, publish):
        # #1646: a Vault outage during an apply claim returns the run to
        # `confirmed` and clears apply_started_at. No Job ran; the next claim
        # will record and deliver its own credentials.
        run = _run(status="confirmed", job_name=None, apply_started_at=None)
        await _recorded(redis, run, "apply")

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()
        publish.assert_not_awaited()

    async def test_a_run_waiting_for_its_job_to_launch_waits(self, redis, enqueue, publish):
        run = _run(status="planning", job_name=None)
        await _recorded(redis, run)

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()
        publish.assert_not_awaited()

    async def test_a_terminal_run_with_no_known_job_asks_about_the_job_by_name(
        self, redis, enqueue, publish
    ):
        # The launch report was lost and the run has since errored. Something
        # may still be running under the bundled listener's name for the Job,
        # so ask rather than assume — the answer ends the wait either way.
        run = _run(status="errored", job_name=None)
        await _recorded(redis, run)

        await svc.watch_pending(_db(run))

        enqueue.assert_not_awaited()
        assert publish.await_args.args[1]["job_name"] == f"tprun-{str(run.id)[:16]}-plan"


class TestWatchBookkeeping:
    async def test_a_missing_record_is_dropped_and_nothing_scheduled(self, redis, enqueue):
        await redis.sadd(svc.PENDING_SET, f"{uuid.uuid4()}:plan")
        await svc.watch_pending(_db(None))
        assert redis.sets[svc.PENDING_SET] == set()
        enqueue.assert_not_awaited()

    async def test_a_vanished_run_is_left_to_expire(self, redis, enqueue, publish):
        run = _run()
        await _recorded(redis, run, job="tprun-x-plan")
        await svc.watch_pending(_db(None))
        enqueue.assert_not_awaited()
        publish.assert_not_awaited()

    async def test_one_bad_record_does_not_stop_the_rest(self, redis, enqueue, publish):
        good = _run(status="errored", job_name="tprun-good-plan")
        bad = _run(status="planned")
        await _recorded(redis, good, job="tprun-good-plan")
        await _recorded(redis, bad, job="tprun-bad-plan")
        await _report(redis, good, "plan", "failed", terminal=True)
        db = AsyncMock()
        db.get = AsyncMock(side_effect=RuntimeError("db"))

        await svc.watch_pending(db)

        (call,) = enqueue.await_args_list
        assert call.args[1] == {"record": svc.record_id(good.id, "plan")}

    async def test_the_walk_is_bounded_and_the_next_cycle_carries_on(self, redis, enqueue, publish):
        """A record lives for its longest lease plus an hour, so an estate with
        hour-long database credentials accumulates thousands of them — and the
        reconciler runs every two seconds. One cycle must not re-read them all;
        across cycles every one is still reached."""
        runs = [_run(status="planned") for _ in range(3)]
        for r in runs:
            await _recorded(redis, r, job=f"tprun-{str(r.id)[:8]}-plan")

        watch_one = AsyncMock()
        with patch.object(svc, "_watch_one", watch_one), patch.object(svc, "_WATCH_BATCH", 2):
            await svc.watch_pending(_db(None))
            first = [c.args[2] for c in watch_one.await_args_list]
            await svc.watch_pending(_db(None))

        seen = [c.args[2] for c in watch_one.await_args_list]
        assert len(first) == 2, first
        assert set(seen) == set(await redis.smembers(svc.PENDING_SET))

    async def test_an_enqueue_failure_never_raises(self, redis, publish):
        run = _run(status="errored", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", "failed", terminal=True)
        with patch(
            "terrapod.services.scheduler.enqueue_trigger",
            AsyncMock(side_effect=ConnectionError("redis")),
        ):
            await svc.watch_pending(_db(run))

    async def test_the_trigger_payload_carries_no_lease_id(self, redis, enqueue, publish):
        run = _run(status="errored", job_name="tprun-x-plan")
        await _recorded(redis, run, job="tprun-x-plan")
        await _report(redis, run, "plan", "succeeded", terminal=True)
        with patch.object(svc, "logger") as log:
            await svc.watch_pending(_db(run))
        assert LEASE_A not in str(enqueue.await_args_list)
        assert LEASE_A not in str(log.mock_calls)


# ── Revoking ──────────────────────────────────────────────────────────────


class TestRevoke:
    async def test_revokes_every_lease_then_deletes_the_record(self, redis):
        run = _run()
        rec = await _recorded(redis, run, leases=[("db", _lease(LEASE_A)), ("db", _lease(LEASE_B))])
        revoke = AsyncMock(return_value=REVOKED)
        with patch.object(svc, "revoke_lease", revoke), patch.object(svc, "logger") as log:
            await svc.handle_lease_revoke({"record": rec})

        assert sorted(c.args[1] for c in revoke.await_args_list) == [LEASE_A, LEASE_B]
        assert all(c.args[0].name == "db" for c in revoke.await_args_list)
        assert svc._key(rec) not in redis.hashes
        assert rec not in redis.sets[svc.PENDING_SET]
        assert LEASE_A not in str(log.mock_calls)
        assert log.info.call_args.kwargs["revoked"] == 2

    async def test_a_missing_record_is_a_no_op(self, redis):
        revoke = AsyncMock()
        with patch.object(svc, "revoke_lease", revoke):
            await svc.handle_lease_revoke({"record": f"{uuid.uuid4()}:plan"})
        revoke.assert_not_awaited()

    async def test_running_twice_is_harmless(self, redis):
        rec = await _recorded(redis, _run())
        revoke = AsyncMock(return_value=REVOKED)
        with patch.object(svc, "revoke_lease", revoke):
            await svc.handle_lease_revoke({"record": rec})
            await svc.handle_lease_revoke({"record": rec})
        assert revoke.await_count == 1

    async def test_a_lease_vault_no_longer_holds_counts_as_done(self, redis):
        rec = await _recorded(redis, _run())
        with (
            patch.object(svc, "revoke_lease", AsyncMock(return_value=GONE)),
            patch.object(svc, "logger") as log,
        ):
            await svc.handle_lease_revoke({"record": rec})
        assert log.info.call_args.kwargs["gone"] == 1
        log.warning.assert_not_called()
        assert svc._key(rec) not in redis.hashes

    async def test_an_instance_that_stopped_revoking_is_left_to_expire(self, redis):
        rec = await _recorded(redis, _run(), leases=[("kv", _lease())])
        revoke = AsyncMock()
        with patch.object(svc, "revoke_lease", revoke):
            await svc.handle_lease_revoke({"record": rec})
        revoke.assert_not_awaited()

    async def test_a_vault_failure_is_logged_without_the_lease_and_the_record_goes(self, redis):
        rec = await _recorded(
            redis, _run(), leases=[("db", _lease(LEASE_A)), ("db", _lease(LEASE_B))]
        )
        revoke = AsyncMock(side_effect=[VaultDenied("HTTP 403"), REVOKED])
        with patch.object(svc, "revoke_lease", revoke), patch.object(svc, "logger") as log:
            await svc.handle_lease_revoke({"record": rec})

        # One failure does not stop the next lease.
        assert revoke.await_count == 2
        log.warning.assert_called_once()
        assert LEASE_A not in str(log.mock_calls)
        assert LEASE_B not in str(log.mock_calls)
        assert svc._key(rec) not in redis.hashes

    async def test_an_unexpected_error_never_escapes(self, redis):
        rec = await _recorded(redis, _run())
        with patch.object(svc, "revoke_lease", AsyncMock(side_effect=RuntimeError(LEASE_A))):
            with patch.object(svc, "logger") as log:
                await svc.handle_lease_revoke({"record": rec})
        assert LEASE_A not in str(log.mock_calls)

    async def test_uses_the_instance_timeout_and_its_stored_secret(self, redis, monkeypatch):
        monkeypatch.setenv("TERRAPOD_VAULT_DB_SECRET", "static-token")
        rec = await _recorded(redis, _run())
        revoke = AsyncMock(return_value=REVOKED)
        with patch.object(svc, "revoke_lease", revoke):
            await svc.handle_lease_revoke({"record": rec})
        assert revoke.await_args.kwargs == {
            "timeout": settings.vault.timeout_seconds,
            "static_token": "static-token",
        }
