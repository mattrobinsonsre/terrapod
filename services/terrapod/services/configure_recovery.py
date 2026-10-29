"""Which hosts a configure still has to reach, and re-running only those (#1973).

A configure can succeed for some hosts and fail for others, and until this
landed Terrapod held no answer to "which ones are left". The terminal-states
half of #1973 shipped in `925b6984` -- a configure ends in the Run's own
`applied`/`errored`/`canceled`, with no `partial` -- and this is the recovery
half, which waited on its two inputs: the resolved-inventory artifact (#2021)
and the per-host rows (#1928).

## Why the remainder cannot be "the hosts that failed"

Measured against ansible-core 2.18.3 and recorded on the issue:
`v2_playbook_on_play_start` fires once per BATCH under `serial:`, and in a
`serial` + `any_errors_fatal` abort the untouched hosts appear in **no callback
event and in no PLAY RECAP at all**. So a host that was never attempted is
indistinguishable, from the results alone, from one that does not exist.

Subtracting from the run's own resolved inventory is what makes it answerable.
Build a retry from "the hosts that failed" instead and it fixes the failure,
reports success, and silently leaves the rest unconfigured -- which is worse
than failing, because it looks finished.

## Done is read off the status the API already derives

`host_status` is the ONE producer. It was derived inline in
`routers/runs.py::_host_result_json`, whose own docstring gave the reason --
"derived here rather than stored, so one reader cannot disagree with another
about what the counters mean" -- and that reason is exactly why this module
must not carry a second copy. The router now calls this.

That reuse also settles a question the issue's own formula gets wrong. The issue
states the remainder as `snapshot - (ok u changed u skipped)`, which puts an
IGNORED-FAILURE host in it. The user settled it the other way (2026-10-26):

* `ignore_errors: true` is the playbook author declaring that failure
  acceptable. Retrying re-runs a play they said was fine, and under the auto
  retry it would re-run to the attempt cap every time, for ever.
* `rescued` needs no decision at all: a `block`/`rescue` that recovered leaves
  `failures` at zero, so `host_status` already reports such a host as `ok` or
  `changed`.

So `OUTSTANDING` names the three statuses that mean "not configured": a host
that failed, one ansible could not reach, and one it never reported at all.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, RunHostResult

logger = structlog.get_logger(__name__)

#: A host that has not been configured. `not-reached` is the one that only the
#: subtraction can produce: it has no row at all, because ansible never
#: mentioned it.
OUTSTANDING: frozenset[str] = frozenset({"failed", "unreachable", "not-reached"})

# There is deliberately NO `STATUSES` constant enumerating everything
# `host_status` can return. One was written here and CodeQL flagged it unused
# (alert 552) -- correctly: nothing read it, and the web's `host-status.test.ts`
# derives the server's status set from `host_status`'s own `return` literals and
# from `OUTSTANDING` above, not from a list. A constant read only by a gate that
# asserts the constant is circular, and this is the shape recorded repeatedly in
# this codebase as a flag nothing reads. Contrast `_ROUTABLE_VERBS`, which looks
# unused to CodeQL and must be KEPT, because two gates genuinely read it: there
# the alert is dismissed, here the code goes.

#: What separates hosts in a `--limit`. The same convention
#: `inventory_narrowing` emits, for the same measured reason: `--limit` has no
#: grouping, so the platform renders a plain host list. A host cannot carry a
#: character that would re-parse, because every one here came from an inventory
#: ansible itself parsed.
LIMIT_SEPARATOR = ":"


def host_status(row: RunHostResult) -> str:
    """One host's outcome, derived from its counters.

    `unreachable` comes off `saw_unreachable` rather than the counter, because
    the recap counters cannot answer it once `ignore_unreachable` is in play --
    measured on #1928, where the callback fired while the summary read
    `unreachable=0 ignored=1`.
    """
    if row.saw_unreachable:
        return "unreachable"
    if row.failures:
        return "failed"
    if row.ignored:
        return "ignored-failure"
    if row.changed:
        return "changed"
    return "ok"


async def targeted_hosts(run: Run) -> set[str]:
    """Every host the run targeted, or an empty set.

    The run's own artifact rather than the live inventory: it is pinned at run
    creation (and re-resolved at a composite's hand-over), so it is what this
    run actually targeted even after the estate has moved.

    **Unioned with what the execution environment reported, when the artifact
    says it is partial** (#2033). A workspace that resolves dynamic inventory
    sources on the runner has an artifact listing only the sources the API could
    read, so on its own it is a SHORT denominator -- and a short denominator
    under-reports which hosts are still outstanding, which is the one thing this
    number exists to get right. The callback inside the EE reports the inventory
    as ansible actually resolved it; see
    `host_results_service.reported_inventory_hosts`.

    Unioned rather than preferred, deliberately. Either side can be the smaller
    one: the artifact is short when the API skipped a source, and the report is
    absent on an older runner image or a Job that died before uploading. A
    union cannot shrink the denominator whichever is missing, where a preference
    would silently take the short side on exactly the runs that most need the
    long one.

    An absent or unreadable artifact yields nothing. For the per-host VIEW that
    degrades to "show what ansible reported"; here it means an EMPTY remainder,
    which is the safe direction -- see `outstanding_hosts`.
    """
    from terrapod.services.host_results_service import reported_inventory_hosts
    from terrapod.storage import get_storage
    from terrapod.storage.keys import resolved_inventory_key

    key = resolved_inventory_key(str(run.workspace_id), str(run.id))
    try:
        raw = await get_storage().get(key)
    except Exception:  # noqa: BLE001 - any storage error degrades the same way
        return set()
    if not raw:
        return set()
    try:
        document = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(document, dict):
        return set()
    hosts = document.get("hosts")
    targeted = set(hosts.keys()) if isinstance(hosts, dict) else set()
    # Only for a partial resolution: where the API resolved everything, the
    # report agrees and reading the stream buys nothing but a storage call on
    # every per-host view.
    if document.get("partial"):
        targeted |= await reported_inventory_hosts(run)
    return targeted


def outstanding_from(targeted: set[str], rows: Sequence[RunHostResult]) -> list[str]:
    """The remainder, from a host set and the rows that reported, sorted.

    Pure, and separate from `outstanding_hosts` below, because the per-host VIEW
    has already fetched both by the time it wants this. Deriving it there from
    the same inputs rather than re-reading them is what makes the number the
    operator sees and the limit a retry carries the SAME computation rather than
    two that agree today.

    Two sources, unioned, because neither alone is the answer:

    * every targeted host with no successful row -- the `serial:` +
      `any_errors_fatal` case the module docstring opens with, and the only
      reason this is a subtraction rather than a filter;
    * every host that DID report, whose derived status is in `OUTSTANDING`.

    Sorted so a retry's `--limit` is stable between reads: the same remainder
    has to produce the same limit, or two readers disagree about what a retry
    would do.
    """
    done = {row.host for row in rows if host_status(row) not in OUTSTANDING}
    reported_outstanding = {row.host for row in rows if host_status(row) in OUTSTANDING}
    return sorted((targeted - done) | reported_outstanding)


async def outstanding_hosts(db: AsyncSession, run: Run) -> list[str]:
    """The hosts this configure has not configured, sorted.

    An empty list means nothing is outstanding -- INCLUDING the case where the
    artifact could not be read, which is deliberate. A failure to read the
    denominator must not be reported as "every host needs configuring", because
    that is the one answer that would have a retry touch the whole estate.
    """
    targeted = await targeted_hosts(run)
    rows = (
        (await db.execute(select(RunHostResult).where(RunHostResult.run_id == run.id)))
        .scalars()
        .all()
    )
    return outstanding_from(targeted, rows)


def remainder_limit(hosts: list[str]) -> str:
    """The `--limit` a retry carries for `hosts`.

    Empty yields empty, and a caller must NOT queue a retry on it: an empty
    `ansible_limit` means the workspace's own default, which for a retry would
    mean the whole inventory. `inventory_narrowing` has the same hazard from the
    other direction -- a limit matching nothing exits 1 -- and answers it by
    falling back to the workspace's limit; here the answer is to queue nothing
    at all, because there is nothing left to configure.
    """
    return LIMIT_SEPARATOR.join(hosts)


#: The `source` an automatic remainder retry carries. A column rather than a
#: marker in the message, mirroring `lifecycle_destroy_retry`'s use of
#: `catalog-lifecycle` / `autodiscovery-lifecycle`: the attempt count is counted
#: from the runs themselves, and a queryable source cannot be broken by someone
#: rewording a string.
#:
#: A MANUAL remainder retry keeps the source of the run it retries, so it does
#: NOT count against the automatic cap. That is deliberate: the cap exists to
#: bound what the platform does unattended, and a human who retries three times
#: has decided to.
RECOVERY_SOURCE = "configure-recovery"


async def _consecutive_recovery_attempts(db: AsyncSession, workspace_id) -> int:
    """How many automatic remainder retries this episode has already had.

    The tail of consecutive `configure-recovery` runs, newest first, so a prior
    episode separated by any other run is not counted -- exactly
    `lifecycle_destroy_retry._consecutive_failed_destroys`' walk, and counted
    from the runs rather than stored for the same reason: a column would be a
    second place the attempt lives.
    """
    rows = (
        (
            await db.execute(
                select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for candidate in rows:
        if candidate.source == RECOVERY_SOURCE:
            n += 1
        else:
            break
    return n


async def configure_retry_cycle() -> None:
    """Queue a configure for the hosts an errored configure did not reach.

    Registered with the distributed scheduler, so it is multi-replica safe
    without leader election -- the same arrangement
    `lifecycle_destroy_retry_cycle` uses, and for the same reason.

    **Off unless an operator turns it on.** `configure_retries` defaults to 0,
    which disables it, and that DIVERGES from the precedent it mirrors
    (`lifecycle_destroy_retries` defaults to 2). The reason is the thing being
    retried: a destroy is declarative and incremental, so re-running it is safe
    by construction, while an arbitrary playbook is whatever the operator wrote
    and may not be idempotent. So the platform will do this unattended only when
    someone has said it may (user's decision, 2026-10-26).

    **Errored only, never canceled.** A canceled configure is a partial
    configure and a human may retry it to the remainder by hand -- but having
    the scheduler resume it would override the person who cancelled it.

    A retry is an ORDINARY configure run carrying the remainder as its
    `ansible_limit` -- #1973's own words, "a new configure run whose limit is
    the computed remainder, which translates cleanly and needs no new concept".
    So it gets a run id, a log, notifications and an audit trail like any other,
    and nothing here needs a second lifecycle.
    """
    from terrapod.config import load_runner_config
    from terrapod.db.session import get_db_session

    cfg = load_runner_config()
    retries = cfg.configure_retries
    if retries <= 0:
        return
    backoff = timedelta(seconds=max(0, cfg.configure_retry_backoff_seconds))
    cutoff = datetime.now(UTC) - backoff

    async with get_db_session() as db:
        errored = (
            (
                await db.execute(
                    select(Run).where(
                        Run.is_configure.is_(True),
                        Run.status == "errored",
                        Run.updated_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in errored:
            try:
                await _retry_one(db, run, retries=retries)
            except Exception as exc:  # noqa: BLE001
                # One workspace's failure must not stop the others, and this is
                # best-effort by construction: the run it would retry is already
                # errored and stays that way.
                logger.warning(
                    "configure retry skipped",
                    run_id=str(run.id),
                    workspace_id=str(run.workspace_id),
                    err=str(exc),
                )


async def _latest_run(db: AsyncSession, workspace_id) -> Run | None:
    rows = await db.execute(
        select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


async def _retry_one(db: AsyncSession, run: Run, *, retries: int) -> None:
    """Queue one automatic retry, or decide there is nothing to do."""
    from terrapod.db.models import Workspace
    from terrapod.services import run_service, workspace_settings

    # Only if this errored configure is still the workspace's latest run. A
    # newer one -- a retry already queued, a manual retry, an apply -- supersedes
    # it and means we must not act. Same guard as the destroy retry, and it is
    # what stops two replicas or two cycles stacking retries.
    latest = await _latest_run(db, run.workspace_id)
    if latest is None or latest.id != run.id:
        return

    attempts = await _consecutive_recovery_attempts(db, run.workspace_id)
    if attempts >= retries:
        return  # exhausted; leave it errored for a human

    hosts = await outstanding_hosts(db, run)
    if not hosts:
        # Nothing outstanding: the configure errored for a reason that is not a
        # host set -- a malformed playbook, an unreadable inventory -- and
        # re-running the whole limit is exactly what `remainder_limit`'s empty
        # case exists to prevent.
        return

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        return

    try:
        retry = await run_service.create_run(
            db,
            workspace=ws,
            message=(
                f"Configure retry for {len(hosts)} outstanding host(s) "
                f"(attempt {attempts + 1}/{retries})"
            ),
            source=RECOVERY_SOURCE,
            is_configure=True,
            playbook=run.playbook or "",
            ansible_limit=remainder_limit(hosts),
            configuration_version_id=run.configuration_version_id,
            created_by="system",
        )
    except (
        run_service.PlanApplyNotEnabled,
        workspace_settings.ConfigureNotEnabled,
        workspace_settings.WorkspaceLocked,
    ):
        # CATCH AND SKIP, because this retry is the platform's own decision and
        # not something anybody asked for (#2116's rule: a requested run gets a
        # 4xx naming the column; a platform-decided one skips). The workspace's
        # declaration can have changed between the configure failing and this
        # cycle firing, and a workspace behaving exactly as declared must not
        # produce an error.
        #
        # INFO rather than warning, and that is the point rather than taste: the
        # attempt cap counts retries that were CREATED, so a refusal creates
        # nothing, leaves the count at zero, and this path is re-reached on
        # every cycle. At warning level that is a line every 30 seconds for
        # ever about a workspace that is configured correctly.
        logger.info(
            "configure retry skipped: workspace no longer declares a configure operation",
            workspace_id=str(run.workspace_id),
            errored_run=str(run.id),
        )
        return
    retry = await run_service.queue_run(db, retry)
    await db.commit()
    logger.info(
        "configure retry queued",
        workspace_id=str(run.workspace_id),
        errored_run=str(run.id),
        retry_run=str(retry.id),
        attempt=attempts + 1,
        of=retries,
        hosts=len(hosts),
    )
