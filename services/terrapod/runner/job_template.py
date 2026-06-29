"""Build K8s Job specs for terraform/tofu plan and apply phases."""

import json
import os
import re

from terrapod.config import RunnerConfig, settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Per-run vars Secret (mounted file). Keep in sync with
# runner/job_entrypoint.py which reads the same path.
_TFVARS_SECRET_KEY = "terraform.tfvars.json"
_TFVARS_FILENAME = "terraform.tfvars.json"
_TFVARS_MOUNT_DIR = "/var/run/terrapod/vars"

# Custom outbound CA trust bundle (#592). The listener ships the raw custom CA
# into a per-run Secret under this key; an init container merges it with the
# runner image's own system roots into an emptyDir, and the runner container
# trusts the merged file. Paths/key mirror the chart's _helpers.tpl so the
# deployment and Job machinery stay identical.
_CA_BUNDLE_KEY = "ca-extra.pem"
_CA_SRC_DIR = "/etc/terrapod-ca-src"
_CA_MERGED_DIR = "/etc/terrapod-ca"
_CA_MERGED_FILE = f"{_CA_MERGED_DIR}/ca-bundle.crt"


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
    vars_secret_name: str = "",
    resource_cpu: str = "1",
    resource_memory: str = "2Gi",
    timeout_minutes: int = 60,
    terraform_version: str = "",
    execution_backend: str = "",
    terragrunt_enabled: bool = False,
    terragrunt_version: str = "",
    namespace: str = "",
    plan_only: bool = False,
    var_files: list[str] | None = None,
    target_addrs: list[str] | None = None,
    replace_addrs: list[str] | None = None,
    refresh_only: bool = False,
    refresh: bool = True,
    allow_empty_apply: bool = False,
    is_destroy: bool = False,
    working_directory: str = "",
    ca_secret_name: str = "",
) -> dict:
    """Build a K8s Job spec for a run phase.

    Args:
        run_id: The run UUID.
        phase: "plan" or "apply".
        runner_config: Global runner config (image, defaults, etc.).
        auth_secret_name: K8s Secret name containing the runner token.
        vars_secret_name: K8s Secret holding all workspace variable values
            (terraform tfvars blob + env vars). When set, env vars are sourced
            via secretKeyRef and terraform vars are mounted as a file — no
            variable value is plaintext in the Job spec.
        env_vars: Workspace env vars [{key, value}] — keys referenced via
            secretKeyRef into vars_secret_name.
        terraform_vars: Terraform vars [{key, value, hcl}] — presence triggers
            the mounted tfvars volume; the entrypoint renders the file.
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
    api_url = os.environ.get("TERRAPOD_API_URL", "http://terrapod-api:8000")
    container_env = [
        # HOME defends against tools that consult $HOME without a
        # passwd entry (helm's repository/index cache,
        # kubectl's ~/.kube/cache, AWS CLI's ~/.aws, git's
        # ~/.gitconfig). Without it, Go's os.UserHomeDir() returns
        # "" and downstream code writes to /.cache/… which isn't
        # writable for UID 1000 and surfaces as misleading
        # "cannot be reached" errors from helm specifically.
        # The default Terrapod runner image ships /home/runner
        # owned by 1000 (see Dockerfile.runner); setting it here
        # too means custom runner images that miss the ENV line
        # still get the right behaviour.
        {"name": "HOME", "value": "/home/runner"},
        {"name": "TP_RUN_ID", "value": run_id},
        {"name": "TP_PHASE", "value": phase},
        {"name": "TP_API_URL", "value": api_url},
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

    # Public/canonical API URL — only forwarded to the runner when it
    # differs from TP_API_URL. The entrypoint uses it to add a terraform
    # CLI host{} block redirecting the canonical hostname (the one users
    # type in `source = "..."` URLs and SSO callbacks) to TP_API_URL (the
    # internal URL the runner actually traverses). Skipped when same so
    # there's no needless self-redirect.
    #
    # Trailing slashes are normalised here so values that only differ by
    # a trailing `/` ("https://x.example/" vs "https://x.example") don't
    # trigger a no-op redirect — the entrypoint's host-extraction re-
    # checks at hostname level either way, this is just a tighter guard.
    public_api_url = os.environ.get("TERRAPOD_PUBLIC_API_URL", "").strip()
    if public_api_url and public_api_url.rstrip("/") != api_url.rstrip("/"):
        container_env.append({"name": "TP_PUBLIC_API_URL", "value": public_api_url})

    # Terraform version + backend
    version = terraform_version or runner_config.default_terraform_version
    backend = execution_backend or runner_config.default_execution_backend
    container_env.append({"name": "TP_VERSION", "value": version})
    container_env.append({"name": "TP_BACKEND", "value": backend})
    # Runner-side executable verification level (#607): the runner re-verifies
    # the terraform/tofu/terragrunt binary against the publisher's signed
    # SHA256SUMS with its own pinned key before executing it. Mirrors the
    # server's binary_cache.verify so operators control it in one place.
    container_env.append(
        {"name": "TP_VERIFY_BINARIES", "value": settings.registry.binary_cache.verify}
    )
    # Operator-overridden publisher keys (#607): propagate the configured trust
    # set to the Job so runner-side verification uses the same keys as the API
    # (set at Job-creation from config, not fetched at request time → not an
    # attacker-controllable trust anchor). Empty (default) → runner uses bundled.
    for _tool, _armor in settings.registry.binary_cache.signing_keys.items():
        if _armor:
            container_env.append({"name": f"TP_SIGNING_KEY_{_tool.upper()}", "value": _armor})
    # Terragrunt (#534): the runner wraps tofu/terraform with terragrunt when
    # enabled. Version is partial (e.g. "1.0") — the binary cache resolves it.
    if terragrunt_enabled:
        container_env.append({"name": "TP_TERRAGRUNT_ENABLED", "value": "true"})
        container_env.append(
            {"name": "TP_TERRAGRUNT_VERSION", "value": terragrunt_version or "1.0"}
        )
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
    if is_destroy:
        container_env.append({"name": "TP_DESTROY", "value": "true"})
    if working_directory:
        container_env.append({"name": "TP_WORKING_DIR", "value": working_directory})

    # Termination grace period — passed to entrypoint for time-budgeted shutdown
    container_env.append(
        {
            "name": "TP_TERMINATION_GRACE",
            "value": str(runner_config.termination_grace_period_seconds),
        }
    )

    # Workspace env vars (category=env). Values are sourced from the per-run
    # vars Secret via secretKeyRef — never plaintext in the Job spec, since env
    # vars can be sensitive and are readable via `kubectl describe` / etcd. The
    # listener populates the Secret (key = the env var name).
    for var in env_vars:
        if vars_secret_name:
            container_env.append(
                {
                    "name": var["key"],
                    "valueFrom": {"secretKeyRef": {"name": vars_secret_name, "key": var["key"]}},
                }
            )
        else:
            container_env.append({"name": var["key"], "value": var["value"]})

    # Terraform vars (category=terraform) are NOT injected as env vars. They are
    # delivered as a read-only mounted file from the same per-run Secret (volume
    # added below) and the entrypoint renders terrapod.auto.tfvars from it. This
    # keeps sensitive values off the Job spec env and makes complex/object
    # values round-trip identically on terraform and tofu.

    # Forward proxy (#592) — lets `terraform init` reach PUBLIC registry/git
    # module sources through a corporate proxy. Both upper and lower case: Go
    # (terraform/tofu, the AWS SDK) reads the UPPER form, while many libraries
    # read the lower form. no_proxy is pre-resolved by the chart; we do NOT
    # force the API host into it — in split-cluster the runner→API hop reaches
    # the public URL and legitimately traverses the proxy.
    if runner_config.proxy:
        p = runner_config.proxy
        for name, value in (
            ("HTTP_PROXY", p.http_proxy),
            ("HTTPS_PROXY", p.https_proxy),
            ("NO_PROXY", p.no_proxy),
        ):
            if value:
                container_env.append({"name": name, "value": value})
                container_env.append({"name": name.lower(), "value": value})

    # Custom CA trust bundle (#592) — point the runner's TLS stacks at the
    # init-container-merged bundle (system roots + custom CA). Volumes + init
    # container are added to the pod spec further down.
    if ca_secret_name and runner_config.ca_bundle_enabled:
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            container_env.append({"name": name, "value": _CA_MERGED_FILE})

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
            # PodFailurePolicy differs by phase:
            #   plan  — K8s-native retry on disruption (eviction, preemption,
            #           node drain). Plan never mutates Terrapod state or
            #           live AWS infra, so partial execution is safe to
            #           retry.
            #   apply — never retry on disruption. Once the container ran,
            #           terraform may have mutated state. K8s PodFailurePolicy
            #           can't reliably distinguish "container started" from
            #           "container never ran" — kubelet behaviour differs
            #           across cluster versions/distributions for pre-start
            #           disruption — so apply is conservatively never-retry.
            #           Operator decides whether to re-run from the Run UI.
            #
            # Order matters: PodFailurePolicy evaluates rules in order, first
            # match wins. The Ignore rule for plan MUST precede the FailJob
            # rule so that eviction (which produces exit 137 via SIGKILL on
            # an already-running container) is ignored before the FailJob
            # rule catches it.
            "podFailurePolicy": {
                "rules": (
                    [
                        {
                            # Plan-only: ignore disruption-triggered failures
                            # so K8s retries via backoffLimit.
                            "action": "Ignore",
                            "onPodConditions": [
                                {"type": "DisruptionTarget", "status": "True"},
                            ],
                        },
                    ]
                    if phase == "plan"
                    else []
                )
                + [
                    {
                        # Any non-zero exit fails the Job (applies to both
                        # plan and apply). For plan, a disruption-driven
                        # SIGKILL would have matched the Ignore rule above
                        # and skipped this; what remains here is a real
                        # tofu error code (or other non-disruption kill).
                        "action": "FailJob",
                        "onExitCodes": {
                            "containerName": "runner",
                            "operator": "NotIn",
                            "values": [0],
                        },
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

    # Per-run vars Secret: mount the terraform variables as a read-only file.
    # The listener creates the Secret with an ownerReference to this Job, so it
    # cascade-deletes when the Job is GC'd (same lifecycle as the auth Secret).
    if vars_secret_name and terraform_vars:
        pod = job_spec["spec"]["template"]["spec"]
        pod["volumes"].append(
            {
                "name": "tfvars",
                "secret": {
                    "secretName": vars_secret_name,
                    "items": [{"key": _TFVARS_SECRET_KEY, "path": _TFVARS_FILENAME}],
                },
            }
        )
        pod["containers"][0]["volumeMounts"].append(
            {"name": "tfvars", "mountPath": _TFVARS_MOUNT_DIR, "readOnly": True}
        )

    # Custom CA trust bundle (#592): mount the per-run CA Secret (raw custom CA,
    # shipped by the listener from its own mounted source) and an emptyDir for
    # the merged bundle, plus an init container that concatenates the runner
    # image's system roots with the custom CA. Runner containers run with
    # readOnlyRootFilesystem, so the merge MUST land on the writable emptyDir,
    # not the image's /etc/ssl. Mirrors terrapod.caBundle.initContainer in
    # _helpers.tpl so deployment and Job behave identically.
    if ca_secret_name and runner_config.ca_bundle_enabled:
        pod = job_spec["spec"]["template"]["spec"]
        pod["volumes"].append(
            {
                "name": "ca-src",
                "secret": {
                    "secretName": ca_secret_name,
                    "items": [{"key": _CA_BUNDLE_KEY, "path": _CA_BUNDLE_KEY}],
                },
            }
        )
        pod["volumes"].append({"name": "ca-merged", "emptyDir": {}})
        pod["containers"][0]["volumeMounts"].append(
            {"name": "ca-merged", "mountPath": _CA_MERGED_DIR, "readOnly": True}
        )
        pod.setdefault("initContainers", []).append(
            {
                "name": "ca-merge",
                "image": image,
                "imagePullPolicy": runner_config.image.pull_policy,
                "command": [
                    "/bin/sh",
                    "-c",
                    f"cat /etc/ssl/certs/ca-certificates.crt {_CA_SRC_DIR}/{_CA_BUNDLE_KEY} "
                    f"> {_CA_MERGED_FILE}",
                ],
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
                    {"name": "ca-src", "mountPath": _CA_SRC_DIR, "readOnly": True},
                    {"name": "ca-merged", "mountPath": _CA_MERGED_DIR},
                ],
            }
        )

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
