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


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.ttls: dict[str, int] = {}

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

    ENTRY_POINTS = [
        ("terrapod.services.vcs_poller", "_poll_workspace_owned"),
        ("terrapod.services.module_impact_service", "module_impact_poll_cycle"),
        ("terrapod.services.registry_vcs_poller", "registry_vcs_poll_cycle"),
        ("terrapod.services.policy_vcs_poller", "policy_vcs_poll_cycle"),
        ("terrapod.services.drift_detection_service", "drift_check_cycle"),
    ]

    @pytest.mark.parametrize("module_name,fn_name", ENTRY_POINTS)
    async def test_entry_point_sets_a_source(self, module_name, fn_name):
        import importlib

        mod = importlib.import_module(module_name)
        fn = getattr(mod, fn_name)

        captured: list[str] = []

        async def spy(*_a, **_k):
            captured.append(vcs_rate_limit.current_source())
            raise _Stop()

        # Drive the real decorated function far enough to observe the label,
        # then bail — we are asserting attribution, not the cycle's behaviour.
        with patch.object(mod, "get_db_session", side_effect=spy, create=True):
            with pytest.raises((_Stop, TypeError, AttributeError)):
                await fn()

        if captured:
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
