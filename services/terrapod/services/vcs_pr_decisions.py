"""Which workspaces each pull-request head affects, recorded by the poller.

One Redis hash per (repository, PR, head sha), one field per workspace, written in a single
MULTI/EXEC. A decision is sticky against a later failure; a new sha is a new key.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from terrapod.api.metrics import VCS_DISABLED_CONNECTION_CANDIDATES, VCS_PR_RECORDS_UNVERIFIED
from terrapod.logging_config import get_logger
from terrapod.services.vcs_aggregate_status import PREvaluation

logger = get_logger(__name__)

AFFECTED = "affected"
NOT_AFFECTED = "not_affected"
FORK_SKIPPED = "fork_skipped"
UNDECIDED = "undecided"
NOT_EVALUATED = "not_evaluated"
DISABLED_REASON = "connection disabled"
#: Stands in for "no workspace tracks this PR's base branch" in a PR's record.
UNTRACKED_ID = uuid.UUID(int=0)
UNTRACKED_NAME = "(no workspace tracks the base branch)"

_DECIDED = frozenset({AFFECTED, NOT_AFFECTED, FORK_SKIPPED})
_RECORDABLE = _DECIDED | {UNDECIDED}

EVALUATION_TTL_SECONDS = 7 * 24 * 3600
_APPLY_THEN_MERGE = "apply_then_merge"
_MAX_PARALLEL_MERGES = 8


_DEFAULT_HOSTS = {
    "github": frozenset({"https://github.com", "https://api.github.com"}),
    "gitlab": frozenset({"https://gitlab.com"}),
}
UNVERIFIED_KEY = "tp:vcs_pr_eval_unverified"
DISABLED_KEY = "tp:vcs_disabled_candidates"

#: Says which of a record's workspaces still belong to a scope, from the database.
MemberCheck = Callable[[set[uuid.UUID], str], Awaitable[set[uuid.UUID]]]


def _server(conn: Any) -> str:
    server = (getattr(conn, "server_url", "") or "").strip().lower().rstrip("/")
    server = server.removesuffix("/api/v3").removesuffix("/api/v4").rstrip("/")
    return "" if server in _DEFAULT_HOSTS.get(conn.provider, frozenset()) else server


def normalise_repo(repo: str) -> str:
    """`owner/repo` as one spelling: case, trailing slash and `.git` folded away."""
    path = repo.strip().lower().removesuffix("/").removesuffix(".git").removesuffix("/")
    return path


def repo_scope(conn: Any, repo: str) -> str:
    """The repository a record belongs to, independent of URL casing and connection.

    Provider and host are part of it (two hosts can share an `owner/repo`) with
    the provider's default host and an API path spelled out or not all equal; the
    connection is not, so two connections to one repository share a record.
    """
    return f"{conn.provider}:{_server(conn)}:{normalise_repo(repo)}"


def evaluation_key(scope: str, pr_number: int, head_sha: str) -> str:
    return f"tp:vcs_pr_eval:{scope}:{pr_number}:{head_sha}"


def head_key(scope: str, pr_number: int) -> str:
    return f"tp:vcs_pr_head:{scope}:{pr_number}"


class Stored(NamedTuple):
    evaluation: PREvaluation | None
    changed: bool


@dataclass(frozen=True)
class _Decision:
    outcome: str
    reason: str
    name: str
    mode: str
    conn_id: str = ""


def _encode(d: _Decision) -> str:
    return json.dumps({"o": d.outcome, "r": d.reason, "n": d.name, "m": d.mode, "c": d.conn_id})


def _decode(ws_id: str, raw: str) -> _Decision:
    try:
        data = json.loads(raw)
        outcome = data["o"]
        if outcome not in _DECIDED | {UNDECIDED, NOT_EVALUATED}:
            raise ValueError(outcome)
        return _Decision(
            outcome,
            str(data.get("r", "")),
            str(data["n"]),
            str(data.get("m", "")),
            str(data.get("c", "")),
        )
    except Exception:
        return _Decision(UNDECIDED, "unreadable record", ws_id, "")


class PRDecisions:
    """The decisions one poll pass reached. In-process, one instance per pass."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._polled: dict[str, dict[uuid.UUID, tuple[str, str, str]]] = {}
        self._completed: dict[str, set[uuid.UUID]] = {}
        self._records: dict[str, dict[tuple[int, str], dict[uuid.UUID, _Decision]]] = {}
        self._slugs: dict[str, str] = {}
        self._reasons: dict[tuple[str, uuid.UUID], str] = {}
        self._branches: dict[str, dict[uuid.UUID, str]] = {}
        self._listings: dict[str, list[Any]] = {}
        self._disabled: set[uuid.UUID] = set()

    def polled(self, ws: Any, conn: Any, repo: str) -> None:
        """Register a workspace as a candidate for this repository's PRs."""
        mode = getattr(ws, "vcs_workflow", "") or ""
        entry = (ws.name, mode, str(conn.id))
        scope = repo_scope(conn, repo)
        self._slugs.setdefault(scope, repo)
        self._polled.setdefault(scope, {})[ws.id] = entry
        if getattr(conn, "status", "active") != "active":
            self._reasons[(scope, ws.id)] = DISABLED_REASON
            self._disabled.add(ws.id)

    def record(
        self, ws: Any, conn: Any, repo: str, pr: Any, outcome: str, reason: str = ""
    ) -> None:
        if outcome not in _RECORDABLE:
            raise ValueError(f"unknown outcome {outcome!r}")
        mode = getattr(ws, "vcs_workflow", "") or ""
        scope = repo_scope(conn, repo)
        self._slugs.setdefault(scope, repo)
        by_pr = self._records.setdefault(scope, {})
        by_pr.setdefault((pr.number, pr.head_sha), {})[ws.id] = _Decision(
            outcome, reason, ws.name, mode, str(conn.id)
        )

    def branch(self, ws_id: uuid.UUID, conn: Any, repo: str, branch: str) -> None:
        """The branch a workspace tracks, once resolved."""
        self._branches.setdefault(repo_scope(conn, repo), {})[ws_id] = branch

    def listing(self, conn: Any, repo: str, prs: list[Any]) -> None:
        """A COMPLETE listing of the repository's open PRs (the first one seen wins)."""
        self._listings.setdefault(repo_scope(conn, repo), list(prs))

    def add_listed(self) -> None:
        """Make every listed PR head part of this pass, so a stale record cannot outlive it.

        One whose base no candidate tracks is recorded as touching nothing, but only when
        every candidate's branch is known: a workspace whose branch could not be resolved
        might track it, and then the head is left to read pending.
        """
        for scope, prs in self._listings.items():
            candidates = self._polled.get(scope, {})
            branches = self._branches.get(scope, {})
            if not candidates:
                continue
            all_known = all(ws_id in branches for ws_id in candidates)
            tracked = set(branches.values())
            conn_id = min(c for _, _, c in candidates.values())
            by_pr = self._records.setdefault(scope, {})
            for pr in prs:
                fields = by_pr.setdefault((pr.number, pr.head_sha), {})
                if all_known and pr.base_ref not in tracked:
                    fields[UNTRACKED_ID] = _Decision(NOT_AFFECTED, "", UNTRACKED_NAME, "", conn_id)

    def completed(self, ws_id: uuid.UUID, conn: Any, repo: str) -> None:
        """A workspace's PR loop ran to the end."""
        self._completed.setdefault(repo_scope(conn, repo), set()).add(ws_id)

    def reason_for(self, scope: str, ws_id: uuid.UUID) -> str:
        """Why a candidate cannot be evaluated this pass, when that is known up front."""
        return self._reasons.get((scope, ws_id), "")

    def disabled_candidates(self) -> set[uuid.UUID]:
        return set(self._disabled)

    def slug_for(self, scope: str) -> str:
        """`owner/repo` as a workspace spelled it, for provider calls."""
        return self._slugs[scope]

    def scopes(self) -> list[str]:
        return sorted(set(self._records) | set(self._polled))

    def prs(self, scope: str) -> list[tuple[int, str]]:
        return sorted(self._records.get(scope, {}))

    def polled_for(self, scope: str) -> dict[uuid.UUID, tuple[str, str, str]]:
        return dict(self._polled.get(scope, {}))

    def records_for(self, scope: str, pr_number: int, head_sha: str) -> dict[uuid.UUID, _Decision]:
        return dict(self._records.get(scope, {}).get((pr_number, head_sha), {}))

    def completed_for(self, scope: str) -> set[uuid.UUID]:
        return set(self._completed.get(scope, set()))


def evaluation_from_fields(fields: dict[str, str]) -> PREvaluation:
    """The `PREvaluation` a stored hash describes. Pure."""
    decisions = [_decode(ws_id, raw) for ws_id, raw in fields.items()]
    by_name = sorted(decisions, key=lambda d: d.name)
    affected = tuple(d.name for d in by_name if d.outcome == AFFECTED)
    forks = tuple(d.name for d in by_name if d.outcome == FORK_SKIPPED)
    undecided = tuple((d.name, d.reason) for d in by_name if d.outcome == UNDECIDED)
    missing = tuple(d.name for d in by_name if d.outcome == NOT_EVALUATED and not d.reason)
    undecided = tuple(
        sorted(
            [
                *undecided,
                *((d.name, d.reason) for d in by_name if d.outcome == NOT_EVALUATED and d.reason),
            ]
        )
    )
    return PREvaluation(
        complete=bool(by_name) and not undecided and not missing,
        affected=affected,
        not_evaluated=missing,
        undecided=undecided,
        fork_skipped=forks,
        apply_then_merge_candidate=any(d.mode == _APPLY_THEN_MERGE for d in by_name),
    )


async def merge_and_store(
    scope: str,
    pr_number: int,
    head_sha: str,
    polled: dict[uuid.UUID, tuple[str, str, str]],
    decisions: PRDecisions,
    member_check: MemberCheck | None = None,
) -> PREvaluation | None:
    stored = await _merge(scope, pr_number, head_sha, polled, decisions, member_check)
    return stored.evaluation


async def _merge(
    scope: str,
    pr_number: int,
    head_sha: str,
    polled: dict[uuid.UUID, tuple[str, str, str]],
    decisions: PRDecisions,
    member_check: MemberCheck | None = None,
) -> Stored:
    """Fold this pass's decisions for one PR head into Redis and read the result back.

    A candidate that neither decided nor finished its loop is `not_evaluated`. Fields for
    workspaces not polled are dropped only when `member_check` proves, from the database,
    that they left the scope. Returns None if Redis fails.
    """
    from terrapod.redis.client import get_redis_client

    key = evaluation_key(scope, pr_number, head_sha)
    mine = decisions.records_for(scope, pr_number, head_sha)
    finished = decisions.completed_for(scope)
    try:
        redis = get_redis_client()
        before = await redis.hgetall(key)
        drop = [
            f
            for f, raw in before.items()
            if uuid.UUID(f) in finished
            and uuid.UUID(f) not in mine
            and _decode(f, raw).outcome == NOT_EVALUATED
        ]
        drop += await _proven_gone(scope, before, polled, member_check)
        if UNTRACKED_ID not in mine and str(UNTRACKED_ID) in before:
            drop.append(str(UNTRACKED_ID))
        pipe = redis.pipeline(transaction=True)
        if drop:
            pipe.hdel(key, *drop)
        for ws_id, d in mine.items():
            if d.outcome in _DECIDED:
                pipe.hset(key, str(ws_id), _encode(d))
            else:
                pipe.hsetnx(key, str(ws_id), _encode(d))
        for ws_id, (name, mode, conn_id) in polled.items():
            if ws_id not in mine and ws_id not in finished:
                reason = decisions.reason_for(scope, ws_id)
                missing = _Decision(NOT_EVALUATED, reason, name, mode, conn_id)
                pipe.hsetnx(key, str(ws_id), _encode(missing))
        pipe.expire(key, EVALUATION_TTL_SECONDS)
        hk = head_key(scope, pr_number)
        pipe.hset(hk, head_sha, str(decisions.started_at))
        pipe.expire(hk, EVALUATION_TTL_SECONDS)
        results = await pipe.execute(raise_on_error=False)
        if any(isinstance(r, Exception) for r in results):
            await redis.delete(key)  # MULTI/EXEC does not roll back: keep no half record
            raise RuntimeError("a command in the transaction failed")
        fields = await redis.hgetall(key)
    except Exception as e:
        logger.warning(
            "Could not store the PR evaluation; statuses stay pending",
            scope=scope,
            pr_number=pr_number,
            error=repr(e),
        )
        return Stored(None, False)
    return Stored(evaluation_from_fields(fields) if fields else None, fields != before)


async def _proven_gone(
    scope: str,
    before: dict[str, str],
    polled: dict[uuid.UUID, tuple[str, str, str]],
    member_check: MemberCheck | None,
) -> list[str]:
    unseen: set[uuid.UUID] = set()
    for field in before:
        try:
            ws_id = uuid.UUID(field)
        except ValueError:
            continue
        if ws_id not in polled and ws_id != UNTRACKED_ID:
            unseen.add(ws_id)
    if not unseen or member_check is None:
        return []
    try:
        still = await member_check(unseen, scope)
    except Exception as e:
        logger.warning("Could not check record members; none pruned", error=repr(e))
        return []
    return [str(w) for w in unseen - still]


async def mark_unverified(ttl: int | None = None) -> None:
    """Candidate registration failed: records may miss workspaces until a cycle succeeds."""
    from terrapod.config import settings
    from terrapod.redis.client import get_redis_client

    ttl = ttl or max(3 * settings.vcs.poll_interval_seconds, 300)
    VCS_PR_RECORDS_UNVERIFIED.set(1)
    try:
        await get_redis_client().set(UNVERIFIED_KEY, "1", ex=ttl)
    except Exception as e:
        logger.warning("Could not mark the PR records unverified", error=repr(e))


async def clear_unverified() -> None:
    from terrapod.redis.client import get_redis_client

    VCS_PR_RECORDS_UNVERIFIED.set(0)
    try:
        await get_redis_client().delete(UNVERIFIED_KEY)
    except Exception as e:
        logger.warning("Could not clear the unverified marker", error=repr(e))


async def record_disabled_count(count: int) -> None:
    """Share the periodic cycle's count of workspaces on disabled connections via Redis."""
    from terrapod.config import settings
    from terrapod.redis.client import get_redis_client

    VCS_DISABLED_CONNECTION_CANDIDATES.set(count)
    try:
        ttl = max(3 * settings.vcs.poll_interval_seconds, 300)
        await get_redis_client().set(DISABLED_KEY, str(count), ex=ttl)
    except Exception as e:
        logger.warning("Could not record the disabled-connection count", error=repr(e))


async def sample_gauges() -> None:
    """Set both VCS gauges from the shared Redis state, so every replica reports it."""
    from terrapod.redis.client import get_redis_client

    try:
        redis = get_redis_client()
        VCS_PR_RECORDS_UNVERIFIED.set(1 if await redis.exists(UNVERIFIED_KEY) else 0)
        VCS_DISABLED_CONNECTION_CANDIDATES.set(int(await redis.get(DISABLED_KEY) or 0))
    except Exception as e:
        logger.debug("Could not sample the VCS gauges", error=repr(e))


async def load(scope: str, pr_number: int, head_sha: str) -> PREvaluation | None:
    """The stored evaluation, or None on a miss, an unverified marker or a Redis error."""
    from terrapod.redis.client import get_redis_client

    try:
        if await get_redis_client().exists(UNVERIFIED_KEY):
            return None
        fields = await get_redis_client().hgetall(evaluation_key(scope, pr_number, head_sha))
    except Exception as e:
        logger.warning("Could not load the PR evaluation", scope=scope, error=repr(e))
        return None
    return evaluation_from_fields(fields) if fields else None


async def latest_head(scope: str, pr_number: int) -> str | None:
    """The head sha most recently observed by a pass, or None.

    The newest observation, not proof of the provider's current head.
    """
    from terrapod.redis.client import get_redis_client

    try:
        seen = await get_redis_client().hgetall(head_key(scope, pr_number))
        return max(seen, key=lambda sha: float(seen[sha])) if seen else None
    except Exception as e:
        logger.warning("Could not load the PR head", scope=scope, error=repr(e))
        return None


async def store_all(
    decisions: PRDecisions, member_check: MemberCheck | None = None
) -> dict[tuple[str, int, str], Stored]:
    """Merge every PR head this pass saw. Never raises: a pass must not fail on this."""
    decisions.add_listed()
    sem = asyncio.Semaphore(_MAX_PARALLEL_MERGES)
    keys: list[tuple[str, int, str]] = []
    for scope in decisions.scopes():
        keys.extend((scope, n, sha) for n, sha in decisions.prs(scope))

    async def one(k: tuple[str, int, str]) -> Stored:
        scope, number, sha = k
        async with sem:
            polled = decisions.polled_for(scope)
            return await _merge(scope, number, sha, polled, decisions, member_check)

    results = await asyncio.gather(*[one(k) for k in keys], return_exceptions=True)
    return {
        k: (r if isinstance(r, Stored) else Stored(None, False))
        for k, r in zip(keys, results, strict=True)
    }
