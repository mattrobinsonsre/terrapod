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


class TestFindingsOnlyWhereAchievable:
    """The safety property (#1128).

    A false finding in an HA readout is worse than silence: it teaches an
    operator to ignore the readout. So a gap is only reported where the cluster
    could actually have closed it.
    """

    def _c(self, **kw):
        base = {
            "name": "api",
            "ready": 3,
            "desired": 3,
            "nodes": 1,
            "pdb": "api-pdb",
            "pdb_permits_disruption": True,
        }
        base.update(kw)
        return ComponentReplicas(**base)

    def test_single_node_cluster_raises_no_concentration_finding(self):
        """k3s, kind, Docker Desktop. Every replica is on one node
        necessarily — flagging it is noise, not signal."""
        findings = component_status.derive_findings(
            [self._c(nodes=1)], schedulable_nodes=1, cluster_zones=None
        )

        assert [f.kind for f in findings] == []

    def test_unknown_cluster_shape_raises_no_concentration_finding(self):
        """Node reads declined. We cannot tell an inevitable single node from
        an avoidable one, and not knowing is not a failure."""
        findings = component_status.derive_findings(
            [self._c(nodes=1)], schedulable_nodes=None, cluster_zones=None
        )

        assert [f.kind for f in findings] == []

    def test_concentration_on_a_multi_node_cluster_is_a_finding(self):
        findings = component_status.derive_findings(
            [self._c(nodes=1)], schedulable_nodes=4, cluster_zones=None
        )

        assert [f.kind for f in findings] == ["node-concentration"]
        assert "at least 4" in findings[0].detail

    def test_spread_across_nodes_is_not_a_finding(self):
        findings = component_status.derive_findings(
            [self._c(nodes=3)], schedulable_nodes=4, cluster_zones=None
        )

        assert findings == []

    def test_an_unzoned_environment_raises_no_zone_finding(self):
        """Bare metal and on-prem carry no zone labels at all, so there is no
        zone redundancy to be missing. A cluster that IS zoned but sits in one
        AZ is a different case, covered in TestZonedEnvironmentVsUnzoned."""
        findings = component_status.derive_findings(
            [self._c(nodes=3, zones=None)], schedulable_nodes=4, cluster_zones=None
        )

        assert [f.kind for f in findings] == []

    def test_one_zone_of_several_is_a_finding(self):
        findings = component_status.derive_findings(
            [self._c(nodes=3, zones=1)], schedulable_nodes=6, cluster_zones=3
        )

        assert [f.kind for f in findings] == ["zone-concentration"]


class TestDisruptionBudgetFindings:
    """These hold on any cluster — they compare the budget to the replica
    count and need no topology at all."""

    def _c(self, **kw):
        base = {"name": "api", "ready": 3, "desired": 3, "nodes": 3}
        base.update(kw)
        return ComponentReplicas(**base)

    def test_multi_replica_without_a_pdb(self):
        """A node drain can take every replica at once."""
        findings = component_status.derive_findings([self._c(pdb="")], None, None)

        assert [f.kind for f in findings] == ["no-pdb"]

    def test_a_pdb_that_permits_no_eviction(self):
        """Looks protective, is the opposite: a node drain stalls rather than
        proceeding safely. Worth naming because people configure it on
        purpose without realising."""
        findings = component_status.derive_findings(
            [self._c(pdb="api-pdb", pdb_permits_disruption=False)], None, None
        )

        assert [f.kind for f in findings] == ["pdb-blocks-eviction"]
        assert "stall" in findings[0].detail

    def test_a_working_pdb_is_not_a_finding(self):
        findings = component_status.derive_findings(
            [self._c(pdb="api-pdb", pdb_permits_disruption=True)], None, None
        )

        assert findings == []

    def test_a_single_replica_component_is_not_double_reported(self):
        """It already shows in `single-replica-components`; adding 'no PDB'
        and 'one node' on top is noise about a fact already stated."""
        findings = component_status.derive_findings(
            [ComponentReplicas(name="web", ready=1, desired=1, nodes=1, pdb="")],
            schedulable_nodes=4,
            cluster_zones=3,
        )

        assert findings == []


class TestDisruptionsAllowed:
    """`status.disruptionsAllowed` is the cluster's own answer and wins; the
    spec is the fallback for the seconds after a PDB is created, when reporting
    'blocks eviction' after every deploy would be noise."""

    def _pdb(self, allowed=None, max_unavailable=None, min_available=None):
        return SimpleNamespace(
            status=SimpleNamespace(disruptions_allowed=allowed) if allowed is not None else None,
            spec=SimpleNamespace(max_unavailable=max_unavailable, min_available=min_available),
        )

    def test_the_cluster_answer_wins(self):
        assert component_status._pdb_permits_disruption(self._pdb(allowed=1), 3) is True
        assert component_status._pdb_permits_disruption(self._pdb(allowed=0), 3) is False

    def test_max_unavailable_zero_blocks(self):
        assert component_status._pdb_permits_disruption(self._pdb(max_unavailable=0), 3) is False

    def test_min_available_equal_to_replicas_blocks(self):
        assert component_status._pdb_permits_disruption(self._pdb(min_available=3), 3) is False

    def test_min_available_below_replicas_permits(self):
        assert component_status._pdb_permits_disruption(self._pdb(min_available=2), 3) is True

    def test_min_available_one_hundred_percent_blocks(self):
        assert component_status._pdb_permits_disruption(self._pdb(min_available="100%"), 3) is False


class TestPodDerivedNodeFallback:
    """When cluster-scoped node reads are declined (#1128).

    Safe because it is asymmetric: two nodes among our own pods PROVES spread
    was possible; one proves nothing. Proof is only needed to raise a finding,
    never to suppress one — so it under-reports and never false-reports.
    """

    def test_two_observed_nodes_still_yields_a_finding(self):
        """Declining the ClusterRole must not blind Terrapod to a concentration
        it can already prove."""
        findings = component_status.derive_findings(
            [
                ComponentReplicas(
                    name="api", ready=3, desired=3, nodes=1, pdb="p", pdb_permits_disruption=True
                )
            ],
            schedulable_nodes=2,
            cluster_zones=None,
        )

        assert [f.kind for f in findings] == ["node-concentration"]
        assert "at least 2" in findings[0].detail, (
            "the wording must not claim an exact cluster size the fallback cannot know"
        )

    def test_zones_stay_unknown_without_node_reads(self):
        """Zone labels live only on Nodes — no pod-side equivalent exists."""
        findings = component_status.derive_findings(
            [
                ComponentReplicas(
                    name="api",
                    ready=3,
                    desired=3,
                    nodes=2,
                    zones=None,
                    pdb="p",
                    pdb_permits_disruption=True,
                )
            ],
            schedulable_nodes=2,
            cluster_zones=None,
        )

        assert findings == []


class TestZonedEnvironmentVsUnzoned:
    """`None` and `1` zones mean completely different things.

    Conflating them — which an earlier version did — loses the one case worth
    reporting: a zoned environment where the whole cluster sits in one AZ.
    """

    def _healthy(self):
        return ComponentReplicas(
            name="api",
            ready=3,
            desired=3,
            nodes=3,
            zones=1,
            pdb="p",
            pdb_permits_disruption=True,
        )

    def test_no_zone_labels_says_nothing(self):
        """Bare metal, on-prem, unzoned. There is no zone redundancy to have,
        so claiming its absence would be a false finding."""
        findings = component_status.derive_findings(
            [self._healthy()], schedulable_nodes=3, cluster_zones=None
        )

        assert findings == []

    def test_a_zoned_cluster_confined_to_one_zone_is_reported(self):
        """The labels prove a zoned environment. A cluster entirely inside one
        AZ there is a real gap — no placement of replicas survives losing that
        zone — even though Terrapod cannot schedule around it."""
        findings = component_status.derive_findings(
            [self._healthy()], schedulable_nodes=3, cluster_zones=1
        )

        assert [f.kind for f in findings] == ["single-zone-cluster"]
        assert findings[0].component == "cluster", (
            "it is a property of the cluster, not a component"
        )

    def test_a_multi_zone_cluster_reports_nothing_at_cluster_level(self):
        findings = component_status.derive_findings(
            [
                ComponentReplicas(
                    name="api",
                    ready=3,
                    desired=3,
                    nodes=3,
                    zones=3,
                    pdb="p",
                    pdb_permits_disruption=True,
                )
            ],
            schedulable_nodes=3,
            cluster_zones=3,
        )

        assert findings == []

    def test_the_cluster_finding_does_not_suppress_component_findings(self):
        """A single-zone cluster can still have a component pinned to one node
        of several — both are true and both are actionable."""
        findings = component_status.derive_findings(
            [
                ComponentReplicas(
                    name="api",
                    ready=3,
                    desired=3,
                    nodes=1,
                    zones=1,
                    pdb="p",
                    pdb_permits_disruption=True,
                )
            ],
            schedulable_nodes=4,
            cluster_zones=1,
        )

        assert {f.kind for f in findings} == {"single-zone-cluster", "node-concentration"}
