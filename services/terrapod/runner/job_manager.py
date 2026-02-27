"""K8s Job lifecycle management — create, watch, delete.

Uses the kubernetes Python client to interact with the K8s API.
"""

import asyncio
import os

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_batch_v1: client.BatchV1Api | None = None
_core_v1: client.CoreV1Api | None = None


def init_k8s() -> None:
    """Initialize the Kubernetes client.

    Uses in-cluster config when running in K8s, falls back to kubeconfig for local dev.
    """
    global _batch_v1, _core_v1  # noqa: PLW0603

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster K8s config")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig")
        except config.ConfigException:
            logger.error("Failed to load K8s config")
            raise

    _batch_v1 = client.BatchV1Api()
    _core_v1 = client.CoreV1Api()


def _get_batch_api() -> client.BatchV1Api:
    if _batch_v1 is None:
        init_k8s()
    assert _batch_v1 is not None
    return _batch_v1


def _get_core_api() -> client.CoreV1Api:
    if _core_v1 is None:
        init_k8s()
    assert _core_v1 is not None
    return _core_v1


def _default_namespace() -> str:
    return os.environ.get("TERRAPOD_RUNNER_NAMESPACE", "terrapod-runners")


async def create_job(job_spec: dict, namespace: str = "") -> str:
    """Create a K8s Job from a spec dict.

    Returns the job name.
    """
    if not namespace:
        namespace = job_spec.get("metadata", {}).get("namespace", _default_namespace())

    batch_api = _get_batch_api()
    job_name = job_spec.get("metadata", {}).get("name", "unknown")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: batch_api.create_namespaced_job(namespace=namespace, body=job_spec),
        )
        logger.info("Created K8s Job", job=job_name, namespace=namespace)
        return job_name
    except ApiException as e:
        logger.error("Failed to create Job", job=job_name, error=str(e))
        raise


async def watch_job(
    job_name: str,
    namespace: str = "",
    timeout_seconds: int = 3600,
) -> str:
    """Watch a Job until completion or failure.

    Returns the final status: "succeeded", "failed", or "timeout".
    """
    if not namespace:
        namespace = _default_namespace()

    batch_api = _get_batch_api()

    def _watch() -> str:
        w = watch.Watch()
        try:
            for event in w.stream(
                batch_api.list_namespaced_job,
                namespace=namespace,
                field_selector=f"metadata.name={job_name}",
                timeout_seconds=timeout_seconds,
            ):
                job = event["object"]
                if job.status.succeeded and job.status.succeeded > 0:
                    w.stop()
                    return "succeeded"
                if job.status.failed and job.status.failed > 0:
                    w.stop()
                    return "failed"
        except Exception:
            pass
        return "timeout"

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _watch)


async def delete_job(
    job_name: str,
    namespace: str = "",
    grace_period: int = 120,
) -> None:
    """Delete a K8s Job with grace period.

    Used for cancellation — sends SIGTERM to the container.
    """
    if not namespace:
        namespace = _default_namespace()

    batch_api = _get_batch_api()

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: batch_api.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=client.V1DeleteOptions(
                    propagation_policy="Foreground",
                    grace_period_seconds=grace_period,
                ),
            ),
        )
        logger.info("Deleted K8s Job", job=job_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            logger.warning("Job already deleted", job=job_name)
        else:
            logger.error("Failed to delete Job", job=job_name, error=str(e))
            raise


async def get_job_status(job_name: str, namespace: str = "") -> str | None:
    """Get the current status of a Job.

    Returns "running", "succeeded", "failed", or None if not found.
    """
    if not namespace:
        namespace = _default_namespace()

    batch_api = _get_batch_api()

    try:
        loop = asyncio.get_event_loop()
        job = await loop.run_in_executor(
            None,
            lambda: batch_api.read_namespaced_job(name=job_name, namespace=namespace),
        )

        if job.status.succeeded and job.status.succeeded > 0:
            return "succeeded"
        if job.status.failed and job.status.failed > 0:
            return "failed"
        if job.status.active and job.status.active > 0:
            return "running"
        return "running"  # Job exists but no status yet
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def get_pod_logs(
    job_name: str,
    namespace: str = "",
    tail_lines: int = 100,
) -> str:
    """Get logs from a Job's pod."""
    if not namespace:
        namespace = _default_namespace()

    core_api = _get_core_api()

    try:
        loop = asyncio.get_event_loop()
        pods = await loop.run_in_executor(
            None,
            lambda: core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            ),
        )

        if not pods.items:
            return ""

        pod_name = pods.items[0].metadata.name
        logs = await loop.run_in_executor(
            None,
            lambda: core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
            ),
        )
        return logs
    except ApiException:
        return ""
