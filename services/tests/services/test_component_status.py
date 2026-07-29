"""In-cluster component readiness (#1122).

The half of "is this HA" that the pair does not answer: a deployment that
replicates flawlessly is still not highly available if it serves from one API
pod.

The behaviour that matters most is the failure mode. A missing RBAC Role must
report as **unknown**, never as zero — an operator may legitimately decline the
permission, and reporting a healthy deployment as dead the moment a permission
is absent is worse than not reporting at all.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from terrapod.services import component_status
from terrapod.services.component_status import ComponentReplicas, ComponentStatus


def _cfg(enabled=True, namespace="terrapod", instance="tp"):
    return SimpleNamespace(
        component_status=SimpleNamespace(
            enabled=enabled, interval_seconds=60, namespace=namespace, instance=instance
        )
    )


class TestSinglePointsOfFailure:
    def test_names_components_on_one_replica(self):
        """Named rather than left for the reader to derive — it is the thing
        being looked for."""
        status = ComponentStatus(
            components=[
                ComponentReplicas("api", ready=3, desired=3),
                ComponentReplicas("web", ready=1, desired=1),
            ]
        )

        assert status.single_points_of_failure == ["web"]

    def test_a_component_mid_incident_still_counts(self):
        """1-of-3 is a single point of failure right now, whatever was
        intended."""
        status = ComponentStatus(components=[ComponentReplicas("api", ready=1, desired=3)])

        assert status.single_points_of_failure == ["api"]

    def test_nothing_visible_means_nothing_named(self):
        """If the sample failed, no component can be asserted to be a SPOF."""
        assert ComponentStatus(components=None).single_points_of_failure == []

    def test_a_scaled_out_deployment_names_nothing(self):
        status = ComponentStatus(
            components=[
                ComponentReplicas("api", ready=3, desired=3),
                ComponentReplicas("web", ready=2, desired=2),
            ]
        )

        assert status.single_points_of_failure == []

    def test_zero_ready_is_not_a_single_point_of_failure(self):
        """It is an outage, which the ready/desired pair already says. Folding
        it in here would muddle 'fragile' with 'already down'."""
        status = ComponentStatus(components=[ComponentReplicas("api", ready=0, desired=3)])

        assert status.single_points_of_failure == []


class TestUnavailableIsNotZero:
    @patch("terrapod.services.component_status.settings")
    async def test_a_declined_role_reports_unknown(self, mock_settings):
        mock_settings.ha = _cfg()

        with patch(
            "terrapod.services.component_status.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=Exception('pods is forbidden: cannot list resource "pods"'),
        ):
            status = await component_status.sample()

        assert status.components is None, (
            "a missing permission must be None (unknown), never an empty list (nothing running)"
        )
        assert "forbidden" in status.unavailable_reason

    @patch("terrapod.services.component_status.settings")
    async def test_outside_kubernetes_reports_unknown(self, mock_settings):
        """Local dev and tests are not a broken cluster."""
        mock_settings.ha = _cfg(namespace="")

        with patch("builtins.open", side_effect=OSError):
            status = await component_status.sample()

        assert status.components is None
        assert "Kubernetes" in status.unavailable_reason

    @patch("terrapod.services.component_status.settings")
    async def test_sampling_never_raises(self, mock_settings):
        """It runs on a timer beside other tasks; a cluster-API blip must not
        take the scheduler cycle down."""
        mock_settings.ha = _cfg()

        with patch(
            "terrapod.services.component_status.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            await component_status.sample()  # must not raise


class TestReadPath:
    @patch("terrapod.services.component_status.settings")
    async def test_disabled_reports_so(self, mock_settings):
        mock_settings.ha = _cfg(enabled=False)

        status = await component_status.read()

        assert status.components is None
        assert "disabled" in status.unavailable_reason

    @patch("terrapod.services.component_status.settings")
    async def test_before_the_first_sample(self, mock_settings):
        mock_settings.ha = _cfg()
        redis = AsyncMock()
        redis.get.return_value = None

        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            status = await component_status.read()

        assert status.components is None
        assert "not sampled" in status.unavailable_reason

    @patch("terrapod.services.component_status.settings")
    async def test_reads_a_shared_sample(self, mock_settings):
        """Any replica answers identically without each one hammering the
        cluster API."""
        mock_settings.ha = _cfg()
        redis = AsyncMock()
        redis.get.return_value = json.dumps(
            {
                "components": [{"name": "api", "ready": 2, "desired": 3}],
                "sampled_at": "2026-07-29T10:00:00Z",
                "unavailable_reason": "",
            }
        )

        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            status = await component_status.read()

        assert status.components == [ComponentReplicas("api", 2, 3)]
        assert status.single_points_of_failure == []

    @patch("terrapod.services.component_status.settings")
    async def test_an_unreachable_cache_is_unknown_not_empty(self, mock_settings):
        mock_settings.ha = _cfg()

        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            status = await component_status.read()

        assert status.components is None


class TestScope:
    def test_listeners_are_not_sampled_from_kubernetes(self):
        """A listener may be in another cluster entirely — that is the ARC
        design. Counting only co-located ones would report a healthy remote
        fleet as absent; their replica counts come from Redis heartbeats,
        which already cross that boundary."""
        assert "listener" not in component_status.COMPONENTS
        assert set(component_status.COMPONENTS) == {"api", "web"}


class TestNoSyncWorkOnTheEventLoop:
    def test_the_kubernetes_read_is_offloaded(self):
        """The client is synchronous; calling it inline would stall the loop
        (rule 13). Asserted structurally because the regression is invisible
        until the API starts stalling under load."""
        import inspect

        source = inspect.getsource(component_status)
        assert "asyncio.to_thread(" in source
        # The blocking helper must not be awaited directly anywhere.
        assert "await _sample_blocking" not in source
