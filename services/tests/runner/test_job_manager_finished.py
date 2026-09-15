"""``job_is_finished`` reads the Job's own condition, not its pod counters (#1649).

``get_job_status`` reports ``failed`` as soon as ``status.failed`` counts a pod,
which is also the state of a Job that Kubernetes is retrying with a new pod
(``backoffLimit``). Revoking a phase's Vault leases then would pull the
credentials from under the retry, so the listener also reports whether the Job
carries a Complete or Failed condition.
"""

from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

from terrapod.runner.job_manager import job_is_finished


def _job(*conditions, failed=0):
    job = MagicMock()
    job.status.failed = failed
    job.status.conditions = [MagicMock(type=t, status=s) for t, s in conditions]
    return job


def _batch(job=None, exc=None):
    api = MagicMock()
    if exc is not None:
        api.read_namespaced_job.side_effect = exc
    else:
        api.read_namespaced_job.return_value = job
    return patch("terrapod.runner.job_manager._get_batch_api", return_value=api)


async def test_a_complete_condition_is_finished():
    with _batch(_job(("Complete", "True"))):
        assert await job_is_finished("j", namespace="ns") is True


async def test_a_failed_condition_is_finished():
    with _batch(_job(("Failed", "True"), failed=4)):
        assert await job_is_finished("j", namespace="ns") is True


async def test_a_failed_pod_being_retried_is_not_finished():
    with _batch(_job(failed=1)):
        assert await job_is_finished("j", namespace="ns") is False


async def test_a_failure_target_before_the_pods_are_gone_is_not_finished():
    # Kubernetes 1.31+ sets FailureTarget first and Failed once pods terminate.
    with _batch(_job(("FailureTarget", "True"), failed=1)):
        assert await job_is_finished("j", namespace="ns") is False


async def test_a_false_condition_is_not_finished():
    with _batch(_job(("Complete", "False"))):
        assert await job_is_finished("j", namespace="ns") is False


async def test_a_job_that_no_longer_exists_is_finished():
    with _batch(exc=ApiException(status=404)):
        assert await job_is_finished("j", namespace="ns") is True


async def test_any_other_api_error_is_raised():
    import pytest

    with _batch(exc=ApiException(status=500)), pytest.raises(ApiException):
        await job_is_finished("j", namespace="ns")
