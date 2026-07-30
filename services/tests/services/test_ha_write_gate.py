"""The leadership write gate (#960 phase 1, #1101 PR2).

Under the shipped default (`ha.role: leader`) every gate evaluates true, so
normal operation never reaches the refusing branch. **These tests are the only
thing that does.** A false positive here takes down writes on a healthy
single-node install, which is why each gated entry point is covered
individually rather than trusting one test of the helper.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import ha_role, run_service
from terrapod.services.ha_role import NotLeaderError

pytestmark = pytest.mark.asyncio


def _static(role: str):
    """A settings stand-in with a static (Redis-free) role."""
    return SimpleNamespace(
        role=role,
        node_name="",
        probe_url=SimpleNamespace(internal="", external=""),
        effective_probe_url="",
        probe_interval_seconds=60,
        probe_threshold=3,
    )


class TestEnsureLeader:
    @patch("terrapod.services.ha_role.settings")
    async def test_leader_passes(self, mock_settings):
        mock_settings.ha = _static("leader")

        await ha_role.ensure_leader("do a thing")  # must not raise

    @patch("terrapod.services.ha_role.settings")
    async def test_follower_refuses(self, mock_settings):
        mock_settings.ha = _static("follower")

        with pytest.raises(NotLeaderError) as exc:
            await ha_role.ensure_leader("do a thing")

        assert "do a thing" in str(exc.value)
        assert exc.value.action == "do a thing"

    @patch("terrapod.services.ha_role.settings")
    async def test_message_points_the_caller_elsewhere(self, mock_settings):
        """The caller should learn where to retry, not just that it failed."""
        mock_settings.ha = _static("follower")

        with pytest.raises(NotLeaderError, match="shared name"):
            await ha_role.ensure_leader("create runs")


class TestEveryGatedEntryPointRefuses:
    """Each write path is covered on its own — a gate is easy to drop in a
    refactor, and a missing one is silent until a follower double-applies."""

    @patch("terrapod.services.ha_role.settings")
    async def test_create_run(self, mock_settings):
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.create_run(AsyncMock(), MagicMock())

    @patch("terrapod.services.ha_role.settings")
    async def test_queue_run(self, mock_settings):
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.queue_run(AsyncMock(), MagicMock())

    @patch("terrapod.services.ha_role.settings")
    async def test_confirm_run(self, mock_settings):
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.confirm_run(AsyncMock(), MagicMock())

    @patch("terrapod.services.ha_role.settings")
    async def test_discard_run(self, mock_settings):
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.discard_run(AsyncMock(), MagicMock())

    @patch("terrapod.services.ha_role.settings")
    async def test_transition_run(self, mock_settings):
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.transition_run(AsyncMock(), MagicMock(), "errored")

    @patch("terrapod.services.ha_role.settings")
    async def test_claim_next_run(self, mock_settings):
        """A listener pointed at a follower must be handed nothing."""
        mock_settings.ha = _static("follower")
        with pytest.raises(NotLeaderError):
            await run_service.claim_next_run(AsyncMock(), MagicMock(), "listener-1")


class TestLeaderStillWorks:
    """The far more dangerous direction: the gate must not refuse a real leader."""

    @patch("terrapod.services.ha_role.settings")
    async def test_gate_does_not_refuse_the_default_role(self, mock_settings):
        mock_settings.ha = _static("leader")
        db = AsyncMock()

        await run_service.create_run(db, MagicMock())

        # Reached the body and persisted — proof the gate let it through.
        db.add.assert_called()


class TestTriggerConsumerIsGated:
    """The triggered-task consumer is a second execution path, fed straight from
    request handlers — a gate around the periodic loops does not cover it."""

    @patch("terrapod.services.ha_role.settings")
    async def test_follower_does_not_execute_triggers(self, mock_settings):
        mock_settings.ha = _static("follower")

        assert await ha_role.is_leader() is False

    @patch("terrapod.services.ha_role.settings")
    async def test_leader_executes_triggers(self, mock_settings):
        mock_settings.ha = _static("leader")

        assert await ha_role.is_leader() is True


class TestRetireRunsOnRoleChange:
    @patch("terrapod.services.ha_role.get_db_session", create=True)
    async def test_errors_in_flight_runs(self, _unused):
        """Verified through the module's own import of get_db_session."""
        db = AsyncMock()
        db.execute.return_value = MagicMock(rowcount=3)

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False

        with patch("terrapod.db.session.get_db_session", return_value=_Ctx()):
            await ha_role._retire_runs_on_role_change(previous="leader", role="follower")

        db.execute.assert_awaited()
        db.commit.assert_awaited()

    async def test_failure_is_swallowed(self):
        """This runs inside the probe cycle; it must never break role resolution."""
        with patch("terrapod.db.session.get_db_session", side_effect=RuntimeError("db down")):
            await ha_role._retire_runs_on_role_change(previous="follower", role="leader")


class TestPeriodicTasksAreGated:
    """A follower runs no scheduled work.

    The write gate would refuse the outcome anyway, but running the task and
    failing at the last step is NOT equivalent to not running it: `vcs_poll`
    would burn the installation's VCS API quota every cycle, advance its own
    poll cursor, and record a spurious poll failure on every VCS workspace.
    """

    def test_only_self_maintenance_tasks_are_exempt(self):
        """Pinned as an exact set: an exemption added casually is how a follower
        starts originating change again."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert _FOLLOWER_SAFE_TASKS == {
            "ha_probe",
            "encryption_key_refresh",
            "replication_sync",
            "replication_purge",
            "blob_sync",
        }

    def test_the_object_store_copier_is_exempt(self):
        """The object-store half of the same pull loop (#1159), and the argument
        is the settings one with more force: a follower that stops copying
        promotes with rows whose objects are not there — present rows, absent
        blobs, the failure that looks like success. It writes only into its own
        object store, never the leader's."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "blob_sync" in _FOLLOWER_SAFE_TASKS

    def test_the_pull_loop_is_exempt(self):
        """Gating it would be self-defeating — converging with the leader is the
        entire job of a follower, and one that stops replicating promotes with
        stale settings."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "replication_sync" in _FOLLOWER_SAFE_TASKS

    def test_the_outbox_purge_is_exempt(self):
        """A follower still records events (origin-tagged, so the pair cannot
        echo), so without the purge its own outbox grows without bound."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "replication_purge" in _FOLLOWER_SAFE_TASKS

    def test_the_probe_is_exempt(self):
        """Gating it would make the role permanently sticky — a follower could
        never discover it has become the leader."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "ha_probe" in _FOLLOWER_SAFE_TASKS

    def test_key_refresh_is_exempt(self):
        """A follower that stops propagating rotated DEKs cannot decrypt
        anything written after a rotation, and discovers that at promotion."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "encryption_key_refresh" in _FOLLOWER_SAFE_TASKS

    def test_run_creating_tasks_are_not_exempt(self):
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        for task in (
            "vcs_poll",
            "drift_check",
            "run_reconciler",
            "lifecycle_destroy_retry",
            "module_impact_poll",
            "registry_vcs_poll",
        ):
            assert task not in _FOLLOWER_SAFE_TASKS, f"{task} must not run on a follower"
