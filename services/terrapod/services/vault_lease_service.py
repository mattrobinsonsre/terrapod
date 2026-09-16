"""Revoking a run phase's dynamic Vault leases once its Job has ended (#1649).

A dynamic secret (``database/creds``, ``aws/creds``, ``pki/issue`` …) is minted
with a lease. Each run phase reads its own, and without this the credential
stays live for the role's whole TTL however short the phase was. With
``vault.instances[].revoke_leases`` on, the lease is revoked once the phase is
over.

The three steps, and where each lives:

1. **Record** — at the run claim (``next_run``), :func:`record_leases` writes
   ``{instance, lease_id}`` into a Redis hash keyed by run and phase, and adds
   the record to a pending set. When the listener reports the phase's Job
   (``job-launched``), :func:`record_job` adds its name and namespace.
2. **Watch** — every reconcile cycle, :func:`watch_pending` looks at each
   pending record. It revokes only once the phase's **Job is terminal**: the
   listener reported it succeeded, failed or gone, and (from a listener new
   enough to say so) the Job carries its own Complete/Failed condition, so a
   pod that is being retried within the Job is not mistaken for the end. That
   is the single place the "is it over" decision is made, and it covers every
   way a phase ends — success, failure, a cancel or discard that reaps the Job,
   a Job the reconciler abandoned. A ``plan-result`` POST does not count: the
   pod is still alive when it sends one.
3. **Revoke** — the watch enqueues a scheduler triggered task, and
   :func:`handle_lease_revoke` calls ``PUT sys/leases/revoke`` for each lease
   with bounded retry, then deletes the record.

**No new point of failure.** Every step is best-effort and catches its own
errors: a run is never failed, delayed or rolled back by any of it. If a record
is lost — Redis down at the claim, a Redis flush, a revoke that keeps failing —
the lease simply expires at its Vault TTL, which is exactly what happens with
the option off. With the option off nothing here touches Redis or Vault at all.

**What is deliberately not revoked.** A claim that fails (Vault denied a later
read, or went away and the run was returned to the queue) records nothing: the
leases it minted were never delivered and expire at their TTL as today. Only a
successful claim's leases are recorded, so an unclaim can never revoke a
credential a later claim of the same phase has handed to a pod.

A lease id is never logged, never put in an exception message, and never put in
a trigger payload — the payload names the record, and the handler reads the ids
from Redis.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter

import structlog

from terrapod.config import settings
from terrapod.services.vault_client import GONE, REVOKED, VaultError, revoke_lease

logger = structlog.get_logger("vault_leases")

#: One Redis hash per run phase: ``lease:<n>`` fields hold ``{instance,
#: lease_id}``, and the ``job`` field the phase Job's name and namespace.
LEASES_PREFIX = "tp:vault:leases:"
#: Record ids (``<run_id>:<phase>``) the reconciler still has to watch. Members
#: whose hash has expired are dropped the next time the watch meets them.
PENDING_SET = "tp:vault:lease_pending"
#: The scheduler trigger that revokes one record's leases.
TRIGGER = "vault_lease_revoke"
#: How long a record outlives its longest lease. Past that the leases have
#: expired in Vault anyway, so there is nothing left to revoke.
RECORD_GRACE_SECONDS = 3600
#: Pending records examined per reconcile cycle, and where the last one stopped.
#: Large enough that an ordinary estate is covered in a single pass, small
#: enough that a large one cannot make the cycle walk everything (#1690).
_WATCH_BATCH = 200
_WATCH_CURSOR = "tp:vault:lease_watch_cursor"
#: Listener job-status reports that mean the phase's Job has ended.
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "deleted"})

_JOB_FIELD = "job"
_LEASE_FIELD_PREFIX = "lease:"
_PHASES = ("plan", "apply")


def record_id(run_id: object, phase: str) -> str:
    return f"{run_id}:{phase}"


def _key(record: str) -> str:
    return f"{LEASES_PREFIX}{record}"


def _redis():
    from terrapod.redis.client import get_redis_client

    return get_redis_client()


# ── Recording ─────────────────────────────────────────────────────────────


async def record_leases(run_id: object, phase: str, leases: list) -> None:
    """Record a successful claim's leases for revocation when the phase ends.

    ``leases`` is ``[(instance_name, VaultLease)]`` from the resolver, which
    only collects leases of instances with ``revoke_leases`` on.

    Never raises. A failure here must not fail the claim — the run is already
    claimed and its credentials are minted — so it is logged and the leases
    expire at their Vault TTL, as they would with the option off.
    """
    if not leases or not settings.vault.revocation_enabled:
        return
    record = record_id(run_id, phase)
    try:
        redis = _redis()
        key = _key(record)
        mapping = {
            f"{_LEASE_FIELD_PREFIX}{uuid.uuid4().hex}": json.dumps(
                {"instance": name, "lease_id": lease.lease_id}
            )
            for name, lease in leases
        }
        await redis.hset(key, mapping=mapping)
        # Outlive the longest lease by an hour. Extended, never shortened, when
        # a later claim of the same phase appends: its leases may be longer.
        ttl = max(lease.duration for _, lease in leases) + RECORD_GRACE_SECONDS
        if await redis.ttl(key) < ttl:
            await redis.expire(key, ttl)
        await redis.sadd(PENDING_SET, record)
    except Exception as e:  # noqa: BLE001 - best-effort by design, see docstring
        logger.warning(
            "could not record Vault leases for revocation; they will expire at their TTL",
            record=record,
            count=len(leases),
            error=type(e).__name__,
        )


async def record_job(run, job_name: str, job_namespace: str) -> None:
    """Attach the phase Job's coordinates to the run phase's lease record.

    Called when the listener reports ``job-launched``. Only touches a record
    that exists — a run with nothing to revoke gets no Redis write. Never
    raises; without the coordinates the watch falls back to the run row.
    """
    if not settings.vault.revocation_enabled or not job_name:
        return
    # The apply claim stamps apply_started_at; the plan claim never does.
    phase = "apply" if run.apply_started_at is not None else "plan"
    record = record_id(run.id, phase)
    try:
        redis = _redis()
        key = _key(record)
        if not await redis.exists(key):
            return
        await redis.hset(
            key, _JOB_FIELD, json.dumps({"name": job_name, "namespace": job_namespace or ""})
        )
    except Exception as e:  # noqa: BLE001 - best-effort by design
        logger.warning(
            "could not record the Job for Vault lease revocation",
            record=record,
            error=type(e).__name__,
        )


# ── Watching ──────────────────────────────────────────────────────────────


def _phase_polled_by_reconciler(run, phase: str, job_name: str) -> bool:
    """Whether the ordinary reconcile path already asks about this Job."""
    if run.job_name != job_name:
        return False
    if phase == "plan":
        return run.status == "planning"
    return run.status in ("applying", "canceling")


def _job_for(run, phase: str, recorded: dict | None) -> dict | None:
    """The phase Job to ask about, or None when there is nothing to ask yet.

    In order: what the listener reported at launch; the run row's Job while it
    still belongs to this phase (``job_name`` is cleared on entering apply);
    and, once the run is terminal, the name the bundled listener gives the
    phase Job — asking about it answers "is anything still running?" even when
    the launch report was lost. Before the run is terminal an unknown Job means
    wait: a pre-launch run may yet launch one.
    """
    if recorded and recorded.get("name"):
        return recorded
    belongs = (
        (run.apply_started_at is None) if phase == "plan" else (run.apply_started_at is not None)
    )
    if belongs and run.job_name:
        return {"name": run.job_name, "namespace": run.job_namespace or ""}

    from terrapod.services.run_service import TERMINAL_STATES

    if run.status in TERMINAL_STATES:
        return {"name": f"tprun-{str(run.id)[:16]}-{phase}", "namespace": run.job_namespace or ""}
    return None


async def _phase_job_ended(run_id: str, phase: str) -> bool:
    """Whether the listener has reported the phase's Job as terminal.

    ``terminal`` is the Job's own Complete/Failed condition as the listener saw
    it; ``False`` means the Job is still retrying a pod. A lagging listener
    sends no ``terminal`` and its status is taken as it is.
    """
    from terrapod.redis.client import get_job_report_from_redis

    report = await get_job_report_from_redis(run_id, phase)
    if not report or report.get("status") not in TERMINAL_JOB_STATUSES:
        return False
    return report.get("terminal") is not False


async def _enqueue(record: str) -> None:
    from terrapod.services.scheduler import enqueue_trigger

    await enqueue_trigger(
        TRIGGER, {"record": record}, dedup_key=f"vault_revoke:{record}", dedup_ttl=300
    )


async def watch_pending(db) -> None:
    """Enqueue revocation for every pending record whose phase Job has ended.

    Called once per reconcile cycle, after the cycle's transitions have
    committed. Each record is handled in its own ``try`` so one bad record
    cannot stop the others, and the caller wraps the whole call too.

    The set is walked a batch at a time rather than whole (#1690). A record
    lives for its longest lease plus an hour, so an estate with hour-long
    database credentials and busy run traffic accumulates thousands of them,
    and the reconciler runs every two seconds — walking the lot meant
    re-reading every member's hash, job report and run thirty times a minute
    for an answer that changes when a Job ends. The cursor is kept in Redis, so
    each cycle carries on where the last stopped and every member is still
    reached, just not all at once.
    """
    if not settings.vault.revocation_enabled:
        return
    redis = _redis()
    cursor = 0
    stored = await redis.get(_WATCH_CURSOR)
    if stored:
        try:
            cursor = int(stored)
        except (TypeError, ValueError):
            cursor = 0
    cursor, records = await redis.sscan(PENDING_SET, cursor=cursor, count=_WATCH_BATCH)
    await redis.setex(_WATCH_CURSOR, RECORD_GRACE_SECONDS, str(cursor))
    for record in records:
        try:
            await _watch_one(db, redis, record)
        except Exception as e:  # noqa: BLE001 - one record must not stop the rest
            logger.warning("Vault lease watch failed", record=record, error=type(e).__name__)


async def _watch_one(db, redis, record: str) -> None:
    from terrapod.db.models import Run

    run_id, _, phase = record.rpartition(":")
    if phase not in _PHASES or not run_id:
        await redis.srem(PENDING_SET, record)
        return
    fields = await redis.hgetall(_key(record))
    if not fields:
        # Expired (the leases have too) or already revoked.
        await redis.srem(PENDING_SET, record)
        return

    if await _phase_job_ended(run_id, phase):
        await _enqueue(record)
        return

    run = await db.get(Run, uuid.UUID(run_id))
    if run is None or not run.pool_id:
        # Nothing to ask. Leave it: the record expires with its leases.
        return
    recorded = json.loads(fields[_JOB_FIELD]) if fields.get(_JOB_FIELD) else None
    job = _job_for(run, phase, recorded)
    if job is None or _phase_polled_by_reconciler(run, phase, job["name"]):
        return

    # The run has moved past this phase (a plan whose runner already posted
    # its result, a cancelled or discarded run), so the ordinary reconcile
    # path no longer asks about this Job. Ask; the answer lands in the job
    # status Redis key and a later cycle acts on it.
    from terrapod.redis.client import publish_listener_event

    await publish_listener_event(
        str(run.pool_id),
        {
            "event": "check_job_status",
            "request_id": str(uuid.uuid4()),
            "run_id": str(run.id),
            "job_name": job["name"],
            "job_namespace": job.get("namespace", ""),
            "phase": phase,
        },
    )


# ── Revoking ──────────────────────────────────────────────────────────────


async def handle_lease_revoke(payload: dict) -> None:
    """Scheduler trigger: revoke one record's leases, then delete the record.

    Idempotent: a missing record is a no-op, and a lease Vault no longer holds
    counts as done. A lease whose instance is gone from the configuration, or
    no longer has ``revoke_leases`` on, is left to expire. A failure is logged
    and the lease expires at its TTL; the record is deleted either way, because
    the retry is bounded — inside :func:`vault_client.revoke_lease` — and not
    open-ended.
    """
    record = str(payload.get("record") or "")
    if not record or not settings.vault.revocation_enabled:
        return
    from terrapod.services.vault_source_service import _secret_for

    redis = _redis()
    key = _key(record)
    fields = await redis.hgetall(key)
    if not fields:
        await redis.srem(PENDING_SET, record)
        return

    outcomes: Counter = Counter()
    for name, raw in fields.items():
        if not name.startswith(_LEASE_FIELD_PREFIX):
            continue
        try:
            entry = json.loads(raw)
            inst = settings.vault.instance_named(str(entry.get("instance", "")))
            lease_id = str(entry.get("lease_id") or "")
        except (ValueError, TypeError, AttributeError):
            outcomes["malformed"] += 1
            continue
        if inst is None or not inst.revoke_leases or not lease_id:
            outcomes["skipped"] += 1
            continue
        try:
            outcome = await revoke_lease(
                inst,
                lease_id,
                timeout=settings.vault.timeout_seconds,
                static_token=_secret_for(inst.name),
            )
            outcomes[outcome] += 1
        except VaultError as e:
            # The message names the instance and HTTP status, never the lease.
            outcomes["failed"] += 1
            logger.warning(
                "could not revoke a Vault lease; it will expire at its TTL",
                record=record,
                instance=inst.name,
                error=str(e),
            )
        except Exception as e:  # noqa: BLE001 - one lease must not stop the rest
            outcomes["failed"] += 1
            logger.warning(
                "could not revoke a Vault lease; it will expire at its TTL",
                record=record,
                instance=inst.name,
                error=type(e).__name__,
            )

    await redis.delete(key)
    await redis.srem(PENDING_SET, record)
    logger.info(
        "Vault leases processed",
        record=record,
        revoked=outcomes[REVOKED],
        gone=outcomes[GONE],
        failed=outcomes["failed"],
        skipped=outcomes["skipped"] + outcomes["malformed"],
    )
