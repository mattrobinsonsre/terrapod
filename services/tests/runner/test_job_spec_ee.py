"""The Ansible execution environment's Job shape (#2154, #1925).

The sidecar is the one `build_job_spec` option that changes the POD's shape
rather than a container's env, so most of what matters here is structural and
positional: which init container comes last, which security context is absent,
which volume both containers share. A behavioural test cannot see any of it, and
Kubernetes accepts a spec that is wrong in every one of these ways -- it simply
runs something different, which is the failure mode the golden matrix exists for
and this file explains.

Proven on a real pod before any of this was written (k3s v1.35.5): the
orchestrator did not start until the sidecar's probe passed, the Job reached
`Complete` although the sidecar never exited, and the copied binary was visible
over the shared volume. These tests pin the spec that produced that.
"""

from __future__ import annotations

from terrapod.runner import job_template
from terrapod.runner.job_template import build_job_spec
from tests.runner.test_job_template import _runner_config

EE = "ghcr.io/example/terrapod-ee:2.21"


def _cfg():
    cfg = _runner_config()
    cfg.service_account_name = ""
    cfg.termination_grace_period_seconds = 120
    cfg.extra_env_from = []
    cfg.extra_env = []
    cfg.image_pull_secrets = []
    # The shared mock sets this False so no test gets phantom CA volumes. The
    # CA scenarios here need it ON, or `ca_secret_name` is inert and the test
    # would pass while asserting nothing -- which is exactly what the golden's
    # own `ca-bundle` scenario has been doing.
    cfg.ca_bundle_enabled = True
    return cfg


def _spec(**kwargs):
    base = {
        "run_id": "run-0123456789abcdef",
        "phase": "apply",
        "runner_config": _cfg(),
        "auth_secret_name": "tprun-auth",
        "env_vars": [],
        "terraform_vars": [],
        "namespace": "terrapod-runners",
    }
    base.update(kwargs)
    return build_job_spec(**base)


def _pod(**kwargs):
    return _spec(**kwargs)["spec"]["template"]["spec"]


def _init(pod, name):
    return next(c for c in pod.get("initContainers", []) if c["name"] == name)


class TestNoEEMeansNoChange:
    """A terraform/tofu-only deployment pays nothing, and that is a contract.

    Asserted here as well as by the golden's absence, because the golden says
    "these bytes" where this says "and that is WHY".
    """

    def test_an_empty_image_renders_no_sidecar_and_no_shared_volume(self):
        pod = _pod()
        assert "initContainers" not in pod
        assert [v["name"] for v in pod["volumes"]] == ["workspace", "tmp", "home", "oidc"]
        assert "securityContext" not in pod or "fsGroup" not in pod.get("securityContext", {})
        assert all(not e["name"].startswith("TP_EE_") for e in pod["containers"][0]["env"])

    def test_the_orchestrator_keeps_its_name(self):
        """`podFailurePolicy` names it, so renaming it changes what fails a Job.

        #1925's sketch calls the main container `orchestrator`. It is `runner`,
        and must stay: `onExitCodes.containerName` is matched by Kubernetes
        against the real name, so a rename would silently stop the exit-code
        rule applying to anything.
        """
        spec = _spec(ee_image=EE)
        assert [c["name"] for c in spec["spec"]["template"]["spec"]["containers"]] == ["runner"]
        rules = spec["spec"]["podFailurePolicy"]["rules"]
        named = [r["onExitCodes"]["containerName"] for r in rules if "onExitCodes" in r]
        assert named == ["runner"]


class TestTheSidecarShape:
    def test_the_ee_is_the_LAST_init_container(self):
        """Ordering is the whole mechanism, and it is positional.

        A native sidecar holds every later init container until its startup
        probe passes, and the main container starts after all of them. So the
        EE must come last, with `agent-copy` immediately before it -- that is
        what makes "the orchestrator does not start until the agent is ready"
        true with no retry loop and no sentinel file.
        """
        pod = _pod(ee_image=EE, ca_secret_name="tp-ca")
        pod["volumes"]  # noqa: B018 -- readability: the pod rendered at all
        names = [c["name"] for c in pod["initContainers"]]
        assert names[-2:] == ["agent-copy", "ee"]
        # And the CA merge precedes it, because the EE mounts the bundle it
        # populates.
        assert names.index("ca-merge") < names.index("ee")

    def test_the_ee_carries_restart_policy_always(self):
        """One line, and it is the difference between a Job that completes and
        one that hangs for ever: without it the sidecar is an ordinary init
        container that never exits."""
        assert _init(_pod(ee_image=EE), "ee")["restartPolicy"] == "Always"

    def test_the_probe_is_exec_and_NOT_httpGet(self):
        """A kubelet httpGet dials the POD's IP from the node.

        The agent binds loopback only, so an httpGet probe can never succeed
        however healthy it is -- a sidecar that works perfectly and a pod stuck
        in PodInitializing. #1925's sketch shows httpGet; it is wrong.
        """
        probe = _init(_pod(ee_image=EE), "ee")["startupProbe"]
        assert "httpGet" not in probe
        assert probe["exec"]["command"][:2] == ["/tp/terrapod-ee-agent", "healthcheck"]

    def test_the_command_overrides_the_image_entrypoint(self):
        """The EE contributes its filesystem, not its entrypoint.

        We want ansible-core and its collections at a path; we do not want to
        program against an arbitrary image's shell, CMD or user setup -- and a
        `command` is what keeps an operator's own EE insensitive to Terrapod
        code changes, because it carries none of ours.
        """
        ee = _init(_pod(ee_image=EE), "ee")
        assert ee["command"][0] == "/tp/terrapod-ee-agent"
        assert ee["command"][1] == "serve"

    def test_shareProcessNamespace_is_NOT_set(self):
        """#1925 records it as a backstop, and it has a cost of its own.

        Both containers run as uid 1000, so a shared PID namespace would let an
        operator-supplied EE read /proc/<pid>/environ of the orchestrator and
        lift that run's TP_AUTH_TOKEN. The agent parents the ansible process, so
        cancellation is an endpoint rather than a kill and this buys nothing.
        """
        assert "shareProcessNamespace" not in _pod(ee_image=EE)

    def test_the_ee_pins_the_shared_uid(self):
        """Pinned as of #1928, and this test is the inverse of what it was.

        It asserted that NO `runAsUser` was set, on the contract "a uid able to
        read the shared volume", which `fsGroup` satisfied for any uid. The cost
        was that every file the two containers exchange had to be
        GROUP-READABLE, because the uids could differ -- the credential files
        among them, holding SSH private keys and vault passphrases.

        Pinning makes those `0600`. It NARROWS the contract, which is a breaking
        change for an operator EE that cannot run as 1000, and is recorded as one
        in `docs/upgrading-to-2.0.md`; the agent refuses such an image at the
        startup probe rather than letting it meet an unreadable file mid-play.

        Inverted rather than deleted: the decision is the thing worth pinning,
        and whichever way it goes a test should fail if it changes silently.
        `runAsNonRoot` stays -- redundant now, kept as the independent statement
        it always was.
        """
        sec = _init(_pod(ee_image=EE), "ee")["securityContext"]
        assert sec["runAsUser"] == 1000
        assert sec["runAsGroup"] == 1000
        assert sec["runAsNonRoot"] is True
        assert sec["readOnlyRootFilesystem"] is True

    def test_the_pinned_uid_is_the_fsgroup(self):
        """One number, not two that happen to agree.

        The uid has to be the volume's group owner or the pinning buys nothing:
        a sidecar pinned to a uid outside the `fsGroup` could not read the shared
        volume at all, which is a worse failure than the group-readable files the
        pinning removes.
        """
        pod = _pod(ee_image=EE)
        assert (
            _init(pod, "ee")["securityContext"]["runAsUser"] == (pod["securityContext"]["fsGroup"])
        )


class TestTheVolumeContract:
    def test_both_containers_share_the_agent_volume_and_the_workspace(self):
        pod = _pod(ee_image=EE)
        ee = {m["name"]: m["mountPath"] for m in _init(pod, "ee")["volumeMounts"]}
        runner = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
        assert ee["tp"] == "/tp" and runner["tp"] == "/tp"
        # The playbook directory and the resolved inventory ride the workspace
        # volume the orchestrator already fetches into, so the handoff needs no
        # volume of its own.
        assert ee["workspace"] == "/workspace" and runner["workspace"] == "/workspace"

    def test_the_ca_bundle_is_mounted_only_when_one_exists(self):
        assert all(m["name"] != "ca-merged" for m in _init(_pod(ee_image=EE), "ee")["volumeMounts"])
        with_ca = _init(_pod(ee_image=EE, ca_secret_name="tp-ca"), "ee")["volumeMounts"]
        assert any(m["name"] == "ca-merged" and m["readOnly"] for m in with_ca)

    def test_everything_writable_is_redirected_under_the_shared_volume(self):
        """Which is what lets the EE keep a read-only root filesystem, and lets
        a custom EE meet the contract without providing any particular writable
        path of its own."""
        env = {e["name"]: e.get("value") for e in _init(_pod(ee_image=EE), "ee")["env"]}
        for key in ("HOME", "TMPDIR", "ANSIBLE_LOCAL_TEMP"):
            assert env[key].startswith("/tp/"), f"{key} escapes the shared volume: {env[key]}"
        assert env["ANSIBLE_COLLECTIONS_PATH"].startswith("/tp/")
        assert env["ANSIBLE_ROLES_PATH"].startswith("/tp/")

    def test_the_fsGroup_is_merged_UNDER_the_operator_s_own_context(self):
        """Their fsGroup is the deployment's business; we only supply one when
        they have not. Merged rather than assigned, because the EE block runs
        after `pod_security_context` is applied."""
        assert _pod(ee_image=EE)["securityContext"]["fsGroup"] == 1000
        cfg = _cfg()
        cfg.pod_security_context = {"fsGroup": 2000, "runAsNonRoot": True}
        pod = build_job_spec(
            run_id="run-0123456789abcdef",
            phase="apply",
            runner_config=cfg,
            auth_secret_name="tprun-auth",
            env_vars=[],
            terraform_vars=[],
            namespace="terrapod-runners",
            ee_image=EE,
        )["spec"]["template"]["spec"]
        assert pod["securityContext"]["fsGroup"] == 2000, "the operator's fsGroup was overridden"
        assert pod["securityContext"]["runAsNonRoot"] is True


class TestTheSharedToken:
    def test_both_containers_read_it_from_the_per_run_secret(self):
        pod = _pod(ee_image=EE)
        for container in (_init(pod, "ee"), pod["containers"][0]):
            entry = next(e for e in container["env"] if e["name"] == "TP_EE_TOKEN")
            ref = entry["valueFrom"]["secretKeyRef"]
            assert (ref["name"], ref["key"]) == ("tprun-auth", "ee-token")
            assert "value" not in entry, "the token must never be plaintext in the Job spec"

    def test_the_orchestrator_s_ee_env_is_appended_after_the_operator_s(self):
        """Kubernetes gives the LAST duplicate precedence, so appending after
        `extraEnv` is what stops the agent's address or token being redirected
        by an operator env entry or a workspace variable."""
        cfg = _cfg()
        cfg.extra_env = [{"name": "TP_EE_ENDPOINT", "value": "http://attacker.invalid"}]
        pod = build_job_spec(
            run_id="run-0123456789abcdef",
            phase="apply",
            runner_config=cfg,
            auth_secret_name="tprun-auth",
            env_vars=[],
            terraform_vars=[],
            namespace="terrapod-runners",
            ee_image=EE,
        )["spec"]["template"]["spec"]
        names = [e["name"] for e in pod["containers"][0]["env"]]
        ours = [i for i, n in enumerate(names) if n == "TP_EE_ENDPOINT"]
        assert len(ours) == 2, "expected the operator's entry and ours"
        last = pod["containers"][0]["env"][ours[-1]]
        assert last["value"] == "http://127.0.0.1:7781", "ours must win the duplicate"


class TestTheResources:
    def test_limits_are_twice_the_requests(self):
        ee = _init(_pod(ee_image=EE, ee_resource_cpu="2", ee_resource_memory="4Gi"), "ee")
        assert ee["resources"]["requests"] == {"cpu": "2", "memory": "4Gi"}
        assert ee["resources"]["limits"] == {"cpu": "4", "memory": "8Gi"}

    def test_an_empty_size_takes_the_builders_own_default(self):
        """Which is the case for a run created by an API that predates the
        columns -- it renders a valid Job rather than an empty request."""
        ee = _init(_pod(ee_image=EE), "ee")
        assert ee["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}

    def test_the_builder_default_matches_the_deployment_default(self):
        """Two constants on purpose, pinned equal here.

        The listener does not mount the API's ConfigMap, so reading
        `Settings.default_ee_resource_*` in the builder would silently yield the
        CODE default rather than the operator's value. Keeping them separate is
        right; letting them drift is not.
        """
        from terrapod.config import Settings

        fields = Settings.model_fields
        assert job_template._EE_DEFAULT_CPU == fields["default_ee_resource_cpu"].default
        assert job_template._EE_DEFAULT_MEMORY == fields["default_ee_resource_memory"].default
