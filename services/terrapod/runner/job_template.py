"""Build K8s Job specs for terraform/tofu plan and apply phases."""

import re

from terrapod.config import RunnerConfig
from terrapod.logging_config import get_logger
from terrapod.runner.phases.cloud_identity import TOKEN_DIR as OIDC_TOKEN_DIR
from terrapod.runner.reserved_env import is_reserved_env_key

logger = get_logger(__name__)

# Per-run vars Secret (mounted files). Keep in sync with
# runner/job_entrypoint.py + runner/phases/execution_hooks.py which read the
# same paths.
_TFVARS_SECRET_KEY = "terraform.tfvars.json"
_TFVARS_FILENAME = "terraform.tfvars.json"
_TFVARS_MOUNT_DIR = "/var/run/terrapod/vars"
# Execution hooks (#619) — shipped as an extra key in the same per-run vars
# Secret, mounted alongside the tfvars file.
_HOOKS_SECRET_KEY = "execution-hooks.json"
_HOOKS_FILENAME = "execution-hooks.json"
# Private-git-module auth (#1028) — another key in the same per-run vars Secret,
# mounted alongside the tfvars file; read by runner/phases/git_auth.py.
_GIT_AUTH_SECRET_KEY = "git-auth.json"
_GIT_AUTH_FILENAME = "git-auth.json"
# Ansible credentials (#1933). Same channel, own key: these are mounted only
# into a configure's Job, because the API sends the attribute only for that
# phase, and a plan executes a pull request's code.
_ANSIBLE_AUTH_SECRET_KEY = "ansible-auth.json"
_ANSIBLE_AUTH_FILENAME = "ansible-auth.json"
# Vault file delivery (#1619) — more keys of the same per-run vars Secret, each
# mounted read-only at its own path. See runner/vault_files.py.
_VAULT_FILES_VOLUME = "vault-files"
_VAULT_HOME_FILES_VOLUME = "vault-home-files"

# Custom outbound CA trust bundle (#592). The listener ships the raw custom CA
# into a per-run Secret under this key; an init container merges it with the
# runner image's own system roots into an emptyDir, and the runner container
# trusts the merged file. Paths/key mirror the chart's _helpers.tpl so the
# deployment and Job machinery stay identical.
_CA_BUNDLE_KEY = "ca-extra.pem"
_CA_SRC_DIR = "/etc/terrapod-ca-src"
_CA_MERGED_DIR = "/etc/terrapod-ca"
_CA_MERGED_FILE = f"{_CA_MERGED_DIR}/ca-bundle.crt"

# Ansible execution environment (#2154). The EE contributes its FILESYSTEM, not
# its entrypoint: we want ansible-core, its collections and the system libraries
# they link against at a path, and we do not want to program against an
# arbitrary image's shell, CMD or user setup. So an init container copies the
# static agent out of the runner image onto a shared volume and the sidecar is
# started with a `command:` override, which Kubernetes applies over the image
# ENTRYPOINT. That is what keeps an EE insensitive to Terrapod code changes --
# it carries none of ours.
_EE_CONTAINER = "ee"
_EE_AGENT_COPY_CONTAINER = "agent-copy"
#: Where the agent lives in the runner image, and where it is copied to.
_EE_AGENT_SRC = "/usr/local/bin/terrapod-ee-agent"
_EE_SHARED_VOLUME = "tp"
_EE_SHARED_DIR = "/tp"
_EE_AGENT_PATH = f"{_EE_SHARED_DIR}/terrapod-ee-agent"
#: Loopback, and that is the whole security model: containers in a pod share a
#: network namespace but not a filesystem, so the orchestrator reaches the agent
#: at 127.0.0.1 while nothing outside the pod can. It also decides the probe
#: form -- see `_ee_startup_probe`.
_EE_LISTEN = "127.0.0.1:7781"
_EE_ENDPOINT = f"http://{_EE_LISTEN}"
#: Everything the EE writes lives under the shared volume, which is what lets it
#: keep `readOnlyRootFilesystem` and lets a custom EE meet the contract without
#: providing any particular writable path of its own.
_EE_TMPDIR = f"{_EE_SHARED_DIR}/tmp"
_EE_COLLECTIONS_DIR = f"{_EE_SHARED_DIR}/collections"
_EE_ROLES_DIR = f"{_EE_SHARED_DIR}/roles"
_EE_HOME = f"{_EE_SHARED_DIR}/home"
#: Where the orchestrator writes the run's resolved inventory for the EE to
#: read (#1972). It MUST be on the shared volume: ansible runs in the EE, which
#: mounts only this volume and the workspace, so an inventory written to the
#: orchestrator's own `/tmp` is invisible to the process that has to read it.
#: Here rather than on the workspace volume because the file is Terrapod's, not
#: part of the operator's fetched tree.
_EE_INVENTORY_DIR = f"{_EE_SHARED_DIR}/inventory"
#: Where the ansible_auth phase writes `0600` credential files (#1933). It has
#: to be on the SHARED volume: ansible runs in the EE container, which mounts
#: only this volume and `workspace`, so a key written to the orchestrator's own
#: `/tmp` -- where the PVC-tempfile rule otherwise sends runner tempfiles -- is
#: materialised correctly and invisible to the one process that must read it.
_EE_CREDENTIALS_DIR = f"{_EE_SHARED_DIR}/credentials"
#: The group both containers reach the shared volumes through. The orchestrator
#: runs 1000:1000; our own EE runs 1000:0; an operator's EE runs whatever its
#: image says. `fsGroup` is what makes all three work: kubelet chowns every
#: volume to this gid AND adds it to the supplementary groups of every container
#: in the pod, so volume access does not depend on the EE's uid at all. That is
#: why we must NOT pin `runAsUser` on the EE -- see `_ee_security_context`.
_EE_FS_GROUP = 1000
#: The builder's own fallback when a run carries no EE size -- a run created
#: before the columns existed, or by an API up to N-2 minors behind. It is a
#: SEPARATE constant from `Settings.default_ee_resource_*` on purpose: the
#: listener does not mount the API's ConfigMap, so reading the API's setting
#: here would silently yield the code default rather than the operator's value.
#: `test_the_builder_default_matches_the_deployment_default` pins the two equal.
_EE_DEFAULT_CPU = "500m"
_EE_DEFAULT_MEMORY = "1Gi"
#: The env key the agent reads its shared token from (ee-agent/agent.go).
_EE_TOKEN_ENV = "TP_EE_TOKEN"
#: The key of the per-run auth Secret holding that token (listener writes it).
_EE_TOKEN_SECRET_KEY = "ee-token"


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


def _vault_file_mode(runner_config: RunnerConfig, fs_group: int | None = None) -> int:
    """0440 when the pod has an fsGroup (the file's group), else 0444.

    Kubelet projects Secret files owned by root. The runner is a non-root UID,
    so without an fsGroup it can only read a world-readable file; with one, the
    group bit suffices and "other" gets nothing.

    `fs_group` is the one the EE path sets (#2154), which does not come from
    `pod_security_context` -- so without this argument a configure run would
    HAVE an fsGroup and still project world-readable files, which is the
    looser of the two and exactly the direction worth not getting wrong.
    """
    if fs_group is not None:
        return 0o440
    psc = runner_config.pod_security_context
    if isinstance(psc, dict) and psc.get("fsGroup") is not None:
        return 0o440
    return 0o444


def _add_vault_file_mounts(
    pod: dict,
    vault_files: list[dict],
    *,
    vars_secret_name: str,
    runner_config: RunnerConfig,
    image: str,
    fs_group: int | None = None,
) -> None:
    """Mount Vault-delivered files (#1619) from the per-run vars Secret.

    ``vault_files`` is ``[{name, secret_key}]`` — names and Secret keys only.
    This function reads nothing else from an entry, so no value can reach the
    Job spec through it: a value exists only as a key of the Secret.

    - relative names → one Secret volume, mounted read-only at
      ``/var/run/terrapod/files``, with an item per file;
    - ``~/`` names → a second volume of the same Secret, each key mounted
      read-only at its path under HOME via ``subPath``. HOME is an emptyDir, so
      the runtime can create the mount point despite readOnlyRootFilesystem; an
      init container first creates the parent directories as the runner's UID,
      because the runtime would otherwise create them root-owned and leave the
      runner unable to write beside its own file (``~/.aws/cli/cache``).
    """
    from terrapod.runner import vault_files as vf

    mode = _vault_file_mode(runner_config, fs_group)
    container = pod["containers"][0]
    rel = [f for f in vault_files if not vf.is_home(f["name"])]
    home = [f for f in vault_files if vf.is_home(f["name"])]

    if rel:
        pod["volumes"].append(
            {
                "name": _VAULT_FILES_VOLUME,
                "secret": {
                    "secretName": vars_secret_name,
                    "items": [{"key": f["secret_key"], "path": f["name"]} for f in rel],
                    "defaultMode": mode,
                },
            }
        )
        container["volumeMounts"].append(
            {"name": _VAULT_FILES_VOLUME, "mountPath": vf.FILES_DIR, "readOnly": True}
        )

    if home:
        pod["volumes"].append(
            {
                "name": _VAULT_HOME_FILES_VOLUME,
                "secret": {
                    "secretName": vars_secret_name,
                    "items": [{"key": f["secret_key"], "path": f["secret_key"]} for f in home],
                    "defaultMode": mode,
                },
            }
        )
        for f in home:
            container["volumeMounts"].append(
                {
                    "name": _VAULT_HOME_FILES_VOLUME,
                    "mountPath": vf.target_path(f["name"]),
                    "subPath": f["secret_key"],
                    "readOnly": True,
                }
            )
        parents = vf.home_parent_dirs([f["name"] for f in home])
        if parents:
            pod.setdefault("initContainers", []).append(
                {
                    "name": "home-dirs",
                    "image": image,
                    "imagePullPolicy": runner_config.image.pull_policy,
                    # Exec form, no shell: names are validated to
                    # [A-Za-z0-9._-] segments, but nothing here depends on that.
                    "command": ["mkdir", "-p", *parents],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumeMounts": [{"name": "home", "mountPath": vf.RUNNER_HOME}],
                }
            )


def _ee_security_context() -> dict:
    """The EE sidecar's securityContext -- and what it deliberately omits.

    No `runAsUser` and no `runAsGroup`. The contract an EE must meet is "a uid
    able to read the shared volume", and `fsGroup` is what satisfies it for any
    uid, so pinning 1000 here would buy nothing and would break an operator's
    image whose files belong to a different user. `runAsNonRoot` stays, because
    we do require that much: an image whose USER is root fails to start with
    kubelet saying so, which is a legible refusal rather than a silent
    privilege.

    `readOnlyRootFilesystem` stays too. Everything ansible writes is redirected
    under the shared volume by `_ee_env`, so an EE needs no writable path of its
    own -- which is the property that keeps the contract to four binaries on
    PATH.
    """
    return {
        "runAsNonRoot": True,
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _ee_startup_probe() -> dict:
    """`exec`, NOT `httpGet` -- and this is not a style choice.

    The agent binds loopback only. A kubelet `httpGet` probe dials the POD's
    IP from the node, which is a different network namespace from the one
    `127.0.0.1:7781` is bound in, so an httpGet probe can never succeed however
    healthy the agent is. #1925's sketch shows `httpGet`; it is wrong, and the
    failure it produces is the worst kind -- a sidecar that works perfectly and
    a pod that never leaves `PodInitializing`.

    `healthcheck` dials loopback from inside the container and asserts the
    contract (ansible-playbook, ansible-galaxy, ssh and git on PATH) while it is
    there. So a non-conforming operator image becomes a startup failure naming
    what is missing, rather than a mid-run mystery.

    `failureThreshold` x `periodSeconds` is the window an image has to become
    ready; past it kubelet restarts the sidecar and the run stalls, which
    `run_reconciler` is what notices (#2154 item 13).
    """
    return {
        "exec": {"command": [_EE_AGENT_PATH, "healthcheck", "--listen", _EE_LISTEN]},
        "periodSeconds": 5,
        "timeoutSeconds": 5,
        "failureThreshold": 24,
    }


def _ee_env(auth_secret_name: str) -> list[dict]:
    """The sidecar's environment: where to write, and the shared token.

    Every writable path points into the shared volume, which is what lets the
    EE keep a read-only root filesystem and lets a custom EE provide no
    particular directory of its own.

    `ANSIBLE_REMOTE_TEMP` is included although it names a path on the TARGET
    host, not here: ansible's default is `~/.ansible/tmp` under the remote
    user, and a target whose home is unwritable fails late and obscurely. It
    costs nothing to be explicit.
    """
    return [
        {"name": "HOME", "value": _EE_HOME},
        {"name": "TMPDIR", "value": _EE_TMPDIR},
        {"name": "ANSIBLE_LOCAL_TEMP", "value": f"{_EE_TMPDIR}/ansible-local"},
        {"name": "ANSIBLE_REMOTE_TEMP", "value": "~/.ansible/tmp"},
        {"name": "ANSIBLE_COLLECTIONS_PATH", "value": _EE_COLLECTIONS_DIR},
        {"name": "ANSIBLE_ROLES_PATH", "value": _EE_ROLES_DIR},
        {
            "name": _EE_TOKEN_ENV,
            "valueFrom": {"secretKeyRef": {"name": auth_secret_name, "key": _EE_TOKEN_SECRET_KEY}},
        },
    ]


def _add_execution_environment(
    pod: dict,
    *,
    ee_image: str,
    auth_secret_name: str,
    resource_cpu: str,
    resource_memory: str,
    runner_config: RunnerConfig,
    runner_image: str,
    ca_mounted: bool,
) -> None:
    """Add the Ansible execution environment as a NATIVE SIDECAR (#2154, #1925).

    Called LAST on purpose, and moving it breaks the pod. `initContainers` run
    in order, and a native sidecar -- an init container with
    `restartPolicy: Always` -- holds every later one until its `startupProbe`
    passes, with the main container starting after all of them. So the EE must
    be the final entry: appended after `ca-merge` (which populates the bundle
    the EE trusts) and after `home-dirs`, and with `agent-copy` immediately
    before it.

    What this shape buys, and why it is a sidecar rather than a container:

    * **Ordering for free.** The orchestrator does not start until the probe
      passes, so there is no retry loop and no sentinel-file handshake.
    * **The Job still completes.** A native sidecar is terminated by Kubernetes
      when the main container exits; a plain `containers` entry that never exits
      would hang the Job for ever.

    `shareProcessNamespace` is deliberately NOT set. #1925 records it as a
    backstop only -- the agent is the ansible process's parent and signals it
    directly, so cancellation is an endpoint rather than a kill -- and it has a
    cost of its own: both containers run as uid 1000, so with a shared PID
    namespace an operator-supplied EE could read `/proc/<pid>/environ` of the
    orchestrator and lift that run's `TP_AUTH_TOKEN`.
    """
    limit_cpu = _double_resource(resource_cpu)
    limit_memory = _double_resource(resource_memory)

    # One volume, several directories under it. A single shared mount is what
    # #1925 specifies (`cp … /tp/`), and it keeps the contract to one path
    # rather than one per concern.
    pod["volumes"].append({"name": _EE_SHARED_VOLUME, "emptyDir": {}})

    # Both containers reach the playbook directory and the resolved inventory
    # (#2021) through the workspace volume the orchestrator already fetches
    # into, so the inventory handoff needs no volume of its own.
    ee_mounts = [
        {"name": _EE_SHARED_VOLUME, "mountPath": _EE_SHARED_DIR},
        {"name": "workspace", "mountPath": "/workspace"},
    ]
    if ca_mounted:
        ee_mounts.append({"name": "ca-merged", "mountPath": _CA_MERGED_DIR, "readOnly": True})

    # The orchestrator sees the same shared volume: it is how it reads what the
    # agent installed, and how the agent binary gets there in the first place.
    pod["containers"][0]["volumeMounts"].append(
        {"name": _EE_SHARED_VOLUME, "mountPath": _EE_SHARED_DIR}
    )
    # Appended AFTER the operator's `extraEnv`, deliberately: Kubernetes gives
    # the last duplicate precedence, so the agent's address and token cannot be
    # redirected by a workspace variable or an operator env entry.
    pod["containers"][0]["env"].extend(
        [
            {"name": "TP_EE_ENDPOINT", "value": _EE_ENDPOINT},
            {"name": "TP_EE_IMAGE", "value": ee_image},
            {"name": "TP_EE_COLLECTIONS_DIR", "value": _EE_COLLECTIONS_DIR},
            {"name": "TP_EE_ROLES_DIR", "value": _EE_ROLES_DIR},
            {"name": "TP_EE_INVENTORY_DIR", "value": _EE_INVENTORY_DIR},
            {"name": "TP_EE_CREDENTIALS_DIR", "value": _EE_CREDENTIALS_DIR},
            {
                "name": _EE_TOKEN_ENV,
                "valueFrom": {
                    "secretKeyRef": {"name": auth_secret_name, "key": _EE_TOKEN_SECRET_KEY}
                },
            },
        ]
    )

    inits = pod.setdefault("initContainers", [])
    inits.append(
        {
            "name": _EE_AGENT_COPY_CONTAINER,
            "image": runner_image,
            "imagePullPolicy": runner_config.image.pull_policy,
            # The runner image, so the directories are created by the uid that
            # will share them, and the binary arrives with its exec bit.
            # `install -D` would create the parents too, but `cp` plus an
            # explicit mkdir says what it does and needs no shell.
            "command": [
                "/bin/sh",
                "-c",
                f"mkdir -p {_EE_TMPDIR} {_EE_COLLECTIONS_DIR} {_EE_ROLES_DIR} {_EE_HOME} "
                f"{_EE_INVENTORY_DIR} {_EE_CREDENTIALS_DIR} "
                f"&& cp {_EE_AGENT_SRC} {_EE_AGENT_PATH}",
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
            "volumeMounts": [{"name": _EE_SHARED_VOLUME, "mountPath": _EE_SHARED_DIR}],
        }
    )
    inits.append(
        {
            "name": _EE_CONTAINER,
            "image": ee_image,
            # A built-in EE is pulled through the cache; an operator's URI is
            # used exactly as given, because it is their own registry reference
            # and frequently already internal. Either way `imagePullSecrets` is
            # pod-level, so it covers this container with no extra wiring.
            "imagePullPolicy": runner_config.image.pull_policy,
            # NATIVE SIDECAR. This single line is the difference between a Job
            # that completes and one that hangs for ever.
            "restartPolicy": "Always",
            "command": [_EE_AGENT_PATH, "serve", "--listen", _EE_LISTEN],
            "env": _ee_env(auth_secret_name),
            "startupProbe": _ee_startup_probe(),
            "resources": {
                "requests": {"cpu": resource_cpu, "memory": resource_memory},
                "limits": {"cpu": limit_cpu, "memory": limit_memory},
            },
            "securityContext": _ee_security_context(),
            "volumeMounts": ee_mounts,
        }
    )

    # fsGroup is the uid alignment, and it is merged UNDER the operator's own
    # pod securityContext rather than over it: they may have set a different
    # fsGroup for their storage, and theirs is the deployment's business. We
    # only supply one when they have not.
    sec = pod.setdefault("securityContext", {})
    sec.setdefault("fsGroup", _EE_FS_GROUP)


def build_job_spec(
    run_id: str,
    phase: str,  # "plan", "apply" or "configure"
    runner_config: RunnerConfig,
    auth_secret_name: str,
    env_vars: list[dict[str, str]],
    terraform_vars: list[dict[str, str]],
    execution_hooks: list[dict] | None = None,
    git_auth: list[dict] | None = None,
    ansible_auth: list[dict] | None = None,
    vault_files: list[dict] | None = None,
    vars_secret_name: str = "",
    workspace_id: str = "",
    resource_cpu: str = "1",
    resource_memory: str = "2Gi",
    timeout_minutes: int = 60,
    namespace: str = "",
    ca_secret_name: str = "",
    #: The engine's TP_* instructions, built by its strategy (#1407 phase 2).
    #: Defaults to empty so a caller that supplies none renders a spec with no
    #: engine env rather than failing — the neutral half stands on its own.
    engine_env: list[dict] | None = None,
    #: How long a FAILED pod is held open for inspection (#1764). 0 -- the
    #: default -- is the ordinary behaviour: the container exits as it always
    #: has. Resolved by the listener from the deployment's configured window,
    #: so a workspace asks for a debug pod and the operator decides how long
    #: one may survive.
    debug_linger_seconds: int = 0,
    #: The Ansible execution environment for a configure run (#2154). EMPTY --
    #: the default -- renders exactly the spec this builder always rendered: no
    #: init container, no sidecar, no shared volume, no fsGroup, no EE env. So a
    #: terraform/tofu-only deployment pays nothing for Ansible support and never
    #: pulls an EE image, which is the property #1925's withdrawn engine gate
    #: was protecting by another mechanism.
    ee_image: str = "",
    #: The EE sidecar's own requests, limits computed at 2x as for the
    #: orchestrator's. Empty falls back to this module's own defaults, which is
    #: the case for a run created by an API that predates the columns.
    ee_resource_cpu: str = "",
    ee_resource_memory: str = "",
) -> dict:
    """Build a K8s Job spec for a run phase.

    Args:
        run_id: The run UUID.
        workspace_id: The run's own workspace, exported to the engine as
            TERRAPOD_WORKSPACE so the Terrapod provider defaults to it (#2036).
            Empty from a listener that predates it.
        phase: "plan", "apply" or "configure".
        runner_config: Global runner config (image, defaults, etc.).
        auth_secret_name: K8s Secret name containing the runner token.
        vars_secret_name: K8s Secret holding all workspace variable values
            (terraform tfvars blob + env vars). When set, env vars are sourced
            via secretKeyRef and terraform vars are mounted as a file — no
            variable value is plaintext in the Job spec.
        env_vars: Workspace env vars [{key, value}] — keys referenced via
            secretKeyRef into vars_secret_name.
        terraform_vars: The engine's own parameter channel (#1898),
            [{key, value, structured, sensitive}] — presence triggers the
            mounted tfvars volume. One list for every engine, because they are
            one role with three deliveries; the entrypoint dispatches on the
            run's engine (a tfvars file for Terraform, `pulumi config set` for
            Pulumi). The name keeps its Terraform spelling because a runner up
            to N-2 minors behind reads this exact key.
        resource_cpu: CPU request (e.g. "1", "500m").
        parallelism: How many operations the engine runs at once (#1431).
        resource_memory: Memory request (e.g. "2Gi", "256Mi").
        timeout_minutes: Job timeout in minutes.
        engine_version: Engine version to use (terraform/tofu, or pulumi).
        execution_backend: Execution backend (terraform or tofu).
        namespace: Target namespace for the Job.
    """
    if not namespace:
        namespace = runner_config.runner_namespace

    run_short = run_id[:16]
    job_name = f"tprun-{run_short}-{phase}"

    # Build container env vars
    api_url = runner_config.server_url or "http://terrapod-api:8000"
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
        # The run's own workspace, so `provider_credentials` can export
        # TERRAPOD_WORKSPACE and the Terrapod provider defaults to it (#2036).
        # Safe in the Job spec, unlike the hostname and token: `TP_` is reserved
        # (reserved_env), so a workspace variable cannot shadow it. Empty when a
        # listener older than #2036 did not pass it, which the export tolerates.
        {"name": "TP_WORKSPACE_ID", "value": workspace_id},
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
    public_api_url = (runner_config.public_api_url or "").strip()
    if public_api_url and public_api_url.rstrip("/") != api_url.rstrip("/"):
        container_env.append({"name": "TP_PUBLIC_API_URL", "value": public_api_url})

    # The engine's own instructions to its entrypoint (#1407 phase 2). Spliced in
    # at exactly the position these lines occupied when they lived here, so the
    # rendered env order is unchanged — the golden spec matrix pins that.
    #
    # This builder knows nothing about which engine it is serving; the strategy
    # composed this list and passes it in.
    container_env.extend(engine_env or [])

    # Termination grace period — passed to entrypoint for time-budgeted shutdown
    container_env.append(
        {
            "name": "TP_TERMINATION_GRACE",
            "value": str(runner_config.termination_grace_period_seconds),
        }
    )

    # Debug mode (#1764). Platform plumbing, not an engine instruction: it
    # governs what the orchestrator does after the run has been reported,
    # whichever engine produced it, so it sits here rather than in engine_env.
    if debug_linger_seconds > 0:
        container_env.append(
            {"name": "TP_DEBUG_LINGER_SECONDS", "value": str(debug_linger_seconds)}
        )

    # Workspace env vars (category=env). Values are sourced from the per-run
    # vars Secret via secretKeyRef — never plaintext in the Job spec, since env
    # vars can be sensitive and are readable via `kubectl describe` / etcd. The
    # listener populates the Secret (key = the env var name).
    for var in env_vars:
        # Platform plumbing is never shadowable. Kubernetes gives the LAST
        # duplicate name precedence, and the workspace block is appended after
        # the platform block, so a workspace variable named TP_API_URL or
        # TP_AUTH_TOKEN would win -- redirecting the runner at a host of the
        # caller's choosing, with the run's own token. Dropping the key here
        # rather than reordering the blocks fixes it for variables already
        # stored, and covers the TP_* keys the platform emits only
        # conditionally (TP_DESTROY, TP_PLAN_ONLY, TP_VAR_FILES and the rest),
        # which have no collision for precedence to resolve.
        if is_reserved_env_key(var["key"]):
            logger.warning(
                "ignoring workspace variable that collides with platform plumbing",
                key=var["key"],
            )
            continue
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

    # The phase's ServiceAccount, resolved HERE -- before the Job spec literal
    # below -- because `automountServiceAccountToken` inside it is computed from
    # whether an account is set. Resolving after the literal would leave the
    # mount decision made from the base account while the pod ran as another
    # (#2189). A positional test pins the ordering, the same shape as the
    # flush-inside-try gate.
    #
    # The apply account is optional and absent by default, so every phase falls
    # back to the base one and the rendered spec is byte-identical.
    effective_service_account = runner_config.service_account_name
    if phase == "apply" and runner_config.apply_service_account_name:
        effective_service_account = runner_config.apply_service_account_name

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
            #   apply, configure — never retry on disruption. Once the
            #           container ran, terraform may have mutated state and a
            #           playbook may have changed a host. K8s PodFailurePolicy
            #           can't reliably distinguish "container started" from
            #           "container never ran" — kubelet behaviour differs
            #           across cluster versions/distributions for pre-start
            #           disruption — so both are conservatively never-retry.
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
            # The run's own timeout, plus the debug window when one is in
            # force (#1764). This is what actually bounds a lingering pod: the
            # orchestrator sleeps for at most `debug_linger_seconds`, and if it
            # misbehaves the cluster ends the pod here regardless. A pod holds
            # the run's auth token and its decrypted tfvars, so the ceiling is
            # a safety property rather than a convenience.
            "activeDeadlineSeconds": timeout_minutes * 60 + max(0, debug_linger_seconds),
            "ttlSecondsAfterFinished": (
                max(runner_config.ttl_seconds_after_finished, debug_linger_seconds)
                if debug_linger_seconds > 0
                else runner_config.ttl_seconds_after_finished
            ),
            "template": {
                "metadata": {
                    "labels": pod_labels,
                },
                "spec": {
                    "terminationGracePeriodSeconds": runner_config.termination_grace_period_seconds,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": bool(effective_service_account),
                    "volumes": [
                        {"name": "workspace", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                        # $HOME. The image sets HOME=/home/runner and describes it
                        # as writable, but `readOnlyRootFilesystem` below makes it
                        # read-only unless something is mounted there — so every
                        # write to $HOME failed EROFS (#1442). That silently broke
                        # private-git-module auth, whose config lands under $HOME,
                        # and equally affects anything else consulting it: helm's
                        # repository cache, kubectl's, the AWS CLI's config.
                        {"name": "home", "emptyDir": {}},
                        # The cloud identity tokens the credential phase writes
                        # (#1901). Needed for exactly the #1442 reason three
                        # lines above: `readOnlyRootFilesystem` below makes
                        # /var/run/terrapod read-only, and the sibling paths
                        # there (vars, files) are read-only Secret mounts, so
                        # without this the phase's first `mkdir` fails EROFS —
                        # which it reports as CloudIdentityUnavailable, failing
                        # every federated run.
                        #
                        # `medium: Memory` because these are bearer credentials
                        # for the workspace's cloud identity: a tmpfs keeps them
                        # off the node's disk, and they live only as long as the
                        # Job.
                        {"name": "oidc", "emptyDir": {"medium": "Memory"}},
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
                                {"name": "home", "mountPath": "/home/runner"},
                                {"name": "oidc", "mountPath": str(OIDC_TOKEN_DIR)},
                            ],
                        }
                    ],
                },
            },
        },
    }

    # Per-run vars Secret: mount the terraform variables (and execution-hook
    # scripts, #619) as read-only files. The listener creates the Secret with an
    # ownerReference to this Job, so it cascade-deletes when the Job is GC'd
    # (same lifecycle as the auth Secret).
    secret_items = []
    if terraform_vars:
        secret_items.append({"key": _TFVARS_SECRET_KEY, "path": _TFVARS_FILENAME})
    if execution_hooks:
        secret_items.append({"key": _HOOKS_SECRET_KEY, "path": _HOOKS_FILENAME})
    if git_auth:
        secret_items.append({"key": _GIT_AUTH_SECRET_KEY, "path": _GIT_AUTH_FILENAME})
    if ansible_auth:
        secret_items.append({"key": _ANSIBLE_AUTH_SECRET_KEY, "path": _ANSIBLE_AUTH_FILENAME})
    if vars_secret_name and secret_items:
        pod = job_spec["spec"]["template"]["spec"]
        pod["volumes"].append(
            {
                "name": "tfvars",
                "secret": {
                    "secretName": vars_secret_name,
                    "items": secret_items,
                },
            }
        )
        pod["containers"][0]["volumeMounts"].append(
            {"name": "tfvars", "mountPath": _TFVARS_MOUNT_DIR, "readOnly": True}
        )

    if vars_secret_name and vault_files:
        _add_vault_file_mounts(
            job_spec["spec"]["template"]["spec"],
            vault_files,
            vars_secret_name=vars_secret_name,
            runner_config=runner_config,
            image=image,
            # Known here rather than read back off the pod, because the EE block
            # that sets it runs last -- and the Secret's file mode has to be
            # decided when the volume is projected, not afterwards.
            fs_group=_EE_FS_GROUP if ee_image else None,
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

    # Service account (CSP identity) — from global runner config (Helm values),
    # with the apply phase optionally taking its own (#2189). Resolved above the
    # spec literal; see `effective_service_account`.
    if effective_service_account:
        job_spec["spec"]["template"]["spec"]["serviceAccountName"] = effective_service_account

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
    if runner_config.host_aliases:
        # Static /etc/hosts entries (#1459), for an internal name cluster DNS
        # does not serve.
        pod_spec["hostAliases"] = runner_config.host_aliases
    if runner_config.extra_volumes:
        # Appended, never assigned: the pod already carries the workspace,
        # vars and CA volumes built above, and replacing the list would drop
        # them and break every run.
        pod_spec.setdefault("volumes", []).extend(runner_config.extra_volumes)
    if runner_config.extra_volume_mounts:
        pod_spec["containers"][0].setdefault("volumeMounts", []).extend(
            runner_config.extra_volume_mounts
        )
    if runner_config.pod_annotations:
        pod_meta = job_spec["spec"]["template"]["metadata"]
        pod_meta["annotations"] = runner_config.pod_annotations

    # LAST, and `_add_execution_environment`'s docstring says why moving it
    # breaks the pod: the sidecar has to be the final `initContainers` entry,
    # and the fsGroup has to merge over the operator's `pod_security_context`
    # rather than be assigned before it.
    if ee_image:
        _add_execution_environment(
            pod_spec,
            ee_image=ee_image,
            auth_secret_name=auth_secret_name,
            resource_cpu=ee_resource_cpu or _EE_DEFAULT_CPU,
            resource_memory=ee_resource_memory or _EE_DEFAULT_MEMORY,
            runner_config=runner_config,
            runner_image=image,
            ca_mounted=bool(ca_secret_name and runner_config.ca_bundle_enabled),
        )

    return job_spec
