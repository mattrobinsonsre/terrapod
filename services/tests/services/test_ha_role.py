"""Tests for leader/follower role resolution (#960 phase 1, #1101).

The precondition this feeds will always evaluate `leader` under the shipped
default, so nothing in normal operation ever reaches the follower branch —
these tests are the only thing that does, and a false negative there takes
down writes on a healthy single node.
"""

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import ha_role

pytestmark = pytest.mark.asyncio


def _cfg(role="leader", node_name="", internal="", external="", threshold=3):
    """A settings stand-in shaped like the real HAConfig."""
    from types import SimpleNamespace

    probe = SimpleNamespace(internal=internal, external=external)
    return SimpleNamespace(
        role=role,
        node_name=node_name,
        probe_url=probe,
        effective_probe_url=internal or external,
        probe_interval_seconds=60,
        probe_threshold=threshold,
    )


class TestStaticRoleNeedsNoRedis:
    """The default path must gain no new dependency and no new failure mode."""

    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_leader_answered_from_config(self, mock_settings, mock_redis):
        mock_settings.ha = _cfg(role="leader")

        assert await ha_role.get_role() == "leader"
        assert await ha_role.is_leader() is True
        mock_redis.assert_not_called()

    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_follower_answered_from_config(self, mock_settings, mock_redis):
        mock_settings.ha = _cfg(role="follower")

        assert await ha_role.get_role() == "follower"
        assert await ha_role.is_leader() is False
        mock_redis.assert_not_called()


class TestAutoReadsTheNodesRedis:
    """Role is node-level: one replica probes, every replica reads."""

    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_reads_resolved_role(self, mock_settings, mock_get_redis):
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x")
        redis = AsyncMock()
        redis.get.return_value = b"leader"
        mock_get_redis.return_value = redis

        assert await ha_role.get_role() == "leader"

    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_unresolved_is_follower(self, mock_settings, mock_get_redis):
        """A node that has started but not yet probed has not earned leadership."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x")
        redis = AsyncMock()
        redis.get.return_value = None
        mock_get_redis.return_value = redis

        assert await ha_role.get_role() == "follower"

    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_unreadable_role_fails_passive(self, mock_settings, mock_get_redis):
        """A node that cannot determine its role does not have one."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x")
        mock_get_redis.side_effect = RuntimeError("redis down")

        assert await ha_role.get_role() == "follower"


class TestProbeCycle:
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_static_role_never_probes(self, mock_settings, mock_get_redis):
        mock_settings.ha = _cfg(role="leader")

        await ha_role.probe_cycle()

        mock_get_redis.assert_not_called()

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_agreement_clears_the_streak(self, mock_settings, mock_get_redis, mock_observe):
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x")
        redis = AsyncMock()
        redis.get.return_value = b"follower"  # current role
        mock_get_redis.return_value = redis
        mock_observe.return_value = "b"  # someone else holds the name

        await ha_role.probe_cycle()

        redis.delete.assert_awaited_with(ha_role._STREAK_KEY)
        # No role write — nothing changed.
        assert not any(c.args and c.args[0] == ha_role._ROLE_KEY for c in redis.set.await_args_list)

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_one_observation_is_not_enough(self, mock_settings, mock_get_redis, mock_observe):
        """Failover must not be one probe away."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x", threshold=3)
        redis = AsyncMock()
        redis.get.side_effect = [b"follower", None]  # role, then no prior streak
        mock_get_redis.return_value = redis
        mock_observe.return_value = "a"  # we now hold the name

        await ha_role.probe_cycle()

        redis.set.assert_any_await(ha_role._STREAK_KEY, "leader:1")
        assert not any(c.args and c.args[0] == ha_role._ROLE_KEY for c in redis.set.await_args_list)

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_promotes_at_the_threshold(self, mock_settings, mock_get_redis, mock_observe):
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x", threshold=3)
        redis = AsyncMock()
        redis.get.side_effect = [b"follower", b"leader:2"]  # one short of the threshold
        mock_get_redis.return_value = redis
        mock_observe.return_value = "a"

        await ha_role.probe_cycle()

        redis.set.assert_any_await(ha_role._ROLE_KEY, "leader")

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_demotes_at_the_same_threshold(self, mock_settings, mock_get_redis, mock_observe):
        """Symmetric: demotion takes exactly as many observations as promotion."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x", threshold=3)
        redis = AsyncMock()
        redis.get.side_effect = [b"leader", b"follower:2"]
        mock_get_redis.return_value = redis
        mock_observe.return_value = "b"  # the name now reaches the peer

        await ha_role.probe_cycle()

        redis.set.assert_any_await(ha_role._ROLE_KEY, "follower")

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_unanswered_probe_counts_toward_demotion(
        self, mock_settings, mock_get_redis, mock_observe
    ):
        """No answer is not the same as 'still mine' — it is not mine."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x", threshold=3)
        redis = AsyncMock()
        redis.get.side_effect = [b"leader", b"follower:2"]
        mock_get_redis.return_value = redis
        mock_observe.return_value = None

        await ha_role.probe_cycle()

        redis.set.assert_any_await(ha_role._ROLE_KEY, "follower")

    @patch("terrapod.services.ha_role._observe")
    @patch("terrapod.services.ha_role.get_redis_client")
    @patch("terrapod.services.ha_role.settings")
    async def test_interrupted_streak_restarts(self, mock_settings, mock_get_redis, mock_observe):
        """A streak in the other direction does not carry over."""
        mock_settings.ha = _cfg(role="auto", node_name="a", internal="https://x", threshold=3)
        redis = AsyncMock()
        redis.get.side_effect = [b"follower", b"somethingelse:2"]
        mock_get_redis.return_value = redis
        mock_observe.return_value = "a"

        await ha_role.probe_cycle()

        redis.set.assert_any_await(ha_role._STREAK_KEY, "leader:1")
