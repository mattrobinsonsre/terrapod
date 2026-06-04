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
