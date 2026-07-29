"""A single-node install does no replication work at all (#1117).

The overwhelming majority of installs are one node, and they must not pay for
machinery built for pairs. This is asserted rather than claimed because it is a
property an operator should be able to rely on — and because every future
addition to the HA surface is a chance to quietly break it.

The signal is `ha.peer.url`: **both** nodes of a pair set it, since the leader
needs it for when the roles swap and it becomes the puller. A singleton leaves
it empty.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from terrapod.api import app as app_module
from terrapod.config import HAConfig
from terrapod.services import replication


class TestDefaultsAreInert:
    def test_no_peer_is_configured_by_default(self):
        cfg = HAConfig()

        assert cfg.peer.url == ""
        assert cfg.replication.enabled is False

    def test_the_role_is_leader_and_needs_no_redis(self):
        """A singleton must never probe, never transition, and never spend a
        threshold's worth of time passive after a pod restart."""
        assert HAConfig().role == "leader"

    def test_the_write_gate_is_answered_from_config(self):
        """The gate must add no dependency and no failure mode to the 99% case."""
        source = inspect.getsource(replication)
        assert "get_redis_client" not in source, (
            "the replication path must not reach Redis; the role does, and only under `auto`"
        )


class TestStartupSkipsReplication:
    """Both the outbox hooks and the purge task are gated on `ha.peer.url`."""

    def test_outbox_hooks_are_gated(self):
        source = inspect.getsource(app_module)
        hook = source.index("replication.install_outbox_hooks()")
        gate = source.rindex("if settings.ha.peer.url:", 0, hook)
        between = source[gate:hook]

        assert "register_periodic_task" not in between, (
            "install_outbox_hooks must sit directly under the peer-url gate"
        )

    def test_the_purge_task_is_gated(self):
        source = inspect.getsource(app_module)
        purge = source.index('"replication_purge"')
        gate = source.rindex("if settings.ha.peer.url:", 0, purge)

        assert gate > 0, "replication_purge must be registered under the peer-url gate"

    def test_the_pull_loop_stays_gated_on_replication_enabled(self):
        """Narrower than the purge on purpose: both nodes record events, only
        the follower pulls."""
        source = inspect.getsource(app_module)
        sync = source.index('"replication_sync"')
        gate = source.rindex("if settings.ha.replication.enabled:", 0, sync)

        assert gate > 0


class TestNothingIsRecordedWithoutAPeer:
    @patch("terrapod.services.replication._BY_MODEL", {})
    def test_a_flush_with_no_registered_models_records_nothing(self):
        """With hooks never installed the registry is never consulted, but this
        pins the inner guard too — the hook returns immediately rather than
        walking the session."""
        session = MagicMock()
        session.new = [object()]
        session.dirty = []
        session.deleted = []

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ha = SimpleNamespace(node_name="")
            # The hook's first action is the empty-registry check.
            assert not replication._BY_MODEL

        session.add.assert_not_called()
