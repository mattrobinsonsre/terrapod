"""Build K8s Job specs for terraform/tofu plan and apply phases."""

import json
import os
import re

from terrapod.config import RunnerConfig
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


def _double_resource(value: str) -> str:
    """Double a K8s resource value.

    Examples:
        '1' → '2', '500m' → '1', '2Gi' → '4Gi', '256Mi' → '512Mi'
    """
    match = re.match(r"^(\d+)(m|Ki|Mi|Gi|Ti)?$", value.strip())
    if not match:
        logger.warning("Could not parse resource value, returning as-is", value=value)
        return value

    number = int(match.group(1))
    suffix = match.group(2) or ""

    doubled = number * 2

    # Handle millicore promotion: 500m * 2 = 1000m = 1
    if suffix == "m" and doubled >= 1000:
        whole = doubled // 1000
        remainder = doubled % 1000
        if remainder == 0:
            return str(whole)
        return f"{doubled}m"

    return f"{doubled}{suffix}"


def build_job_spec(
    run_id: str,
    phase: str,  # "plan" or "apply"
    runner_config: RunnerConfig,
    presigned_urls: dict[str, str],
    env_vars: list[dict[str, str]],
    terraform_vars: list[dict[str, str]],
    resource_cpu: str = "1",
    resource_memory: str = "2Gi",
    timeout_minutes: int = 60,
    terraform_version: str = "",
    execution_backend: str = "",
    namespace: str = "",
    plan_only: bool = False,
    var_files: list[str] | None = None,
) -> dict:
    """Build a K8s Job spec for a run phase.

    Args:
        run_id: The run UUID.
        phase: "plan" or "apply".
        runner_config: Global runner config (image, defaults, etc.).
        presigned_urls: Download/upload URLs for artifacts.
        env_vars: Workspace env vars [{key, value}].
        terraform_vars: Terraform vars [{key, value}] → TF_VAR_*.
        resource_cpu: CPU request (e.g. "1", "500m").
        resource_memory: Memory request (e.g. "2Gi", "256Mi").
        timeout_minutes: Job timeout in minutes.
        terraform_version: Terraform/tofu version to use.
        execution_backend: Execution backend (terraform or tofu).
        namespace: Target namespace for the Job.
    """
    if not namespace:
        namespace = os.environ.get("TERRAPOD_RUNNER_NAMESPACE", "terrapod-runners")

    run_short = run_id[:8]
    job_name = f"tprun-{run_short}-{phase}"

    # Resolve the default runner definition for setup_script
    default_def = next(
        (d for d in runner_config.definitions if d.name == runner_config.default),
        runner_config.definitions[0] if runner_config.definitions else None,
    )

    # Build container env vars
    container_env = [
        {"name": "TP_RUN_ID", "value": run_id},
        {"name": "TP_PHASE", "value": phase},
        {
            "name": "TP_API_URL",
            "value": os.environ.get("TERRAPOD_API_URL", "http://terrapod-api:8000"),
        },
    ]

    # Setup script (runs before terraform init)
    setup_script = default_def.setup_script if default_def else ""
    if setup_script:
        container_env.append({"name": "TP_SETUP_SCRIPT", "value": setup_script})

    # Terraform version + backend
    version = terraform_version or runner_config.default_terraform_version
    backend = execution_backend or runner_config.default_execution_backend
    container_env.append({"name": "TP_VERSION", "value": version})
    container_env.append({"name": "TP_BACKEND", "value": backend})
    if plan_only:
        container_env.append({"name": "TP_PLAN_ONLY", "value": "true"})
    if var_files:
        container_env.append({"name": "TP_VAR_FILES", "value": json.dumps(var_files)})

    # Binary cache URL
    container_env.append(
        {
            "name": "TP_BINARY_URL",
            "value": presigned_urls.get("binary_url", ""),
        }
    )

    # Presigned URLs for artifacts
    url_mappings = {
        "TP_CONFIG_URL": "config_download_url",
        "TP_STATE_URL": "state_download_url",
        "TP_PLAN_LOG_UPLOAD_URL": "plan_log_upload_url",
        "TP_PLAN_FILE_UPLOAD_URL": "plan_file_upload_url",
        "TP_APPLY_LOG_UPLOAD_URL": "apply_log_upload_url",
        "TP_STATE_UPLOAD_URL": "state_upload_url",
        "TP_PLAN_FILE_DOWNLOAD_URL": "plan_file_download_url",
    }
    for env_name, url_key in url_mappings.items():
        if url_key in presigned_urls:
            container_env.append({"name": env_name, "value": presigned_urls[url_key]})

    # Workspace env vars (category=env)
    for var in env_vars:
        container_env.append({"name": var["key"], "value": var["value"]})

    # Terraform vars (category=terraform → TF_VAR_*)
    for var in terraform_vars:
        container_env.append({"name": f"TF_VAR_{var['key']}", "value": var["value"]})

    # Image config
    image = runner_config.image.repository
    if runner_config.image.tag:
        image = f"{image}:{runner_config.image.tag}"

    # Compute limits as 2x requests
    limit_cpu = _double_resource(resource_cpu)
    limit_memory = _double_resource(resource_memory)

    # Pod labels (conditionally include Azure Workload Identity label)
    pod_labels = {
        "app.kubernetes.io/name": "terrapod-runner",
        "terrapod.io/run-id": run_id,
        "terrapod.io/phase": phase,
    }
    if runner_config.azure_workload_identity:
        pod_labels["azure.workload.identity/use"] = "true"

    # Build Job spec
    job_spec = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "terrapod-runner",
                "app.kubernetes.io/component": "runner",
                "terrapod.io/run-id": run_id,
                "terrapod.io/phase": phase,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_minutes * 60,
            "ttlSecondsAfterFinished": runner_config.ttl_seconds_after_finished,
            "template": {
                "metadata": {
                    "labels": pod_labels,
                },
                "spec": {
                    "terminationGracePeriodSeconds": 120,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": bool(runner_config.service_account_name),
                    "volumes": [
                        {"name": "workspace", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                    "containers": [
                        {
                            "name": "runner",
                            "image": image,
                            "imagePullPolicy": runner_config.image.pull_policy,
                            "env": container_env,
                            "resources": {
                                "requests": {
                                    "cpu": resource_cpu,
                                    "memory": resource_memory,
                                },
                                "limits": {
                                    "cpu": limit_cpu,
                                    "memory": limit_memory,
                                },
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "volumeMounts": [
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                },
            },
        },
    }

    # Service account (CSP identity) — from global runner config (Helm values)
    if runner_config.service_account_name:
        job_spec["spec"]["template"]["spec"]["serviceAccountName"] = runner_config.service_account_name

    # Scheduling and placement from runner config
    pod_spec = job_spec["spec"]["template"]["spec"]
    if runner_config.node_selector:
        pod_spec["nodeSelector"] = runner_config.node_selector
    if runner_config.tolerations:
        pod_spec["tolerations"] = runner_config.tolerations
    if runner_config.affinity:
        pod_spec["affinity"] = runner_config.affinity
    if runner_config.priority_class_name:
        pod_spec["priorityClassName"] = runner_config.priority_class_name
    if runner_config.topology_spread_constraints:
        pod_spec["topologySpreadConstraints"] = runner_config.topology_spread_constraints
    if runner_config.pod_security_context:
        pod_spec["securityContext"] = runner_config.pod_security_context
    if runner_config.pod_annotations:
        pod_meta = job_spec["spec"]["template"]["metadata"]
        pod_meta["annotations"] = runner_config.pod_annotations

    return job_spec
