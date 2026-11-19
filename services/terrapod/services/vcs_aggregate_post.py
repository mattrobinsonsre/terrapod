"""Computing and posting the aggregate plan, policy and apply commit statuses.

Gathers the latest runs at a PR head and the poller's record of what it affects, then posts
the verdicts from the poll pass and the run lifecycle. Never green from unreadable evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping
from typing import Any, NamedTuple

import httpx
from sqlalchemy import func, select

from terrapod.api.metrics import VCS_AGGREGATE_STATUS_POSTS, VCS_AGGREGATE_STATUS_REJECTED
from terrapod.config import aggregate_statuses_enabled, settings
from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import (
    github_service,
    gitlab_service,
    run_links,
    vcs_provider,
    vcs_rate_limit,
)
from terrapod.services.run_service import TERMINAL_STATES
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.vcs_aggregate_status import (
    Aggregate,
    PREvaluation,
    Verdict,
    WorkspaceRun,
    aggregate,
    github_state,
    gitlab_state,
)
from terrapod.services.vcs_pr_decisions import PRDecisions, Stored, load, repo_scope
from terrapod.services.vcs_status_comment import (
    _LIVE_SESSION_STATES,
    _collect_gates,
    _release_comment_lock,
)

logger = get_logger(__name__)

#: `(run id, status, has_changes)` of a run whose new state the caller already
#: knows, for when the database row has not caught up with the trigger.
Overlay = tuple[uuid.UUID, str, "bool | None"]
Overlays = Mapping[uuid.UUID, list[tuple[str, "bool | None"]]]

# One post can wait out (retries + 1) rate-limit sleeps; the lock is also
# extended before every post, so this only has to cover one.
_POST_BUDGET = (max(github_service._MAX_RETRIES, gitlab_service._MAX_RETRIES) + 1) * max(
    github_service._MAX_RETRY_WAIT_SECONDS, gitlab_service._MAX_RETRY_WAIT_SECONDS
)
_LOCK_TTL = int(_POST_BUDGET) + 60
_DIRTY_TTL = _LOCK_TTL * 2
_LAST_TTL = 7 * 24 * 3600
_REJECTED_TTL = 3600
_REJECTED_AUTH_TTL = 300
_BACKOFF_BASE = 30
_BACKOFF_CAP = 900
_GREEN_RECHECK = 600
_MAX_ROUNDS = 3
_MAX_PARALLEL_PUBLISHES = 4
_TRANSIENT_4XX = frozenset({408, 429})
_EXTEND_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
#: How far along a run is; a terminal state beats every other.
_RANK = {
    s: i
    for i, s in enumerate(
        ("pending", "queued", "planning", "planned", "confirmed", "applying", "configuring")
    )
}


class _LockLost(Exception):
    """The lock expired or changed hands before a post: stop, someone else recomputes."""


def _as_overlays(overlay: Overlay | Overlays | None) -> Overlays:
    if overlay is None:
        return {}
    if isinstance(overlay, tuple):
        return {overlay[0]: [(overlay[1], overlay[2])]}
    return overlay


def _overlaid(run: Run, overlay: Overlays | None) -> tuple[str, bool | None]:
    """The furthest-along state known for a run, never moving a terminal one back."""
    options = (overlay or {}).get(run.id)
    if not options or run.status in TERMINAL_STATES:
        return run.status, run.has_changes
    best = (run.status, run.has_changes)
    for status, has_changes in options:
        if status in TERMINAL_STATES and best[0] not in TERMINAL_STATES:
            best = (status, has_changes)
        elif best[0] not in TERMINAL_STATES and _RANK.get(status, 0) > _RANK.get(best[0], 0):
            best = (status, has_changes)
    return best


async def collect_head_rows(
    db,
    scope: str,
    pr_number: int,
    head_sha: str,
    overlay: Overlay | Overlays | None = None,
    gates_cache: dict | None = None,
) -> list[WorkspaceRun]:
    """The latest run per workspace for this PR at this head, on this repository.

    Module-impact runs are left out (they carry another repository's PR number),
    as is every run at another commit.
    """
    result = await db.execute(
        select(Run, Workspace)
        .join(Workspace, Workspace.id == Run.workspace_id)
        .where(
            Run.vcs_pull_request_number == pr_number,
            Run.vcs_commit_sha == head_sha,
            Run.source.is_distinct_from("module-test"),
        )
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    overlays = _as_overlays(overlay)
    latest: dict[uuid.UUID, tuple[Workspace, Run]] = {}
    for run, ws in result.all():
        if ws.id in latest:
            continue
        conn = ws.vcs_connection
        slug = vcs_provider.repo_path(conn, ws.vcs_repo_url or "") if conn is not None else None
        if slug is None or repo_scope(conn, slug) != scope:
            continue
        latest[ws.id] = (ws, run)
    rows: list[WorkspaceRun] = []
    for ws, run in sorted(latest.values(), key=lambda pair: pair[0].name):
        status, has_changes = _overlaid(run, overlays)
        rows.append(
            WorkspaceRun(
                name=ws.name,
                mode=ws.vcs_workflow,
                status=status,
                plan_finished=run.plan_finished_at is not None,
                apply_started=run.apply_started_at is not None,
                has_changes=has_changes,
                gates=await _collect_gates(db, run, gates_cache),
                run_url=run_links.run_url(ws.id, run.id),
            )
        )
    return rows


async def _evaluate(
    db,
    scope: str,
    pr_number: int,
    head_sha: str,
    overlays: Overlays | None,
    gates_cache: dict | None = None,
) -> tuple[Aggregate, PREvaluation | None]:
    evaluation = await load(scope, pr_number, head_sha)
    rows = await collect_head_rows(db, scope, pr_number, head_sha, overlays, gates_cache)
    return aggregate(rows, evaluation), evaluation


async def compute(
    db, conn: Any, repo: str, pr_number: int, head_sha: str, overlay: Overlay | None = None
) -> Aggregate:
    """The three verdicts for one PR head. A missing record can only give pending."""
    scope = repo_scope(conn, repo)
    return (await _evaluate(db, scope, pr_number, head_sha, _as_overlays(overlay)))[0]


async def compute_recorded(
    db, conn: Any, repo: str, pr_number: int, head_sha: str, gates_cache: dict | None = None
) -> Aggregate | None:
    """`compute`, but None when no record vouches for the head (nothing to report)."""
    scope = repo_scope(conn, repo)
    agg, evaluation = await _evaluate(db, scope, pr_number, head_sha, None, gates_cache)
    return agg if evaluation is not None else None


async def _sessions(db, scope: str, repo: str, pr_number: int) -> list[PRSession]:
    rows = (
        (
            await db.execute(
                select(PRSession).where(
                    func.lower(PRSession.repo) == repo.strip().lower(),
                    PRSession.pr_number == pr_number,
                )
            )
        )
        .scalars()
        .all()
    )
    return [s for s in rows if repo_scope(s.vcs_connection, s.repo) == scope]


async def _repository_connections(db, scope: str) -> list[Any]:
    """The connections of every workspace on the repository, lowest id first."""
    repo = scope.rsplit(":", 1)[-1]
    rows = (
        await db.execute(
            select(Workspace.vcs_repo_url, VCSConnection)
            .join(VCSConnection, Workspace.vcs_connection_id == VCSConnection.id)
            .where(func.lower(Workspace.vcs_repo_url).contains(repo, autoescape=True))
        )
    ).all()
    found: dict[uuid.UUID, Any] = {}
    for url, c in rows:
        parsed = vcs_provider.parse_repo_url(c, url or "")
        if parsed is not None and repo_scope(c, f"{parsed[0]}/{parsed[1]}") == scope:
            found[c.id] = c
    return [found[i] for i in sorted(found)]


async def _posting_connection(db, scope: str, conn: Any | None) -> Any | None:
    """The lowest-id active connection among all the repository's workspaces.

    Independent of the PR, so one connection posts every status of the repository.
    """
    conns = await _repository_connections(db, scope)
    active = [c for c in conns if c.status == "active"]
    if active:
        if len(conns) > 1:
            logger.info(
                "Workspaces on one repository use different connections",
                posting_connection=str(active[0].id),
                connections=[str(c.id) for c in conns],
            )
        return active[0]
    if conns:
        return None
    return conn if conn is not None and conn.status == "active" else None


def _comment_anchor(conn: Any, repo: str, pr_number: int, sessions: list[PRSession]) -> str:
    comment_id = next((s.status_comment_id for s in sessions if s.status_comment_id), None)
    if not comment_id:
        return ""
    server = (conn.server_url or "").rstrip("/")
    if conn.provider == "gitlab":
        return f"{server or 'https://gitlab.com'}/{repo}/-/merge_requests/{pr_number}#note_{comment_id}"
    base = server.removesuffix("/api/v3") if server else "https://github.com"
    return f"{base}/{repo}/pull/{pr_number}#issuecomment-{comment_id}"


def _target_url(v: Verdict, anchor: str) -> str:
    """A blocker's run page, else the comment, else the first run's page."""
    if v.state == "success":
        return anchor or v.target_url or ""
    return v.target_url or anchor


def _digest(v: Verdict, url: str) -> str:
    raw = f"{v.state}\x00{v.description}\x00{url}".encode()
    return f"{v.state}:{hashlib.sha256(raw).hexdigest()[:16]}"


async def _post(conn: Any, repo: str, sha: str, context: str, v: Verdict, url: str) -> None:
    owner, name = repo.rsplit("/", 1)
    if conn.provider == "gitlab":
        await gitlab_service.create_commit_status(
            conn,
            owner,
            name,
            sha,
            state=gitlab_state(v),
            description=v.description,
            target_url=url,
            context=context,
        )
    else:
        await github_service.create_commit_status(
            conn,
            owner,
            name,
            sha,
            state=github_state(v),
            description=v.description,
            target_url=url,
            context=context,
        )


def _is_rate_limited(resp: Any) -> bool:
    try:
        headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        if "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0":
            return True
        return "rate limit" in str(resp.text)[:4096].lower()
    except Exception:
        return False


def _now() -> float:
    return time.time()


def _rate_limit_delay(resp: Any, now: float | None = None) -> float:
    """How long a rate-limited response says to wait, clamped; a default when it does not say."""
    now = _now() if now is None else now
    try:
        headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
    except Exception:
        headers = {}
    for name, convert in (
        ("retry-after", float),
        ("x-ratelimit-reset", lambda v: float(v) - now),
    ):
        if name in headers:
            try:
                return max(1.0, min(convert(headers[name]), float(_BACKOFF_CAP)))
            except ValueError:
                continue
    return float(_BACKOFF_BASE * 2)


def _backoff_key(conn_id: Any) -> str:
    return f"tp:vcs_agg_backoff:{conn_id}"


async def _backoff_until(redis, conn_id: Any) -> float:
    try:
        return float(await redis.get(_backoff_key(conn_id)) or 0)
    except Exception:
        return 0.0


class Failed(NamedTuple):
    """A `failed:` marker: failures so far, retry not before, whether the provider shows green."""

    count: int
    until: float
    green: bool
    digest: str


def _failed_marker(count: int, until: float, digest: str, green: bool = False) -> str:
    return f"failed:{count}:{until}:{'g' if green else ''}:{digest}"


def _parse_failed(marker: str | None) -> Failed | None:
    if not marker or not marker.startswith("failed:"):
        return None
    try:
        count, until, flag, digest = marker[len("failed:") :].split(":", 3)
        return Failed(int(count), float(until), flag == "g", digest)
    except ValueError:
        return None


def _rejection_ttl(code: int, resp: Any) -> int | None:
    """How long to remember a refusal, or None when it may succeed on a retry."""
    if code >= 500 or code < 400 or code in _TRANSIENT_4XX:
        return None
    if code in (401, 403):
        return None if _is_rate_limited(resp) else _REJECTED_AUTH_TTL
    return _REJECTED_TTL


def _already_that_state(code: int, resp: Any) -> bool:
    """GitLab answers 400 when a status is re-posted with the state it already has."""
    return code == 400 and "cannot transition" in str(getattr(resp, "text", "")).lower()


async def _post_if_changed(
    redis,
    conn: Any,
    repo: str,
    scope: str,
    sha: str,
    context: str,
    v: Verdict,
    url: str,
    hold: Any = None,
) -> str | None:
    """Post one verdict unless it is what was last posted; returns its digest if posted.

    A failed or skipped post overwrites the marker with `failed:` (exponential backoff;
    every poll interval when it replaces a green the provider shows). A rate limit
    pauses the whole connection.
    """
    now = _now()
    digest = _digest(v, url)
    last_key = f"tp:vcs_agg_last:{scope}:{sha}:{context}"
    rejected_key = f"tp:vcs_agg_rejected:{scope}:{sha}:{context}"
    last = None
    try:
        last = await redis.get(last_key)
        if digest in (last, await redis.get(rejected_key)):
            return None
    except Exception as e:
        logger.warning("Could not read the last-posted marker", context=context, error=repr(e))
    failed = _parse_failed(last)
    same = failed is not None and failed.digest == digest
    prior = failed.count if same else 0
    if same and now < failed.until:
        return None
    shows_green = (last or "").startswith("success:") or bool(failed and failed.green)
    stale_green = shows_green and v.state != "success"
    paused_until = await _backoff_until(redis, conn.id)
    if paused_until > now:
        marker = _failed_marker(prior, paused_until, digest, shows_green)
        await _mark(redis, last_key, marker, _LAST_TTL)
        return None
    if hold is not None and not await hold():
        raise _LockLost()
    try:
        await _post(conn, repo, sha, context, v, url)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if conn.provider == "gitlab" and _already_that_state(code, e.response):
            # Treated as posted, so a running/pending status's text and link never update.
            logger.debug("GitLab already has this status state", context=context, repo=repo)
            await _mark(redis, last_key, digest, _LAST_TTL)
            return None
        remember = _rejection_ttl(code, e.response)
        limited = code == 429 or (code in (401, 403) and _is_rate_limited(e.response))
        delay = _rate_limit_delay(e.response, now) if limited else 0.0
        if limited:
            await _mark(redis, _backoff_key(conn.id), str(now + delay), math.ceil(delay))
        logger.error(
            "Aggregate commit status rejected"
            if remember
            else "Aggregate commit status not posted",
            context=context,
            repo=repo,
            sha=sha[:12],
            status_code=code,
        )
        await _fail(
            redis,
            last_key,
            prior + 1,
            now,
            max(delay, remember or 0),
            digest,
            shows_green,
            stale_green,
        )
        VCS_AGGREGATE_STATUS_POSTS.labels(
            provider=conn.provider, outcome="rejected" if remember else "transient"
        ).inc()
        if remember:
            VCS_AGGREGATE_STATUS_REJECTED.labels(
                provider=conn.provider, status_code=str(code)
            ).inc()
            await _mark(redis, rejected_key, digest, remember)
        return None
    except Exception as e:
        logger.error(
            "Aggregate commit status not posted (transport retries exhausted)",
            context=context,
            repo=repo,
            sha=sha[:12],
            error=repr(e),
        )
        await _fail(redis, last_key, prior + 1, now, 0.0, digest, shows_green, stale_green)
        VCS_AGGREGATE_STATUS_POSTS.labels(provider=conn.provider, outcome="transient").inc()
        return None
    await _mark(redis, last_key, digest, _LAST_TTL)
    VCS_AGGREGATE_STATUS_POSTS.labels(provider=conn.provider, outcome="ok").inc()
    return digest


async def _fail(
    redis,
    key: str,
    count: int,
    now: float,
    floor: float,
    digest: str,
    green: bool,
    stale_green: bool,
) -> None:
    """Record a failed post; `floor` is a rate-limit pause or a refusal's memory."""
    wait = (
        float(settings.vcs.poll_interval_seconds)
        if stale_green
        else min(_BACKOFF_BASE * 2 ** (count - 1), _BACKOFF_CAP)
    )
    await _mark(redis, key, _failed_marker(count, now + max(wait, floor), digest, green), _LAST_TTL)


async def _mark(redis, key: str, value: str, ttl: int) -> None:
    try:
        await redis.set(key, value, ex=ttl)
    except Exception as e:
        logger.warning("Could not record a posted status", key=key, error=repr(e))


async def _publish_locked(
    db,
    redis,
    conn: Any,
    scope: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    overlays: Overlays | None,
    enqueue_refresh: bool,
    hold: Any = None,
) -> Aggregate | None:
    agg, _ = await _evaluate(db, scope, pr_number, head_sha, overlays)
    sessions = await _sessions(db, scope, repo, pr_number)
    if sessions and not any(s.state in _LIVE_SESSION_STATES for s in sessions):
        return None
    post_conn = await _posting_connection(db, scope, conn)
    if post_conn is None:
        logger.warning("No active VCS connection to post the aggregate statuses", repo=repo)
        return None
    names = settings.vcs.commit_statuses
    anchor = _comment_anchor(post_conn, repo, pr_number, sessions)
    posted: list[str] = []
    for context, v in (
        (names.plan_name, agg.plan),
        (names.policy_name, agg.policy),
        (names.apply_name, agg.apply),
    ):
        if v is None:
            continue
        done = await _post_if_changed(
            redis, post_conn, repo, scope, head_sha, context, v, _target_url(v, anchor), hold
        )
        if done:
            posted.append(done)
    await _mark(redis, _checked_key(scope, head_sha), str(_now()), _LAST_TTL)
    if posted and enqueue_refresh:
        await _enqueue_refreshes(sessions, head_sha, posted)
    return agg


async def _enqueue_refreshes(sessions: list[PRSession], sha: str, posted: list[str]) -> None:
    digest = hashlib.sha256("|".join(posted).encode()).hexdigest()[:12]
    for sess in sessions:
        if sess.state not in _LIVE_SESSION_STATES:
            continue
        try:
            await enqueue_trigger(
                "vcs_status_comment_update",
                {"session_id": str(sess.id)},
                dedup_key=f"vcs_status:{sess.id}:agg:{sha}:{digest}",
            )
        except Exception as e:
            logger.warning("Could not enqueue the PR comment refresh", error=repr(e))


async def _mark_dirty(redis, key: str, overlay: Overlay | None) -> None:
    pipe = redis.pipeline(transaction=True)
    pipe.hset(key, "*", "1")
    if overlay is not None:
        pipe.hset(key, f"{overlay[0]}|{overlay[1]}", json.dumps(overlay[2]))
    pipe.expire(key, _DIRTY_TTL)
    await pipe.execute()


async def _take_dirty(redis, key: str) -> dict[str, str]:
    pipe = redis.pipeline(transaction=True)
    pipe.hgetall(key)
    pipe.delete(key)
    return (await pipe.execute())[0]


async def _restore_dirty(redis, key: str, taken: dict[str, str]) -> None:
    try:
        pipe = redis.pipeline(transaction=True)
        for field, value in taken.items():
            pipe.hset(key, field, value)
        pipe.expire(key, _DIRTY_TTL)
        await pipe.execute()
    except Exception as e:
        logger.warning("Could not put the waiting changes back", error=repr(e))


def _overlays_of(taken: dict[str, str]) -> dict[uuid.UUID, list[tuple[str, bool | None]]]:
    out: dict[uuid.UUID, list[tuple[str, bool | None]]] = {}
    for field, value in taken.items():
        run_id, _, status = field.partition("|")
        if not status:
            continue
        try:
            out.setdefault(uuid.UUID(run_id), []).append((status, json.loads(value)))
        except ValueError:
            continue
    return out


async def _invalidate_last(redis, scope: str, sha: str) -> None:
    names = settings.vcs.commit_statuses
    keys = [
        f"tp:vcs_agg_last:{scope}:{sha}:{c}"
        for c in (names.plan_name, names.policy_name, names.apply_name)
    ]
    try:
        await redis.delete(*keys)
    except Exception as e:
        logger.warning("Could not invalidate the last-posted markers", error=repr(e))


async def _abandon(redis, dirty_key: str, taken: dict[str, str], scope: str, sha: str) -> None:
    """An aborted round: keep the waiting change and make the pass re-post this head."""
    await _restore_dirty(redis, dirty_key, taken)
    await _invalidate_last(redis, scope, sha)


def _dirty_key(scope: str, sha: str) -> str:
    return f"tp:vcs_agg_dirty:{scope}:{sha}"


def _checked_key(scope: str, sha: str) -> str:
    return f"tp:vcs_agg_checked:{scope}:{sha}"


async def publish_scope(
    db,
    conn: Any,
    scope: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    overlay: Overlay | None = None,
    *,
    enqueue_refresh: bool = False,
) -> Aggregate | None:
    if not aggregate_statuses_enabled():
        return None
    with vcs_rate_limit.vcs_source("aggregate-statuses", repo=repo):
        return await _publish_scope(
            db, conn, scope, repo, pr_number, head_sha, overlay, enqueue_refresh
        )


async def _publish_scope(
    db,
    conn: Any,
    scope: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    overlay: Overlay | None,
    enqueue_refresh: bool,
) -> Aggregate | None:
    """Recompute and post, serialised per (repository, commit).

    Callers leave their change in a dirty hash and try the lock once; the holder takes
    the hash before reading and goes round again if more arrives. Returns None if nothing
    was computed (Redis down, lock lost, closed PR).
    """
    from terrapod.redis.client import get_redis_client

    lock_key = f"tp:vcs_agg_lock:{scope}:{head_sha}"
    dirty_key = _dirty_key(scope, head_sha)
    result: Aggregate | None = None
    try:
        redis = get_redis_client()
        await _mark_dirty(redis, dirty_key, overlay)
        for _ in range(_MAX_ROUNDS):
            token = uuid.uuid4().hex
            if not await redis.set(lock_key, token, nx=True, ex=_LOCK_TTL):
                return result

            async def hold(token=token) -> bool:
                return bool(await redis.eval(_EXTEND_LUA, 1, lock_key, token, _LOCK_TTL))

            taken: dict[str, str] = {}
            published = False
            try:
                taken = await _take_dirty(redis, dirty_key)
                if taken:
                    result = await _publish_locked(
                        db,
                        redis,
                        conn,
                        scope,
                        repo,
                        pr_number,
                        head_sha,
                        _overlays_of(taken),
                        enqueue_refresh,
                        hold,
                    )
                published = True
            except _LockLost:
                logger.warning(
                    "Lost the aggregate lock mid-publish", repo=repo, pr_number=pr_number
                )
                return None
            finally:
                try:
                    if not published:  # any abort, cancellation included
                        await _abandon(redis, dirty_key, taken, scope, head_sha)
                finally:
                    await _release_comment_lock(redis, lock_key, token)
            if not await redis.exists(dirty_key):
                return result
        logger.warning(
            "Aggregate statuses kept changing; re-posting on the next pass",
            repo=repo,
            pr_number=pr_number,
        )
        await _invalidate_last(redis, scope, head_sha)
    except Exception as e:
        logger.warning(
            "Aggregate statuses not posted", repo=repo, pr_number=pr_number, error=repr(e)
        )
    return result


async def publish(
    db,
    conn: Any,
    repo: str,
    pr_number: int,
    head_sha: str,
    overlay: Overlay | None = None,
    *,
    enqueue_refresh: bool = False,
) -> Aggregate | None:
    return await publish_scope(
        db,
        conn,
        repo_scope(conn, repo),
        repo,
        pr_number,
        head_sha,
        overlay,
        enqueue_refresh=enqueue_refresh,
    )


async def posting_notes(scope: str, head_sha: str, names: Any) -> dict[str, str]:
    """Per status name, why its Gates line may not be what the provider shows."""
    from terrapod.redis.client import get_redis_client

    out: dict[str, str] = {}
    try:
        redis = get_redis_client()
        for name in (names.plan_name, names.policy_name, names.apply_name):
            last = await redis.get(f"tp:vcs_agg_last:{scope}:{head_sha}:{name}")
            if _parse_failed(last) is None:
                continue
            refused = await redis.get(f"tp:vcs_agg_rejected:{scope}:{head_sha}:{name}")
            out[name] = "refused by the provider" if refused else "not posted yet, retrying"
    except Exception as e:
        logger.debug("Could not read the posting state", error=repr(e))
        return {}
    return out


async def _needs_publish(redis, scope: str, sha: str, stored: Stored) -> bool:
    """Republish on a changed record, a waiting change, or anything posted not yet green.

    A `failed:` marker waits out its backoff first. A head that is all green is
    recomputed now and then (no post unless something changed), which bounds a green
    left stale by a publish that never reached us.
    """
    if stored.changed:
        return True
    ev = stored.evaluation
    contexts = [settings.vcs.commit_statuses.plan_name, settings.vcs.commit_statuses.policy_name]
    if ev is not None and ev.apply_then_merge_candidate:
        contexts.append(settings.vcs.commit_statuses.apply_name)
    try:
        if await redis.exists(_dirty_key(scope, sha)):
            return True
        all_green = True
        for context in contexts:
            last = await redis.get(f"tp:vcs_agg_last:{scope}:{sha}:{context}")
            if last is None:
                return True
            if last.startswith("success:"):
                continue
            all_green = False
            failed = _parse_failed(last)
            if failed is None or _now() >= failed.until:
                return True
        if all_green:
            checked = await redis.get(_checked_key(scope, sha))
            return checked is None or _now() - float(checked) > _GREEN_RECHECK
    except Exception:
        return True
    return False


async def publish_pass(decisions: PRDecisions, stored: dict[tuple[str, int, str], Stored]) -> None:
    """Post for every PR head a poll pass recorded. Never raises."""
    if not stored or not aggregate_statuses_enabled():
        return
    from terrapod.redis.client import get_redis_client

    sem = asyncio.Semaphore(_MAX_PARALLEL_PUBLISHES)
    redis = get_redis_client()

    async def one(key: tuple[str, int, str], item: Stored) -> None:
        scope, pr_number, sha = key
        ev = item.evaluation
        if ev is None:
            return
        async with sem, get_db_session() as db:
            if not await _needs_publish(get_redis_client(), scope, sha, item):
                return
            conn = await _posting_connection(db, scope, None)
            if conn is None or await _backoff_until(redis, conn.id) > _now():
                return
            await publish_scope(
                db,
                conn,
                scope,
                decisions.slug_for(scope),
                pr_number,
                sha,
                enqueue_refresh=True,
            )

    with vcs_rate_limit.vcs_source("aggregate-statuses"):
        results = await asyncio.gather(
            *[one(k, v) for k, v in stored.items()], return_exceptions=True
        )
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Aggregate statuses not posted for a PR", error=repr(r))
