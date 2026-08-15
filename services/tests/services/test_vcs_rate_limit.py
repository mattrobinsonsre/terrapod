"""Rate-limit observation and call attribution (#1334)."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import vcs_rate_limit


def _conn(**over):
    c = MagicMock()
    c.id = over.get("id", "conn-1")
    c.name = over.get("name", "gh-main")
    c.provider = over.get("provider", "github")
    return c


class _FakePipeline:
    """Queues commands and applies them on execute, like the real client.

    It exists because the tally is pipelined, and a fake without it makes the
    tally's own try/except swallow an AttributeError — leaving the suite green
    on code that never ran. That is not hypothetical: it is what this fake did
    before, and it hid the tally completely.
    """

    def __init__(self, redis, transaction):
        self.redis = redis
        self.transaction = transaction
        self.queued: list = []

    def incr(self, key):
        self.queued.append(("incr", key))
        return self

    def expire(self, key, ttl):
        self.queued.append(("expire", key, ttl))
        return self

    def hincrby(self, key, field, amount):
        self.queued.append(("hincrby", key, field, amount))
        return self

    def mget(self, keys):
        self.queued.append(("mget", keys))
        return self

    def hgetall(self, key):
        self.queued.append(("hgetall", key))
        return self

    async def execute(self):
        out = []
        for cmd in self.queued:
            if cmd[0] == "incr":
                self.redis.counters[cmd[1]] = self.redis.counters.get(cmd[1], 0) + 1
                out.append(self.redis.counters[cmd[1]])
            elif cmd[0] == "expire":
                self.redis.ttls[cmd[1]] = cmd[2]
                out.append(True)
            elif cmd[0] == "hincrby":
                h = self.redis.store.setdefault(cmd[1], {})
                h[cmd[2]] = str(int(h.get(cmd[2], 0)) + cmd[3])
                out.append(int(h[cmd[2]]))
            elif cmd[0] == "mget":
                out.append([self.redis.counters.get(k) for k in cmd[1]])
            elif cmd[0] == "hgetall":
                out.append(self.redis.store.get(cmd[1], {}))
        self.queued = []
        return out


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction=True):
        p = _FakePipeline(self, transaction)
        self.pipelines.append(p)
        return p

    async def hset(self, key, mapping=None):
        self.store.setdefault(key, {}).update({k: str(v) for k, v in (mapping or {}).items()})

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def hgetall(self, key):
        return self.store.get(key, {})


class TestHeaderParsing:
    def test_github_headers_are_read(self):
        reset = int(time.time()) + 1800
        snap = vcs_rate_limit.parse_headers(
            {
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4991",
                "X-RateLimit-Reset": str(reset),
                "X-RateLimit-Resource": "core",
            }
        )
        assert snap is not None
        assert (snap.limit, snap.remaining, snap.resource) == (5000, 4991, "core")
        assert 0 < snap.seconds_until_reset <= 1800

    def test_gitlab_unprefixed_headers_are_read(self):
        snap = vcs_rate_limit.parse_headers(
            {"RateLimit-Limit": "2000", "RateLimit-Remaining": "1999"}
        )
        assert snap is not None
        assert (snap.limit, snap.remaining) == (2000, 1999)

    def test_a_server_that_reports_nothing_yields_nothing(self):
        """The honesty requirement: absent must stay absent.

        A self-hosted GitLab may have rate limiting switched off entirely.
        Rendering that as a zero budget would show a permanent false alarm;
        rendering it as a full budget would hide a real one.
        """
        assert vcs_rate_limit.parse_headers({}) is None
        assert vcs_rate_limit.parse_headers({"Content-Type": "application/json"}) is None

    def test_an_exhausted_budget_is_recorded_not_discarded(self):
        """Zero remaining is the single most important reading to keep."""
        snap = vcs_rate_limit.parse_headers(
            {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0"}
        )
        assert snap is not None and snap.remaining == 0

    def test_garbage_headers_do_not_raise(self):
        assert (
            vcs_rate_limit.parse_headers(
                {"X-RateLimit-Limit": "lots", "X-RateLimit-Remaining": "some"}
            )
            is None
        )


class TestRecording:
    async def test_a_snapshot_round_trips_through_redis(self):
        redis = _FakeRedis()
        reset = int(time.time()) + 600
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await vcs_rate_limit.record(
                _conn(),
                {
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "42",
                    "X-RateLimit-Reset": str(reset),
                },
                outcome="200",
            )
            got = await vcs_rate_limit.get_snapshot("conn-1")

        assert got is not None
        assert got.remaining == 42
        assert redis.ttls, "the reading must expire rather than linger forever"

    async def test_recording_never_breaks_the_call_that_produced_it(self):
        """It sits in the path of every VCS operation, so it fails silent."""
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            await vcs_rate_limit.record(
                _conn(), {"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "0"}, outcome="200"
            )  # must not raise

    async def test_no_snapshot_when_nothing_recorded(self):
        with patch("terrapod.redis.client.get_redis_client", return_value=_FakeRedis()):
            assert await vcs_rate_limit.get_snapshot("never-seen") is None


class TestAttribution:
    def test_the_label_nests_and_restores(self):
        assert vcs_rate_limit.current_source() == vcs_rate_limit.SOURCE_UNKNOWN
        with vcs_rate_limit.vcs_source("workspace-poll"):
            assert vcs_rate_limit.current_source() == "workspace-poll"
            with vcs_rate_limit.vcs_source("autodiscovery"):
                assert vcs_rate_limit.current_source() == "autodiscovery"
            assert vcs_rate_limit.current_source() == "workspace-poll"
        assert vcs_rate_limit.current_source() == vcs_rate_limit.SOURCE_UNKNOWN

    async def test_the_decorator_labels_for_the_whole_call(self):
        seen = []

        @vcs_rate_limit.attributed("registry-tags")
        async def cycle():
            seen.append(vcs_rate_limit.current_source())

        await cycle()
        assert seen == ["registry-tags"]
        assert vcs_rate_limit.current_source() == vcs_rate_limit.SOURCE_UNKNOWN

    async def test_concurrent_subsystems_do_not_bleed_into_each_other(self):
        """Cycles run concurrently, and a contextvar that leaked across tasks
        would mis-attribute the spend — which is the whole point of the label."""
        seen: dict[str, str] = {}

        @vcs_rate_limit.attributed("workspace-poll")
        async def poll():
            await asyncio.sleep(0.01)
            seen["poll"] = vcs_rate_limit.current_source()

        @vcs_rate_limit.attributed("module-impact")
        async def impact():
            seen["impact"] = vcs_rate_limit.current_source()
            await asyncio.sleep(0.02)
            seen["impact_after"] = vcs_rate_limit.current_source()

        await asyncio.gather(poll(), impact())
        assert seen == {
            "poll": "workspace-poll",
            "impact": "module-impact",
            "impact_after": "module-impact",
        }


class TestEveryEntryPointIsAttributed:
    """`unknown` is a defect, not a category (#1334).

    An unattributed call path is invisible in exactly the situation the counter
    exists for, and it is the easy thing to forget when adding a subsystem —
    so assert the attribution at each entry point rather than trusting review.
    """

    # (module, function, args) — args because not every entry point is a
    # zero-arg cycle. `_poll_workspace_owned` takes a workspace id and a
    # semaphore, and calling it bare raised TypeError before the body ran,
    # which the old expected-exception tuple absorbed as a pass.
    ENTRY_POINTS = [
        ("terrapod.services.vcs_poller", "_poll_workspace_owned", "workspace"),
        ("terrapod.services.module_impact_service", "module_impact_poll_cycle", "none"),
        ("terrapod.services.registry_vcs_poller", "registry_vcs_poll_cycle", "none"),
        ("terrapod.services.policy_vcs_poller", "policy_vcs_poll_cycle", "none"),
        ("terrapod.services.drift_detection_service", "drift_check_cycle", "none"),
    ]

    @pytest.mark.parametrize("module_name,fn_name,arg_kind", ENTRY_POINTS)
    async def test_entry_point_sets_a_source(self, module_name, fn_name, arg_kind):
        import importlib
        import uuid as _uuid

        mod = importlib.import_module(module_name)
        fn = getattr(mod, fn_name)
        args: tuple = ()
        if arg_kind == "workspace":
            args = (_uuid.uuid4(), asyncio.Semaphore(1))

        captured: list[str] = []

        # A PLAIN def, deliberately. `side_effect=<async def>` makes the mock
        # *call* it, which returns a coroutine without executing the body — so
        # `captured` stayed empty, the resulting TypeError was in the expected
        # tuple below, and `if captured:` skipped the only assertion. All five
        # cases passed with every @attributed decorator deleted (#1345).
        def spy(*_a, **_k):
            captured.append(vcs_rate_limit.current_source())
            raise _Stop()

        # Drive the real decorated function far enough to observe the label,
        # then bail — we are asserting attribution, not the cycle's behaviour.
        with patch.object(mod, "get_db_session", side_effect=spy, create=True):
            with pytest.raises((_Stop, TypeError, AttributeError)):
                await fn(*args)

        # Asserted unconditionally: `if captured:` made an unfired spy a silent
        # pass, which is precisely the failure this guard exists to catch.
        assert captured, f"{module_name}.{fn_name} never reached the spy — the guard proved nothing"
        assert captured[0] != vcs_rate_limit.SOURCE_UNKNOWN, (
            f"{module_name}.{fn_name} makes provider calls without attributing them"
        )

    def test_the_autodiscovery_pass_is_labelled(self):
        """Its calls run under the poll cycle, so it needs its own label to be
        distinguishable from the workspace poll it shares a cycle with."""
        from pathlib import Path

        src = Path(
            "/app/terrapod/services/vcs_poller.py"
            if Path("/app/terrapod/services/vcs_poller.py").exists()
            else "services/terrapod/services/vcs_poller.py"
        ).read_text()
        assert src.count('vcs_source("autodiscovery")') >= 2, (
            "both the periodic and webhook-triggered autodiscovery passes must be labelled"
        )


class _Stop(Exception):
    pass


class TestConsumptionTally:
    """The tally is driven through `record`, its real caller.

    Deliberately not by calling `_tally` directly: the tally is best-effort and
    swallows its own failures, so a test that reaches past `record` would keep
    passing if the two ever stopped being connected.
    """

    async def _record_calls(self, redis, ctx_kwargs, n=1):
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with vcs_rate_limit.vcs_source("workspace-poll", **ctx_kwargs):
                for _ in range(n):
                    await vcs_rate_limit.record(_conn(), {}, outcome="200")

    async def test_calls_land_in_the_rate_window_and_against_a_consumer(self):
        redis = _FakeRedis()
        await self._record_calls(redis, {"repo": "org/infra", "kind": "workspace"}, n=3)

        assert redis.pipelines, "the tally never ran — the fake is swallowing it"
        assert sum(redis.counters.values()) == 3
        consumers = redis.store[vcs_rate_limit._consumer_key("conn-1")]
        assert consumers == {"workspace\x1forg/infra": "3"}

    async def test_labels_are_rolled_up_so_the_load_can_be_split(self):
        """The rollup is the actionable half: an operator over budget splits the
        connection, and labels are the only line an estate divides along."""
        redis = _FakeRedis()
        await self._record_calls(
            redis,
            {"repo": "org/a", "kind": "workspace", "labels": {"team": "platform", "env": "prod"}},
            n=2,
        )
        await self._record_calls(
            redis, {"repo": "org/b", "kind": "workspace", "labels": {"team": "data"}}, n=1
        )

        assert redis.store[vcs_rate_limit._label_key("conn-1")] == {
            "team=platform": "2",
            "env=prod": "2",
            "team=data": "1",
        }

    async def test_a_call_with_no_repo_still_names_something_actionable(self):
        """A token refresh has no repo; it must not vanish from the breakdown."""
        redis = _FakeRedis()
        await self._record_calls(redis, {})
        assert redis.store[vcs_rate_limit._consumer_key("conn-1")] == {
            "workspace-poll\x1f(workspace-poll)": "1"
        }

    async def test_the_tally_never_breaks_the_call_that_produced_it(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            await vcs_rate_limit.record(_conn(), {}, outcome="200")  # must not raise

    async def test_pathological_label_sets_are_bounded(self):
        """Labels are operator-supplied and become Redis fields, so one poll
        must not turn into a thousand writes."""
        redis = _FakeRedis()
        await self._record_calls(redis, {"repo": "r", "labels": {f"k{i}": "v" for i in range(200)}})
        assert len(redis.store[vcs_rate_limit._label_key("conn-1")]) <= 8

    async def test_the_pipeline_is_not_transactional(self):
        """The three keys have different prefixes, so they hash to different
        slots on a cluster-mode Redis, where a MULTI across slots is refused."""
        redis = _FakeRedis()
        await self._record_calls(redis, {"repo": "r"})
        assert all(p.transaction is False for p in redis.pipelines)


class TestBudgetWindow:
    """The window is measured, never assumed (#1345, fixed in #1346).

    GitHub refills per HOUR; GitLab.com per MINUTE. The UI renders the rate as
    a share of the allowance, so assuming an hour made a GitLab connection read
    ~60x busier than it was.

    A single response cannot answer this: headers say when the budget next
    refills, never how wide the window is. It is the gap between consecutive
    refills, so it is learned across observations.
    """

    def _snap(self, reset_at: int, window: int = 0):
        return vcs_rate_limit.RateLimitSnapshot(
            limit=5000,
            remaining=4000,
            reset_at=reset_at,
            observed_at=reset_at - 10,
            resource="core",
            window_seconds=window,
        )

    def test_a_single_response_does_not_claim_to_know_the_window(self):
        """The bug this replaces: rounding time-to-reset up to a bucket is
        sampling-dependent. Observed 612s from a GitHub reset it yields 900,
        not 3600, and every share computed from it is wrong by a factor of
        four. Better to report nothing than a number that moves with when you
        happened to look."""
        snap = vcs_rate_limit.parse_headers(
            {
                "X-RateLimit-Limit": "15000",
                "X-RateLimit-Remaining": "12234",
                "X-RateLimit-Reset": str(int(time.time()) + 612),
            }
        )
        assert snap is not None
        assert snap.window_seconds == 0

    def test_an_hourly_refill_is_learned_from_the_gap_between_resets(self):
        base = int(time.time()) + 600
        got = vcs_rate_limit._learn_window(self._snap(base), self._snap(base + 3600))
        assert got == 3600

    def test_a_per_minute_refill_is_learned_the_same_way(self):
        base = int(time.time()) + 30
        got = vcs_rate_limit._learn_window(self._snap(base), self._snap(base + 60))
        assert got == 60, "a per-minute throttle must not be reported as hourly"

    def test_nothing_is_claimed_until_a_refill_has_been_seen(self):
        base = int(time.time()) + 600
        assert vcs_rate_limit._learn_window(None, self._snap(base)) == 0
        # Same window observed twice: still no refill, so still nothing known.
        assert vcs_rate_limit._learn_window(self._snap(base), self._snap(base)) == 0

    def test_a_quiet_gap_cannot_inflate_a_known_window(self):
        """Nobody called for three hours, so the reset jumped three windows.
        A gap can only ever read LONG, never short, so the learned value takes
        the minimum and converges on the truth from above — without assuming
        which provider this is."""
        base = int(time.time()) + 600
        got = vcs_rate_limit._learn_window(self._snap(base, window=3600), self._snap(base + 10800))
        assert got == 3600

    def test_a_shorter_observation_corrects_a_longer_one(self):
        base = int(time.time()) + 600
        got = vcs_rate_limit._learn_window(self._snap(base, window=3600), self._snap(base + 60))
        assert got == 60

    def test_an_implausible_gap_is_rejected_rather_than_stored(self):
        """A clock jump or a provider changing its mind is not a window."""
        base = int(time.time()) + 600
        assert vcs_rate_limit._learn_window(self._snap(base, window=60), self._snap(base + 5)) == 60
        assert (
            vcs_rate_limit._learn_window(self._snap(base, window=60), self._snap(base + 999_999))
            == 60
        )

    async def test_the_window_survives_the_round_trip_through_redis(self):
        """The actual #1345 defect: computed, returned, consumed — never
        stored. Everything above passes on code that writes nothing."""
        redis = _FakeRedis()
        base = int(time.time())
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await vcs_rate_limit.record(
                _conn(),
                {
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "4999",
                    "X-RateLimit-Reset": str(base + 600),
                },
                outcome="200",
            )
            await vcs_rate_limit.record(
                _conn(),
                {
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "4998",
                    "X-RateLimit-Reset": str(base + 600 + 3600),
                },
                outcome="200",
            )
            got = await vcs_rate_limit.get_snapshot("conn-1")

        assert got is not None
        assert got.window_seconds == 3600

    async def test_every_field_survives_the_round_trip(self):
        """Guards the CLASS of bug, not just the one field: a value that
        reaches the dataclass, the parser, the reader, the SDK and the UI but
        is missing from the write mapping reads back as a default forever, and
        every test above it still passes."""
        redis = _FakeRedis()
        headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4321",
            "X-RateLimit-Reset": str(int(time.time()) + 900),
            "X-RateLimit-Resource": "graphql",
        }
        parsed = vcs_rate_limit.parse_headers(headers)
        assert parsed is not None
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await vcs_rate_limit.record(_conn(), headers, outcome="200")
            got = await vcs_rate_limit.get_snapshot("conn-1")

        assert got is not None
        for field in ("limit", "remaining", "reset_at", "resource"):
            assert getattr(got, field) == getattr(parsed, field), (
                f"{field} did not survive being written to Redis and read back"
            )

    def test_gitlab_names_the_throttle_rather_than_a_resource(self):
        """`RateLimit-Name` answers the same question as GitHub's
        `X-RateLimit-Resource` — which budget is this — so it lands in the same
        field instead of defaulting to GitHub's `core`."""
        snap = vcs_rate_limit.parse_headers(
            {
                "RateLimit-Limit": "2000",
                "RateLimit-Remaining": "1900",
                "RateLimit-Name": "throttle_authenticated_api",
            }
        )
        assert snap is not None
        assert snap.resource == "throttle_authenticated_api"


class TestVerdict:
    """The verdict compares projected spend against what is left.

    Not the level, and not the rate in isolation — that is the whole point. A
    budget that refills hourly reads healthy right after a reset however fast it
    is being spent, which is exactly how a connection burning twice its budget
    looked fine for part of every hour.
    """

    def test_a_hopeless_rate_is_called_out_even_on_a_full_budget(self):
        # 11,400/hr against 5,000 remaining and a full hour to go.
        verdict, eta = vcs_rate_limit._verdict(11_400, 5_000, 3_600)
        assert verdict == "will_exhaust"
        assert 1_500 < eta < 1_700  # ~26 minutes

    def test_a_high_rate_close_to_the_reset_is_fine(self):
        verdict, _ = vcs_rate_limit._verdict(4_000, 4_500, 60)
        assert verdict == "comfortable"

    def test_a_modest_rate_against_a_nearly_empty_budget_is_not(self):
        verdict, _ = vcs_rate_limit._verdict(500, 200, 3_600)
        assert verdict == "will_exhaust"

    def test_approaching_the_limit_reads_tight_before_it_reads_doomed(self):
        # Projected spend lands between 70% and 100% of what is left.
        verdict, _ = vcs_rate_limit._verdict(800, 1_000, 3_600)
        assert verdict == "tight"

    def test_exhausted_is_its_own_verdict(self):
        assert vcs_rate_limit._verdict(100, 0, 600) == ("exhausted", 0)

    def test_no_traffic_is_idle_not_healthy(self):
        assert vcs_rate_limit._verdict(0, 5_000, 3_600)[0] == "idle"

    def test_an_unreported_budget_does_not_invent_a_verdict(self):
        """No budget reported means NO verdict — busy or quiet (#1345).

        Both directions were fabrications: `idle` understated a connection
        being polled hard, and `comfortable` asserted headroom against a limit
        we cannot see. The rate is still reported and still answers "how hard
        are we hitting this"; only the classification is withheld.
        """
        assert vcs_rate_limit._verdict(500, None, 0)[0] is None
        assert vcs_rate_limit._verdict(0, None, 0)[0] is None
        # A reported budget still classifies normally.
        assert vcs_rate_limit._verdict(0, 5_000, 3_600)[0] == "idle"
        assert vcs_rate_limit._verdict(10, 0, 60)[0] == "exhausted"


class TestConsumptionReadback:
    async def test_rate_verdict_and_breakdown_round_trip(self):
        redis = _FakeRedis()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with vcs_rate_limit.vcs_source(
                "workspace-poll", repo="org/busy", kind="workspace", labels={"team": "platform"}
            ):
                for _ in range(40):
                    await vcs_rate_limit.record(_conn(), {}, outcome="200")
            with vcs_rate_limit.vcs_source(
                "registry-tags", consumer="default/vpc/aws", kind="module"
            ):
                await vcs_rate_limit.record(_conn(), {}, outcome="200")

            snap = vcs_rate_limit.RateLimitSnapshot(
                limit=5000,
                remaining=30,
                reset_at=int(time.time()) + 3600,
                observed_at=int(time.time()),
                resource="core",
                window_seconds=3600,  # GitHub's hourly quota
            )
            got = await vcs_rate_limit.get_consumption("conn-1", snap)

        assert got is not None
        assert got.calls_per_hour == 41
        assert got.verdict == "will_exhaust"
        assert got.top_consumers[0] == {"name": "org/busy", "kind": "workspace", "calls": 40}
        assert {"name": "default/vpc/aws", "kind": "module", "calls": 1} in got.top_consumers
        assert got.label_totals == [
            {"label": "team=platform", "key": "team", "value": "platform", "calls": 40}
        ]

    async def test_a_connection_nothing_is_known_about_reports_nothing(self):
        with patch("terrapod.redis.client.get_redis_client", return_value=_FakeRedis()):
            got = await vcs_rate_limit.get_consumption("never-seen", None)
        assert got is not None and got.calls_per_hour == 0
        assert got.verdict is None and got.top_consumers == []

    async def test_redis_being_down_reports_nothing_rather_than_zero(self):
        """Zero traffic and no information are different, and conflating them
        would show a healthy verdict for a connection we cannot see."""
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            assert await vcs_rate_limit.get_consumption("conn-1", None) is None


class TestConsumerAttribution:
    """Every subsystem must name what it is polling, not just itself.

    A per-subsystem total cannot answer the question an operator actually has,
    which is which repo, workspace or module to go and fix.
    """

    def test_the_policy_sync_handler_attributes_its_own_calls(self):
        """The fan-out cycle only enqueues triggers, so the decorator on it
        labels nothing — the provider calls happen in the handler, on a fresh
        task with a fresh context."""
        src = _source("policy_vcs_poller.py")
        handler = src[src.index("async def handle_policy_vcs_sync") :]
        handler = handler[: handler.index("\n@vcs_rate_limit")]
        assert "vcs_rate_limit.vcs_source(" in handler

    @pytest.mark.parametrize(
        "filename", ["module_impact_service.py", "registry_vcs_poller.py", "vcs_poller.py"]
    )
    def test_the_pollers_name_the_thing_they_are_polling(self, filename):
        assert "vcs_rate_limit.vcs_target(" in _source(filename)


def _source(filename: str) -> str:
    from pathlib import Path

    packaged = Path(f"/app/terrapod/services/{filename}")
    return (
        packaged if packaged.exists() else Path(f"services/terrapod/services/{filename}")
    ).read_text()


class TestChokepointRecords:
    async def test_every_github_response_is_recorded(self):
        """Recorded at the chokepoint, so it cannot be forgotten per call site."""
        from terrapod.services.github_service import _github_request

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4000"}

        client = MagicMock()
        client.request = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(vcs_rate_limit, "record", new=AsyncMock()) as rec:
                await _github_request("GET", "https://api.github.com/x", "tok", conn=_conn())

        rec.assert_awaited_once()
        assert rec.await_args.kwargs["outcome"] == "200"

    async def test_every_gitlab_response_is_recorded_too(self):
        """GitLab was uninstrumented entirely (#1345).

        `record()` had exactly one call site — in github_service — so a GitLab
        connection reported a rate of zero and a fabricated `idle` verdict while
        the poller hammered it. The docs and the runbook both promised the
        reading came from the same headers on both providers.
        """
        from terrapod.services.gitlab_service import _gitlab_request

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"RateLimit-Limit": "2000", "RateLimit-Remaining": "1900"}

        client = MagicMock()
        client.request = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(vcs_rate_limit, "record", new=AsyncMock()) as rec:
                await _gitlab_request("GET", "https://gitlab.com/api/v4/x", _conn())

        rec.assert_awaited_once()
        assert rec.await_args.kwargs["outcome"] == "200"

    async def test_no_provider_call_escapes_the_count(self):
        """The chokepoint only counts what actually routes through it.

        gitlab_service once had 19 raw `httpx.AsyncClient()` call sites against
        ONE via the helper, so instrumenting the helper alone measured almost
        nothing — the panel read "comfortable" while the poll cycle was the
        thing spending the budget. Rather than pin a list of today's hot paths
        (which says nothing about the next function someone adds), this asserts
        the invariant directly: **a function that makes a provider HTTP call
        must also count it**, either by routing through the retrying helper or,
        where it cannot (a streaming download, or a write that must never be
        replayed), by calling `record` itself.
        """
        import ast
        import inspect

        from terrapod.services import github_service, gitlab_service

        # Any of these means the function talks to the provider over HTTP.
        makes_a_call = (
            "client.get(",
            "client.post(",
            "client.put(",
            "client.request(",
            "client.stream(",
        )

        offenders: list[str] = []
        inspected = 0
        for module, helper in (
            (gitlab_service, "_gitlab_request("),
            (github_service, "_github_request("),
        ):
            module_src = inspect.getsource(module)
            for node in ast.parse(module_src).body:
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                src = ast.get_source_segment(module_src, node) or ""
                if not (any(m in src for m in makes_a_call) or helper in src):
                    continue
                inspected += 1
                if helper in src or "vcs_rate_limit.record" in src:
                    continue
                offenders.append(f"{module.__name__.rsplit('.', 1)[-1]}.{node.name}")

        # Bite-check: a detector that matches nothing passes vacuously, which is
        # exactly how this gate would rot into decoration.
        assert inspected >= 35, (
            f"the detector only found {inspected} provider calls across both modules — "
            "it has stopped recognising them, so the gate is no longer guarding anything"
        )

        assert not offenders, (
            "these provider calls are invisible to the saturation panel — route them "
            "through the counted helper, or call vcs_rate_limit.record directly if the "
            f"call streams or must not be retried: {offenders}"
        )

    async def test_a_rate_limited_response_is_recorded_too(self):
        """The 403 that means 'exhausted' carries the most valuable reading of
        all, so it must not be skipped in favour of only successful calls."""
        from terrapod.services.github_service import _github_request

        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000"}
        resp.content = b"{}"

        client = MagicMock()
        client.request = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch("asyncio.sleep", new=AsyncMock()):
                with patch.object(vcs_rate_limit, "record", new=AsyncMock()) as rec:
                    await _github_request("GET", "https://api.github.com/x", "tok", conn=_conn())

        assert rec.await_count >= 1
        assert rec.await_args.kwargs["outcome"] == "403"
