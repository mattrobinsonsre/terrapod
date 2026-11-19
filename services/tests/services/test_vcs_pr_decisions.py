"""The per-pass collector and the Redis merge that makes decisions sticky."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import vcs_pr_decisions as d
from terrapod.services.vcs_aggregate_status import aggregate
from terrapod.services.vcs_pr_decisions import (
    UNTRACKED_ID,
    PRDecisions,
    evaluation_from_fields,
    merge_and_store,
)

CONN = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="")
CONN2 = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="")
REPO = "org/repo"
SCOPE = d.repo_scope(CONN, REPO)
SHA = "a" * 40


class _HashRedis:
    """The hash commands the merge uses; a MULTI applies all-or-nothing."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False
        self.fail_exec_after: int | None = None
        self.transactions: list[bool] = []
        self.error_in_result = False
        self.fail_marker = False
        self.strings: dict[str, str] = {}

    def pipeline(self, transaction=True):
        self.transactions.append(transaction)
        return _Pipe(self, transaction)

    async def hgetall(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return dict(self.hashes.get(key, {}))

    async def set(self, key, value, *, ex=None):
        self.strings[key] = value

    async def exists(self, key):
        if self.fail_marker:
            raise ConnectionError("redis down")
        return int(key in self.strings)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            n += int(self.strings.pop(k, None) is not None) + int(
                self.hashes.pop(k, None) is not None
            )
        return n


class _Pipe:
    def __init__(self, r, transaction):
        self.r, self.transaction, self.ops = r, transaction, []

    def __getattr__(self, name):
        def q(*a, **kw):
            self.ops.append((name, a, kw))
            return self

        return q

    async def execute(self, raise_on_error=True):
        if self.r.fail:
            raise ConnectionError("redis down")
        n = self.r.fail_exec_after
        if n is not None:
            if self.transaction:
                raise ConnectionError("dropped before EXEC")  # nothing applied
            self.ops = self.ops[:n]  # a non-transactional pipeline applies a prefix
        for name, a, _kw in self.ops:
            h = self.r.hashes.setdefault(a[0], {})
            if name == "hset":
                h[a[1]] = a[2]
            elif name == "hsetnx":
                h.setdefault(a[1], a[2])
            elif name == "hdel":
                for f in a[1:]:
                    h.pop(f, None)
            elif name == "expire":
                self.r.ttls[a[0]] = a[1]
        if self.r.fail_exec_after is not None:
            raise ConnectionError("dropped mid-pipeline")
        results = [True] * len(self.ops)
        if self.r.error_in_result:
            results[-1] = RuntimeError("WRONGTYPE")
        return results


@pytest.fixture
def redis():
    r = _HashRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=r):
        yield r


def _ws(name, mode="merge_then_apply"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, vcs_workflow=mode)


def _pr(number=7, sha=SHA):
    return SimpleNamespace(number=number, head_sha=sha)


def _pass(*records, polled=(), completed=(), conn=CONN):
    """One pass: `records` are (ws, pr, outcome[, reason])."""
    dec = PRDecisions()
    for ws in polled:
        dec.polled(ws, conn, REPO)
    for rec in records:
        ws, pr, outcome, *reason = rec
        dec.polled(ws, conn, REPO)
        dec.record(ws, conn, REPO, pr, outcome, *reason)
    for ws in completed:
        dec.completed(ws.id, conn, REPO)
    return dec


async def _merge(dec, pr=None, scope=SCOPE):
    pr = pr or _pr()
    return await merge_and_store(scope, pr.number, pr.head_sha, dec.polled_for(scope), dec)


class TestMerge:
    async def test_every_workspace_decided_is_complete_and_lists_the_affected(self, redis):
        a, b = _ws("a"), _ws("b")
        dec = _pass((a, _pr(), "affected"), (b, _pr(), "not_affected"), completed=[a, b])
        ev = await _merge(dec)
        assert ev.complete and ev.affected == ("a",)
        assert not ev.not_evaluated and not ev.undecided and not ev.fork_skipped

    async def test_a_polled_workspace_with_no_decision_is_not_evaluated(self, redis):
        a, b = _ws("a"), _ws("b")
        dec = _pass((a, _pr(), "not_affected"), polled=[b], completed=[a])
        ev = await _merge(dec)
        assert ev.not_evaluated == ("b",) and not ev.complete

    async def test_a_workspace_that_finished_without_seeing_the_pr_is_not_a_candidate(self, redis):
        a, other_branch = _ws("a"), _ws("other")
        dec = _pass((a, _pr(), "affected"), polled=[other_branch], completed=[a, other_branch])
        ev = await _merge(dec)
        assert ev.complete and ev.not_evaluated == ()

    async def test_undecided_and_fork_are_reported(self, redis):
        a, b = _ws("a"), _ws("b")
        dec = _pass((a, _pr(), "undecided", "run not created"), (b, _pr(), "fork_skipped"))
        ev = await _merge(dec)
        assert ev.undecided == (("a", "run not created"),)
        assert ev.fork_skipped == ("b",) and not ev.complete

    async def test_a_decision_is_sticky_across_passes(self, redis):
        a, b = _ws("a"), _ws("b")
        await _merge(_pass((a, _pr(), "affected"), (b, _pr(), "not_affected"), completed=[a, b]))
        # Second pass: b's poll failed entirely, a's run could not be created.
        again = _pass((a, _pr(), "undecided", "archive"), polled=[b])
        ev = await _merge(again)
        assert ev.affected == ("a",)
        assert ev.undecided == () and ev.not_evaluated == ()
        assert ev.complete

    async def test_a_later_decision_replaces_an_undecided_one(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "undecided", "archive")))
        ev = await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        assert ev.affected == ("a",) and ev.complete

    async def test_a_new_head_sha_starts_fresh(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        new = _pr(sha="b" * 40)
        ev = await _merge(_pass((a, new, "undecided", "archive")), pr=new)
        assert ev.affected == () and ev.undecided == (("a", "archive"),)

    async def test_a_stale_not_evaluated_clears_once_the_workspace_finishes_elsewhere(self, redis):
        a, b = _ws("a"), _ws("b")
        await _merge(_pass((a, _pr(), "affected"), polled=[b], completed=[a]))
        ev = await _merge(_pass((a, _pr(), "affected"), polled=[b], completed=[a, b]))
        assert ev.not_evaluated == () and ev.complete

    async def test_two_passes_writing_different_workspaces_do_not_clobber_each_other(self, redis):
        a, b = _ws("a"), _ws("b")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        ev = await _merge(_pass((b, _pr(), "not_affected"), polled=[a], completed=[b]))
        assert ev.affected == ("a",) and ev.complete

    async def test_the_key_expires_and_the_head_is_recorded(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        key = d.evaluation_key(SCOPE, 7, SHA)
        assert key == f"tp:vcs_pr_eval:github::org/repo:7:{SHA}"
        assert redis.ttls[key] == 7 * 24 * 3600
        assert redis.ttls[d.head_key(SCOPE, 7)] == 7 * 24 * 3600
        assert await d.latest_head(SCOPE, 7) == SHA

    async def test_the_write_is_one_transaction(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        assert redis.transactions == [True]

    async def test_a_write_that_fails_part_way_leaves_nothing_readable(self, redis):
        a, b = _ws("a"), _ws("b")
        redis.fail_exec_after = 1
        dec = _pass((a, _pr(), "not_affected"), polled=[b], completed=[a])
        assert await _merge(dec) is None
        assert await d.load(SCOPE, 7, SHA) is None

    async def test_a_partial_write_would_have_read_complete_without_the_transaction(self, redis):
        # The hazard the transaction removes: the decided field lands, the blocker does not.
        a, b = _ws("a"), _ws("b")
        key = d.evaluation_key(SCOPE, 7, SHA)
        pipe = redis.pipeline(transaction=False)
        redis.fail_exec_after = 1
        pipe.hset(key, str(a.id), d._encode(d._Decision("not_affected", "", "a", "", "")))
        pipe.hsetnx(key, str(b.id), d._encode(d._Decision("not_evaluated", "", "b", "", "")))
        with pytest.raises(ConnectionError):
            await pipe.execute()
        ev = await d.load(SCOPE, 7, SHA)
        assert ev is not None and ev.complete  # what MULTI/EXEC prevents


class TestOneRecordPerRepository:
    def test_scope_ignores_url_casing_git_suffix_and_trailing_slash(self):
        base = d.repo_scope(CONN, "org/repo")
        assert d.repo_scope(CONN, "Org/Repo") == base
        assert d.repo_scope(CONN, "org/repo.git") == base
        assert d.repo_scope(CONN, "org/repo/") == base
        assert d.repo_scope(CONN2, "ORG/REPO") == base

    @pytest.mark.parametrize(
        "server",
        [
            "",
            "https://github.com",
            "https://github.com/",
            "https://api.github.com",
            "HTTPS://GitHub.com",
        ],
    )
    def test_github_dot_com_spellings_share_a_scope(self, server):
        conn = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url=server)
        assert d.repo_scope(conn, REPO) == SCOPE

    def test_enterprise_with_and_without_the_api_path_share_a_scope(self):
        def scope(server):
            conn = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url=server)
            return d.repo_scope(conn, REPO)

        assert scope("https://ghe.example/api/v3") == scope("https://ghe.example")
        assert scope("https://ghe.example/api/v3/") == scope("https://GHE.example/")
        assert scope("https://ghe.example") != SCOPE

    def test_gitlab_default_and_explicit_host_share_a_scope(self):
        def scope(server):
            conn = SimpleNamespace(id=uuid.uuid4(), provider="gitlab", server_url=server)
            return d.repo_scope(conn, REPO)

        assert scope("") == scope("https://gitlab.com") == scope("https://gitlab.com/")
        assert scope("https://gitlab.example") != scope("")

    def test_scope_separates_providers_and_servers(self):
        gl = SimpleNamespace(id=uuid.uuid4(), provider="gitlab", server_url="")
        ghe = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="https://ghe.example/")
        base = d.repo_scope(CONN, REPO)
        assert d.repo_scope(gl, REPO) != base and d.repo_scope(ghe, REPO) != base
        assert d.repo_scope(ghe, REPO) == d.repo_scope(
            SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="https://GHE.example"),
            REPO,
        )

    async def test_two_connections_and_casings_land_in_one_record(self, redis):
        a, b = _ws("a"), _ws("b")
        dec = PRDecisions()
        dec.polled(a, CONN, "org/repo")
        dec.polled(b, CONN2, "Org/Repo")
        dec.record(a, CONN, "org/repo", _pr(), "affected")
        dec.record(b, CONN2, "Org/Repo", _pr(), "not_affected")
        dec.completed(a.id, CONN, "org/repo")
        dec.completed(b.id, CONN2, "Org/Repo")
        assert len(dec.scopes()) == 1
        ev = await _merge(dec, scope=dec.scopes()[0])
        assert ev.complete and ev.affected == ("a",)
        fields = redis.hashes[d.evaluation_key(SCOPE, 7, SHA)]
        assert {json.loads(v)["c"] for v in fields.values()} == {str(CONN.id), str(CONN2.id)}

    async def test_one_connection_failing_keeps_the_shared_record_incomplete(self, redis):
        a, b = _ws("a"), _ws("b")
        dec = PRDecisions()
        dec.polled(a, CONN, "org/repo")
        dec.polled(b, CONN2, "ORG/repo")
        dec.record(a, CONN, "org/repo", _pr(), "not_affected")
        dec.completed(a.id, CONN, "org/repo")
        ev = await _merge(dec, scope=SCOPE)
        assert not ev.complete and ev.not_evaluated == ("b",)


class TestChangedFlag:
    async def test_the_changed_flag_reflects_the_hash(self, redis):
        a = _ws("a")
        first = await d._merge(
            SCOPE,
            7,
            SHA,
            _pass((a, _pr(), "affected"), completed=[a]).polled_for(SCOPE),
            _pass((a, _pr(), "affected"), completed=[a]),
        )
        assert first.changed
        dec = _pass((a, _pr(), "affected"), completed=[a])
        again = await d._merge(SCOPE, 7, SHA, dec.polled_for(SCOPE), dec)
        assert not again.changed
        dec = _pass((a, _pr(), "not_affected"), completed=[a])
        assert (await d._merge(SCOPE, 7, SHA, dec.polled_for(SCOPE), dec)).changed

    def test_the_collector_remembers_the_repository_as_spelled(self):
        dec = _pass((_ws("a"), _pr(), "affected"))
        assert dec.slug_for(SCOPE) == REPO


class TestLatestHead:
    async def test_an_older_pass_cannot_move_the_head_backwards(self, redis):
        a = _ws("a")
        newer = _pass((a, _pr(7, "b" * 40), "affected"))
        newer.started_at = 2000.0
        older = _pass((a, _pr(7, "c" * 40), "affected"))
        older.started_at = 1000.0
        await _merge(newer, pr=_pr(7, "b" * 40))
        await _merge(older, pr=_pr(7, "c" * 40))
        assert await d.latest_head(SCOPE, 7) == "b" * 40

    async def test_a_miss_or_error_is_none(self, redis):
        assert await d.latest_head(SCOPE, 7) is None
        redis.fail = True
        assert await d.latest_head(SCOPE, 7) is None


class TestLoad:
    async def test_a_miss_is_none(self, redis):
        assert await d.load(SCOPE, 7, SHA) is None

    async def test_a_redis_error_is_none(self, redis):
        redis.fail = True
        assert await d.load(SCOPE, 7, SHA) is None

    async def test_a_stored_record_round_trips(self, redis):
        a, b = _ws("a"), _ws("b")
        stored = await _merge(_pass((a, _pr(), "affected"), polled=[b], completed=[a]))
        assert await d.load(SCOPE, 7, SHA) == stored

    async def test_a_loaded_incomplete_record_never_reads_green(self, redis):
        b = _ws("b")
        await _merge(_pass(polled=[b]), pr=_pr())
        ev = await d.load(SCOPE, 7, SHA)
        assert ev is not None and not ev.complete
        assert aggregate([], ev).plan.state == "pending"


class TestEvaluationFromFields:
    def test_an_unreadable_field_is_undecided_not_ignored(self):
        ev = evaluation_from_fields({"ws-1": "not json"})
        assert not ev.complete and ev.undecided

    def test_an_unknown_outcome_is_undecided(self):
        raw = json.dumps({"o": "weird", "n": "a", "r": "", "m": ""})
        assert evaluation_from_fields({"x": raw}).undecided

    def test_the_collector_rejects_an_unknown_outcome(self):
        with pytest.raises(ValueError):
            PRDecisions().record(_ws("a"), CONN, REPO, _pr(), "bogus")


class TestStoreAll:
    async def test_merges_every_pr_head_seen(self, redis):
        a = _ws("a")
        dec = _pass((a, _pr(1, "1" * 40), "affected"), (a, _pr(2, "2" * 40), "not_affected"))
        dec.completed(a.id, CONN, REPO)
        out = await d.store_all(dec)
        assert len(out) == 2
        assert all(v.evaluation is not None and v.evaluation.complete for v in out.values())

    async def test_nothing_seen_touches_no_redis(self):
        assert await d.store_all(PRDecisions()) == {}


class TestNothingIsPrunedBecauseAPassDidNotSelectIt:
    async def _seeded(self, redis):
        a, b = _ws("a"), _ws("b")
        await _merge(_pass((a, _pr(), "affected"), (b, _pr(), "affected"), completed=[a, b]))
        return a, b

    async def _again(self, a, check):
        dec = _pass((a, _pr(), "affected"), completed=[a])  # this pass never saw b
        return await merge_and_store(SCOPE, 7, SHA, dec.polled_for(SCOPE), dec, member_check=check)

    async def test_a_workspace_the_database_still_has_in_scope_keeps_its_field(self, redis):
        a, _ = await self._seeded(redis)

        async def check(ids, scope):
            return set(ids)

        ev = await self._again(a, check)
        assert ev.affected == ("a", "b")

    async def test_without_a_check_nothing_is_pruned(self, redis):
        a, _ = await self._seeded(redis)
        assert (await self._again(a, None)).affected == ("a", "b")

    async def test_a_failing_check_prunes_nothing(self, redis):
        a, _ = await self._seeded(redis)

        async def check(ids, scope):
            raise RuntimeError("db down")

        assert (await self._again(a, check)).affected == ("a", "b")

    async def test_a_workspace_proven_gone_is_dropped(self, redis):
        a, b = await self._seeded(redis)
        asked = []

        async def check(ids, scope):
            asked.append((set(ids), scope))
            return set()

        ev = await self._again(a, check)
        assert ev.affected == ("a",)
        assert asked == [({b.id}, SCOPE)]

    async def test_a_polled_workspace_is_never_even_asked_about(self, redis):
        a, b = await self._seeded(redis)
        asked = []

        async def check(ids, scope):
            asked.append(set(ids))
            return set(ids)

        dec = _pass((a, _pr(), "affected"), polled=[b], completed=[a])
        await merge_and_store(SCOPE, 7, SHA, dec.polled_for(SCOPE), dec, member_check=check)
        assert asked == []


class TestAFailingCommandInsideTheTransaction:
    async def test_an_error_in_the_result_list_is_a_failure_and_clears_the_record(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        redis.error_in_result = True
        assert await _merge(_pass((a, _pr(), "affected"), completed=[a])) is None
        assert await d.load(SCOPE, 7, SHA) is None  # a half-applied record is not kept


class TestUnverifiedMarker:
    async def test_set_and_clear(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        assert await d.load(SCOPE, 7, SHA) is not None
        await d.mark_unverified(60)
        assert await d.load(SCOPE, 7, SHA) is None
        await d.clear_unverified()
        assert await d.load(SCOPE, 7, SHA) is not None

    async def test_a_redis_error_reading_the_marker_reads_as_unverified(self, redis):
        a = _ws("a")
        await _merge(_pass((a, _pr(), "affected"), completed=[a]))
        redis.fail_marker = True
        assert await d.load(SCOPE, 7, SHA) is None


class TestADisabledConnectionSaysSo:
    def _conn(self, status):
        return SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="", status=status)

    async def test_the_reason_reaches_the_description(self, redis):
        from terrapod.services.vcs_aggregate_status import aggregate

        off, a = self._conn("disabled"), _ws("a")
        b = _ws("b")
        dec = PRDecisions()
        dec.polled(a, CONN, REPO)
        dec.polled(b, off, REPO)
        dec.record(a, CONN, REPO, _pr(), "not_affected")
        dec.completed(a.id, CONN, REPO)
        ev = await _merge(dec, scope=dec.scopes()[0])
        assert not ev.complete
        assert ev.undecided == (("b", "connection disabled"),)
        assert ev.not_evaluated == ()
        assert "b (connection disabled)" in aggregate([], ev).plan.description

    async def test_an_active_connection_is_still_just_not_evaluated(self, redis):
        b = _ws("b")
        dec = PRDecisions()
        dec.polled(b, CONN, REPO)
        ev = await _merge(dec, scope=dec.scopes()[0], pr=_pr())
        assert ev.not_evaluated == ("b",) and ev.undecided == ()

    async def test_the_workspace_counts_are_exposed_for_the_cycle_gauge(self, redis):
        off = self._conn("disabled")
        dec = PRDecisions()
        w = _ws("w")
        dec.polled(w, off, REPO)
        dec.polled(_ws("x"), CONN, REPO)
        assert dec.disabled_candidates() == {w.id}

    async def test_re_enabling_replaces_the_blocker_with_a_decision(self, redis):
        off, b = self._conn("disabled"), _ws("b")
        dec = PRDecisions()
        dec.polled(b, off, REPO)
        await _merge(dec, scope=dec.scopes()[0], pr=_pr())
        on = _pass((b, _pr(), "not_affected"), completed=[b], conn=off)
        ev = await _merge(on, scope=on.scopes()[0])
        assert ev.complete and ev.undecided == ()


class TestOneNormalisationForSelectionAndScope:
    @pytest.mark.parametrize(
        "spelling",
        [
            "acme/mono",
            "Acme/Mono",
            "acme/mono.git",
            "acme/mono/",
            "acme/mono.git/",
            "  ACME/mono.GIT/ ",
        ],
    )
    def test_every_spelling_is_one_repository(self, spelling):
        assert d.normalise_repo(spelling) == "acme/mono"
        assert d.repo_scope(CONN, spelling) == d.repo_scope(CONN, "acme/mono")


class TestTheGaugesReadTheSharedState:
    """Per-replica gauges would go stale on every replica that did not set them."""

    async def test_unverified_follows_redis_not_this_process(self):
        class _R:
            def __init__(self):
                self.v = {}

            async def exists(self, key):
                return int(key in self.v)

            async def get(self, key):
                return self.v.get(key)

        redis = _R()
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch.object(d, "VCS_PR_RECORDS_UNVERIFIED") as unverified,
            patch.object(d, "VCS_DISABLED_CONNECTION_CANDIDATES") as disabled,
        ):
            redis.v[d.UNVERIFIED_KEY] = "1"
            redis.v[d.DISABLED_KEY] = "3"
            await d.sample_gauges()
            unverified.set.assert_called_with(1)
            disabled.set.assert_called_with(3)
            redis.v.clear()  # another replica's cycle cleared it
            await d.sample_gauges()
            unverified.set.assert_called_with(0)
            disabled.set.assert_called_with(0)

    async def test_a_redis_error_leaves_the_gauges_alone(self):
        with (
            patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")),
            patch.object(d, "VCS_PR_RECORDS_UNVERIFIED") as unverified,
        ):
            await d.sample_gauges()
        unverified.set.assert_not_called()

    async def test_the_cycle_publishes_the_disabled_count_to_redis(self):
        class _R:
            def __init__(self):
                self.v, self.ttl = {}, {}

            async def set(self, key, value, ex=None):
                self.v[key] = value
                self.ttl[key] = ex

        redis = _R()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await d.record_disabled_count(4)
        assert redis.v[d.DISABLED_KEY] == "4" and redis.ttl[d.DISABLED_KEY]


class TestTheSchedulerSamplerRefreshesThem:
    async def test_the_queue_depth_sampler_also_samples_the_vcs_gauges(self):
        import asyncio

        from terrapod.services import scheduler

        shutdown = asyncio.Event()
        calls = []

        async def sample():
            calls.append(1)
            shutdown.set()

        redis = AsyncMock()
        redis.llen = AsyncMock(return_value=0)
        with (
            patch.object(scheduler, "get_redis_client", return_value=redis),
            patch("terrapod.services.vcs_pr_decisions.sample_gauges", new=sample),
        ):
            await asyncio.wait_for(scheduler._run_queue_depth_sampler(shutdown, interval=1), 5)
        assert calls


async def _stored_fields(scope, pr):
    from terrapod.redis.client import get_redis_client

    return await get_redis_client().hgetall(d.evaluation_key(scope, pr.number, pr.head_sha))


class TestUntrackedBases:
    def _pr(self, number, base, sha=None):
        return SimpleNamespace(number=number, head_sha=sha or f"{number:040d}", base_ref=base)

    def _dec(self, *, branches, listing, polled_extra=()):
        dec = PRDecisions()
        for i, branch in enumerate(branches):
            ws = _ws(f"w{i}")
            dec.polled(ws, CONN, REPO)
            dec.branch(ws.id, CONN, REPO, branch)
            dec.completed(ws.id, CONN, REPO)  # its loop ran, it just never saw that PR
        for ws in polled_extra:
            dec.polled(ws, CONN, REPO)
        if listing is not None:
            dec.listing(CONN, REPO, listing)
        return dec

    async def test_a_pr_into_an_untracked_branch_is_recorded_as_touching_nothing(self, redis):
        dec = self._dec(branches=["main"], listing=[self._pr(1, "main"), self._pr(2, "release/1")])
        dec.add_listed()
        scope = dec.scopes()[0]
        assert dec.prs(scope) == [(1, f"{1:040d}"), (2, f"{2:040d}")]
        assert UNTRACKED_ID not in dec.records_for(scope, 1, f"{1:040d}")
        ev = await _merge(dec, pr=self._pr(2, "release/1"), scope=scope)
        assert ev.complete and ev.affected == () and ev.apply_then_merge_candidate is False

    async def test_a_tracked_branch_is_left_to_its_workspaces(self, redis):
        dec = self._dec(branches=["main", "release/1"], listing=[self._pr(2, "release/1")])
        dec.add_listed()
        scope = dec.scopes()[0]
        assert dec.records_for(scope, 2, f"{2:040d}") == {}

    async def test_no_complete_listing_records_nothing(self, redis):
        dec = self._dec(branches=["main"], listing=None)
        dec.add_listed()
        assert dec.prs(dec.scopes()[0]) == []

    async def test_a_candidate_with_an_unknown_branch_leaves_it_pending(self, redis):
        dec = self._dec(
            branches=["main"], listing=[self._pr(2, "release/1")], polled_extra=[_ws("x")]
        )
        dec.add_listed()
        scope = dec.scopes()[0]
        assert dec.records_for(scope, 2, f"{2:040d}") == {}
        ev = await _merge(dec, pr=self._pr(2, "release/1"), scope=scope)
        assert not ev.complete and ev.not_evaluated == ("x",)

    async def test_a_base_that_becomes_tracked_loses_the_marker(self, redis):
        pr = self._pr(2, "release/1")
        dec = self._dec(branches=["main"], listing=[pr])
        dec.add_listed()
        scope = dec.scopes()[0]
        assert (await _merge(dec, pr=pr, scope=scope)).complete

        tracker = _ws("y")
        later = self._dec(branches=["main"], listing=[pr], polled_extra=[tracker])
        later.branch(tracker.id, CONN, REPO, "release/1")
        later.add_listed()
        ev = await _merge(later, pr=pr, scope=scope)
        assert not ev.complete and ev.not_evaluated == ("y",)
        assert str(UNTRACKED_ID) not in await _stored_fields(scope, pr)

    async def test_the_sentinel_is_not_looked_up_as_a_workspace(self, redis):
        dec = self._dec(branches=["main"], listing=[self._pr(2, "release/1")])
        dec.add_listed()
        scope = dec.scopes()[0]
        pr = self._pr(2, "release/1")
        await _merge(dec, pr=pr, scope=scope)
        asked: list[set] = []

        async def nobody_exists(ids, scope):
            asked.append(set(ids))
            return set()

        again = self._dec(branches=["main"], listing=[pr])
        again.add_listed()
        ev = await merge_and_store(
            scope, 2, pr.head_sha, again.polled_for(scope), again, member_check=nobody_exists
        )
        assert ev is not None and ev.complete
        assert not any(UNTRACKED_ID in ids for ids in asked)

    async def test_a_record_with_no_field_is_never_complete(self, redis):
        assert not d.evaluation_from_fields({}).complete
        dec = self._dec(branches=["main", "release/1"], listing=[self._pr(2, "release/1")])
        dec.add_listed()
        scope = dec.scopes()[0]
        stored = await d._merge(scope, 2, f"{2:040d}", dec.polled_for(scope), dec)
        assert stored.evaluation is None

    async def test_store_all_covers_the_listed_prs(self, redis):
        dec = self._dec(branches=["main"], listing=[self._pr(2, "release/1")])
        out = await d.store_all(dec)
        assert len(out) == 1 and next(iter(out.values())).evaluation.complete
