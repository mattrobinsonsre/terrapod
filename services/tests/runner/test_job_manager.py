"""Tests for runner.job_manager — specifically the get_job_failure_info
fallback used when the pod has been GC'd before the listener could read
its terminated state."""

from types import SimpleNamespace
from unittest.mock import patch

from kubernetes.client.rest import ApiException


def _job_with_conditions(conditions):
    return SimpleNamespace(status=SimpleNamespace(conditions=conditions))


class TestGetJobFailureInfo:
    """Fallback from #430 — when the pod is gone, recover the exit code
    from Job.status.conditions[?].reason == "PodFailurePolicy"."""

    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_extracts_exit_code_137_from_taint_eviction_message(self, _ns, mock_batch):
        """The K8s Job-controller writes a condition like:
            reason=PodFailurePolicy
            message="Container runner for pod terrapod/tprun-xxx-lx72v
                     failed with exit code 137 matching FailJob rule at index 0"
        We need to parse the 137 out."""
        from terrapod.runner.job_manager import get_job_failure_info

        mock_batch.return_value.read_namespaced_job.return_value = _job_with_conditions(
            [
                SimpleNamespace(reason="Other", message="unrelated"),
                SimpleNamespace(
                    reason="PodFailurePolicy",
                    message=(
                        "Container runner for pod terrapod/tprun-abc-lx72v "
                        "failed with exit code 137 matching FailJob rule at index 0"
                    ),
                ),
            ]
        )

        info = await get_job_failure_info("tprun-abc-plan", namespace="terrapod")
        assert info == {"exit_code": 137, "reason": ""}

    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_extracts_exit_code_1_for_normal_tofu_error(self, _ns, mock_batch):
        """Same fallback used when tofu exits 1 and the pod is somehow GC'd.
        Reason is NOT OOMKilled in that case, so the API maps exit_code=1
        + reason='' to runner_exit_status='error'."""
        from terrapod.runner.job_manager import get_job_failure_info

        mock_batch.return_value.read_namespaced_job.return_value = _job_with_conditions(
            [
                SimpleNamespace(
                    reason="PodFailurePolicy",
                    message="Container runner ... failed with exit code 1 matching ...",
                ),
            ]
        )

        info = await get_job_failure_info("tprun-abc-plan", namespace="terrapod")
        assert info == {"exit_code": 1, "reason": ""}

    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_returns_none_when_no_podfailurepolicy_condition(self, _ns, mock_batch):
        """If the Job failed for some other reason (e.g. DeadlineExceeded)
        we have no exit code to extract — return None so the API leaves
        runner_exit_status empty and the reconciler renders the generic
        message. That's strictly no worse than the pre-fix behaviour."""
        from terrapod.runner.job_manager import get_job_failure_info

        mock_batch.return_value.read_namespaced_job.return_value = _job_with_conditions(
            [
                SimpleNamespace(reason="DeadlineExceeded", message="..."),
            ]
        )

        assert await get_job_failure_info("tprun-abc-plan", namespace="terrapod") is None

    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_returns_none_on_unparseable_message(self, _ns, mock_batch):
        """If the message format changes upstream and our regex doesn't match,
        return None rather than guessing."""
        from terrapod.runner.job_manager import get_job_failure_info

        mock_batch.return_value.read_namespaced_job.return_value = _job_with_conditions(
            [
                SimpleNamespace(
                    reason="PodFailurePolicy",
                    message="Some new wording that doesn't say 'exit code N'.",
                ),
            ]
        )

        assert await get_job_failure_info("tprun-abc-plan", namespace="terrapod") is None

    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_swallows_apiexception(self, _ns, mock_batch):
        """Never raise. If the K8s API call fails (Job already deleted, RBAC
        issue, network hiccup), return None and let the listener fall back
        to the generic path."""
        from terrapod.runner.job_manager import get_job_failure_info

        mock_batch.return_value.read_namespaced_job.side_effect = ApiException(404)

        assert await get_job_failure_info("tprun-abc-plan", namespace="terrapod") is None


class TestDefaultNamespace:
    """Regression: the runner-namespace fallback must come from the loaded
    RunnerConfig (the runners.yaml ConfigMap), NOT a removed env var.

    The config-channel refactor moved the runner namespace from the
    TERRAPOD_RUNNER_NAMESPACE env var onto the ConfigMap. A call site that
    omitted an explicit namespace (get_job_uid during auth-Secret creation)
    then fell back to the stale hard-coded default and created the Secret /
    looked up the Job in the wrong namespace than the one the Job itself was
    created in — a 403 on every launch when the configured namespace differed
    from that default."""

    def test_default_namespace_sourced_from_runner_config(self):
        from terrapod.runner import job_manager

        fake_cfg = SimpleNamespace(runner_namespace="cfg-runner-ns")
        with patch("terrapod.config.load_runner_config", return_value=fake_cfg):
            assert job_manager._default_namespace() == "cfg-runner-ns"

    def test_default_namespace_ignores_legacy_env(self, monkeypatch):
        from terrapod.runner import job_manager

        # The chart no longer sets this; the fallback must not honour it.
        monkeypatch.setenv("TERRAPOD_RUNNER_NAMESPACE", "should-be-ignored")
        fake_cfg = SimpleNamespace(runner_namespace="cfg-runner-ns")
        with patch("terrapod.config.load_runner_config", return_value=fake_cfg):
            assert job_manager._default_namespace() == "cfg-runner-ns"


def _pod(phase, sched_status="True", reason=None):
    cond = SimpleNamespace(type="PodScheduled", status=sched_status, reason=reason)
    return SimpleNamespace(status=SimpleNamespace(phase=phase, conditions=[cond]))


def _job(succeeded=None, failed=None, active=None):
    return SimpleNamespace(
        status=SimpleNamespace(succeeded=succeeded, failed=failed, active=active)
    )


class TestUnschedulableDetection:
    """#748 — a Pending pod the scheduler can't place counts as job.active, so
    get_job_status must surface it as `unschedulable` (not `running`) so the
    reconciler can fail fast with a clear reason."""

    @patch("terrapod.runner.job_manager._get_core_api")
    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_pending_unschedulable_reports_unschedulable(self, _ns, mock_batch, mock_core):
        from terrapod.runner.job_manager import get_job_status

        mock_batch.return_value.read_namespaced_job.return_value = _job(active=1)
        mock_core.return_value.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_pod("Pending", sched_status="False", reason="Unschedulable")]
        )
        assert await get_job_status("tprun-x-plan") == "unschedulable"

    @patch("terrapod.runner.job_manager._get_core_api")
    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_pending_but_still_scheduling_is_running(self, _ns, mock_batch, mock_core):
        # PodScheduled=False without reason=Unschedulable means "mid-scheduling",
        # not "can't be placed" — must NOT be flagged.
        mock_batch.return_value.read_namespaced_job.return_value = _job(active=1)
        mock_core.return_value.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_pod("Pending", sched_status="False", reason=None)]
        )
        from terrapod.runner.job_manager import get_job_status

        assert await get_job_status("tprun-x-plan") == "running"

    @patch("terrapod.runner.job_manager._get_core_api")
    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_running_pod_is_running(self, _ns, mock_batch, mock_core):
        mock_batch.return_value.read_namespaced_job.return_value = _job(active=1)
        mock_core.return_value.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_pod("Running")]
        )
        from terrapod.runner.job_manager import get_job_status

        assert await get_job_status("tprun-x-plan") == "running"

    @patch("terrapod.runner.job_manager._get_core_api")
    @patch("terrapod.runner.job_manager._get_batch_api")
    @patch("terrapod.runner.job_manager._default_namespace", return_value="terrapod")
    async def test_succeeded_short_circuits_before_pod_check(self, _ns, mock_batch, mock_core):
        mock_batch.return_value.read_namespaced_job.return_value = _job(succeeded=1)
        from terrapod.runner.job_manager import get_job_status

        assert await get_job_status("tprun-x-plan") == "succeeded"
        mock_core.return_value.list_namespaced_pod.assert_not_called()
