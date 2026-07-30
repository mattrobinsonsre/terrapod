"""Two ways a pair could break its own replication, both found on a live pair.

Neither is theoretical. A real two-node deployment sat in permanent failure with
the follower logging `POST /oauth/token 429` every cycle and never recovering:

1. **The leader pulled too.** `_FOLLOWER_SAFE_TASKS` says "allowed on a
   follower"; it does not say "not on a leader", so both nodes ran the pull
   tasks and each minted a token against the other every cycle. It is also
   wrong by design — the follower pulls precisely so a peer outage cannot block
   a healthy leader, and a leader that pulls has taken a dependency on the node
   it is supposed to be able to survive.

2. **A 429 was treated as a rejected credential.** Discarding the cached token
   on one guarantees a fresh mint next cycle, into the same limit that just
   refused it. And `/oauth/token` shared the strict login bucket meant to slow
   password guessing, so the ceiling for a machine grant was 10/min.
"""

import httpx
import pytest

from terrapod.api.rate_limit import _is_auth_path
from terrapod.services.scheduler import (
    _FOLLOWER_ONLY_TASKS,
    _FOLLOWER_SAFE_TASKS,
    should_run_here,
)


class TestOnlyTheFollowerPulls:
    def test_the_pull_tasks_are_follower_only(self):
        assert _FOLLOWER_ONLY_TASKS == {"replication_sync", "blob_sync"}

    def test_follower_only_is_a_subset_of_follower_safe(self):
        # Otherwise the follower — the node that is supposed to run them —
        # would be the one skipping them.
        assert _FOLLOWER_ONLY_TASKS <= _FOLLOWER_SAFE_TASKS

    @pytest.mark.parametrize("task", sorted(_FOLLOWER_ONLY_TASKS))
    def test_a_leader_does_not_pull(self, task):
        assert should_run_here(task, is_leader=True) is False

    @pytest.mark.parametrize("task", sorted(_FOLLOWER_ONLY_TASKS))
    def test_a_follower_does(self, task):
        """The negative above is only meaningful if the positive holds —
        otherwise it would pass against a scheduler that runs nothing."""
        assert should_run_here(task, is_leader=False) is True

    def test_ordinary_work_stays_leader_only(self):
        # The gate that predates this must not have been widened by it: a
        # follower running `vcs_poll` burns VCS quota and records spurious
        # failures on every workspace.
        assert should_run_here("vcs_poll", is_leader=True) is True
        assert should_run_here("vcs_poll", is_leader=False) is False

    def test_the_other_follower_safe_tasks_run_on_both(self):
        for task in sorted(_FOLLOWER_SAFE_TASKS - _FOLLOWER_ONLY_TASKS):
            assert should_run_here(task, is_leader=True) is True, task
            assert should_run_here(task, is_leader=False) is True, task


class TestTheGrantIsNotRateLimitedAsALogin:
    def test_the_login_endpoints_keep_the_strict_bucket(self):
        # That bucket exists to slow password guessing. Nothing here relaxes it.
        assert _is_auth_path("/api/terrapod/v1/auth/local/authorize") is True
        assert _is_auth_path("/oauth/authorize") is True

    def test_the_token_endpoint_does_not(self):
        """A pair minting a machine token must not compete with human logins for
        a 10/min budget — that is how a pair rate-limited its own replication
        into permanent failure."""
        assert _is_auth_path("/oauth/token") is False


class TestATransientFailureKeepsTheCachedToken:
    """A rate limit or a 5xx says nothing about whether the credential is valid.
    Dropping the cache there converts a transient failure into a permanent one,
    because the retry it forces runs straight back into the same limit."""

    @staticmethod
    def _http_error(status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://peer/oauth/token")
        return httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(status, request=request)
        )

    @pytest.mark.parametrize(
        "status,should_reset",
        [(429, False), (500, False), (503, False), (401, True), (403, True)],
    )
    async def test_settings_sync_clears_the_token_only_on_a_rejection(
        self, monkeypatch, status, should_reset
    ):
        from terrapod.services import replication_sync

        async def boom(_client):
            raise self._http_error(status)

        cleared: list[bool] = []
        monkeypatch.setattr(replication_sync, "_peer_token", boom)
        monkeypatch.setattr(replication_sync, "reset_token_cache", lambda: cleared.append(True))
        monkeypatch.setattr(replication_sync.settings.ha.peer, "url", "https://peer")
        monkeypatch.setattr(replication_sync.settings.ha.replication, "enabled", True)

        await replication_sync.sync_cycle()

        assert bool(cleared) is should_reset

    @pytest.mark.parametrize(
        "status,should_reset",
        [(429, False), (503, False), (401, True), (403, True)],
    )
    async def test_blob_sync_clears_the_token_only_on_a_rejection(
        self, monkeypatch, status, should_reset
    ):
        from terrapod.services import blob_classes, blob_sync, replication_sync

        async def boom(_client):
            raise self._http_error(status)

        cleared: list[bool] = []
        monkeypatch.setattr(blob_sync, "_peer_token", boom)
        monkeypatch.setattr(replication_sync, "reset_token_cache", lambda: cleared.append(True))
        monkeypatch.setattr(blob_sync.settings.ha.peer, "url", "https://peer")
        # It returns early unless at least one class is set to copy.
        monkeypatch.setattr(blob_classes, "effective_mode", lambda _c: blob_sync.COPY)

        result = await blob_sync.run_cycle()

        assert result.skipped_reason == "peer authentication failed"
        assert bool(cleared) is should_reset
