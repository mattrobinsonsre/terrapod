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
    auth_secret_name: str,
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
    target_addrs: list[str] | None = None,
    replace_addrs: list[str] | None = None,
    refresh_only: bool = False,
    refresh: bool = True,
    allow_empty_apply: bool = False,
    working_directory: str = "",
) -> dict:
    """Build a K8s Job spec for a run phase.

    Args:
        run_id: The run UUID.
        phase: "plan" or "apply".
        runner_config: Global runner config (image, defaults, etc.).
        auth_secret_name: K8s Secret name containing the runner token.
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

    run_short = run_id[:16]
    job_name = f"tprun-{run_short}-{phase}"

    # Build container env vars
    container_env = [
        {"name": "TP_RUN_ID", "value": run_id},
        {"name": "TP_PHASE", "value": phase},
        {
            "name": "TP_API_URL",
            "value": os.environ.get("TERRAPOD_API_URL", "http://terrapod-api:8000"),
        },
        {
            "name": "TP_AUTH_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": auth_secret_name,
                    "key": "token",
                }
            },
        },
    ]

    # Terraform version + backend
    version = terraform_version or runner_config.default_terraform_version
    backend = execution_backend or runner_config.default_execution_backend
    container_env.append({"name": "TP_VERSION", "value": version})
    container_env.append({"name": "TP_BACKEND", "value": backend})
    if plan_only:
        container_env.append({"name": "TP_PLAN_ONLY", "value": "true"})
    if var_files:
        container_env.append({"name": "TP_VAR_FILES", "value": json.dumps(var_files)})
    if target_addrs:
        container_env.append({"name": "TP_TARGET_ADDRS", "value": json.dumps(target_addrs)})
    if replace_addrs:
        container_env.append({"name": "TP_REPLACE_ADDRS", "value": json.dumps(replace_addrs)})
    if refresh_only:
        container_env.append({"name": "TP_REFRESH_ONLY", "value": "true"})
    if not refresh:
        container_env.append({"name": "TP_REFRESH", "value": "false"})
    if allow_empty_apply:
        container_env.append({"name": "TP_ALLOW_EMPTY_APPLY", "value": "true"})
    if working_directory:
        container_env.append({"name": "TP_WORKING_DIR", "value": working_directory})

    # Termination grace period — passed to entrypoint for time-budgeted shutdown
    container_env.append(
        {
            "name": "TP_TERMINATION_GRACE",
            "value": str(runner_config.termination_grace_period_seconds),
        }
    )

    # Workspace env vars (category=env)
    for var in env_vars:
        container_env.append({"name": var["key"], "value": var["value"]})

    # Terraform vars (category=terraform → TF_VAR_*)
    for var in terraform_vars:
        container_env.append({"name": f"TF_VAR_{var['key']}", "value": var["value"]})

    # Extra env vars from runner config (Helm values → runners.extraEnv)
    for extra in runner_config.extra_env:
        container_env.append(extra)

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
            "backoffLimit": 3,
            "podFailurePolicy": {
                "rules": [
                    {
                        "action": "FailJob",
                        "onExitCodes": {
                            "containerName": "runner",
                            "operator": "NotIn",
                            "values": [0],
                        },
                    },
                    {
                        "action": "Count",
                        "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                    },
                ],
            },
            "activeDeadlineSeconds": timeout_minutes * 60,
            "ttlSecondsAfterFinished": runner_config.ttl_seconds_after_finished,
            "template": {
                "metadata": {
                    "labels": pod_labels,
                },
                "spec": {
                    "terminationGracePeriodSeconds": runner_config.termination_grace_period_seconds,
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

    # envFrom — inject all keys from Secrets/ConfigMaps as env vars
    if runner_config.extra_env_from:
        job_spec["spec"]["template"]["spec"]["containers"][0]["envFrom"] = (
            runner_config.extra_env_from
        )

    # Service account (CSP identity) — from global runner config (Helm values)
    if runner_config.service_account_name:
        job_spec["spec"]["template"]["spec"]["serviceAccountName"] = (
            runner_config.service_account_name
        )

    # Scheduling and placement from runner config
    pod_spec = job_spec["spec"]["template"]["spec"]

    # Image pull secrets — for pulling custom runner images from private registries
    if runner_config.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [{"name": s} for s in runner_config.image_pull_secrets]
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
