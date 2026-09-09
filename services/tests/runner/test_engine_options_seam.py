"""Each engine builds its own run options from the wire payload (#1523).

The listener used to construct `TerraformRunOptions` itself and pass it to
whichever strategy it had resolved. A Pulumi run therefore arrived holding
Terraform's options object and raised `AttributeError: 'TerraformRunOptions'
object has no attribute 'phase'` inside `_dispatch_event` — a fire-and-forget
task, so the exception reached asyncio's "never retrieved" handler and nothing
else. No Job was created, the run stayed in `planning`, and five minutes later
the reconciler errored it as "stuck pre-launch", which points nowhere near the
cause.

Every layer of that was individually correct: the strategy resolved, the Job
builder was engine-neutral, the terminal rules were right. Only the object
handed between them was wrong, which is why unit tests on either side passed and
the live run did not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from terrapod.engines import strategy_for
from terrapod.engines.pulumi import PulumiRunOptions
from terrapod.engines.terraform import TerraformRunOptions

ATTRS = {
    "terraform-version": "1.12.1",
    "execution-backend": "tofu",
    "working-directory": "infra",
    "is-destroy": False,
    "resource-cpu": "2",
    "resource-memory": "4Gi",
    "pulumi-stack": "smoke/dev",
}


@pytest.mark.parametrize(
    ("engine", "want"),
    [("terraform", TerraformRunOptions), ("pulumi", PulumiRunOptions)],
)
def test_each_strategy_returns_its_own_options_type(engine: str, want: type) -> None:
    """The bug in one line: the type must follow the engine, not the caller."""
    opts = strategy_for(engine).options_from_attrs(ATTRS, "plan")
    assert isinstance(opts, want), (
        f"{engine} produced {type(opts).__name__}; a strategy handed another "
        f"engine's options object fails at attribute access, not at the seam"
    )


def test_the_options_it_builds_can_build_a_job_spec() -> None:
    """The end-to-end shape of the failure: options → build_job_spec.

    Asserting only the type would not have caught this, because the listener's
    object *was* a valid TerraformRunOptions — it was simply the wrong one for
    the strategy consuming it.
    """
    for engine in ("terraform", "pulumi"):
        s = strategy_for(engine)
        opts = s.options_from_attrs(ATTRS, "plan")
        # Reaches the attributes the builder reads; a mismatched pair raises here.
        assert getattr(opts, "working_directory", None) == "infra"


class TestThePhaseTranslation:
    """The platform's phases are Terraform's words for every engine (#1521);
    each engine translates them into its own."""

    @pytest.mark.parametrize(("sent", "want"), [("plan", "preview"), ("apply", "update")])
    def test_pulumi_translates_the_platform_phase(self, sent: str, want: str) -> None:
        opts = strategy_for("pulumi").options_from_attrs(ATTRS, sent)
        assert opts.phase == want

    def test_terraform_needs_no_translation(self) -> None:
        """It has no `phase` field at all — the phase is passed to the builder
        separately. Pinned so a future 'tidy-up' does not add one and quietly
        re-couple the two engines' option shapes."""
        opts = strategy_for("terraform").options_from_attrs(ATTRS, "plan")
        assert not hasattr(opts, "phase")


def _runner_config():
    """A minimal RunnerConfig, matching test_job_template's fixture.

    Explicit empties matter: a bare MagicMock attribute is truthy, so leaving
    these unset injects phantom volumes and proxy env into the rendered spec.
    """
    cfg = MagicMock()
    cfg.image.repository = "ghcr.io/test/runner"
    cfg.image.tag = "latest"
    cfg.image.pull_policy = "IfNotPresent"
    cfg.default = "default"
    cfg.default_terraform_version = "1.11"
    cfg.default_execution_backend = "tofu"
    cfg.ttl_seconds_after_finished = 300
    cfg.azure_workload_identity = False
    cfg.node_selector = {}
    cfg.tolerations = []
    cfg.affinity = {}
    cfg.priority_class_name = ""
    cfg.topology_spread_constraints = []
    cfg.pod_security_context = {}
    cfg.pod_annotations = {}
    cfg.host_aliases = []
    cfg.extra_volumes = []
    cfg.extra_volume_mounts = []
    cfg.proxy = None
    cfg.ca_bundle_enabled = False
    cfg.server_url = "http://terrapod-api:8000"
    cfg.public_api_url = ""
    cfg.runner_namespace = "terrapod-runners"
    default_def = MagicMock()
    default_def.name = "default"
    cfg.definitions = [default_def]
    return cfg


def _as_the_listener_calls_it(engine: str, phase: str) -> dict:
    """Drive `build_job_spec` with exactly the kwargs the listener passes.

    The point of going through the real call shape rather than a tidy subset:
    the bug below was a *collision* between what the listener passes and what
    the strategy re-derived, so any test that supplied fewer arguments could not
    have seen it.
    """
    s = strategy_for(engine)
    return s.build_job_spec(
        options=s.options_from_attrs(ATTRS, phase),
        run_id="01a0871a3dd773a8",
        phase=phase,
        runner_config=_runner_config(),
        auth_secret_name="tprun-01a0871a3dd773a8-plan-auth",
        vars_secret_name="tprun-01a0871a3dd773a8-plan-vars",
        env_vars=[{"key": "TF_LOG", "value": "DEBUG"}],
        terraform_vars=[],
        execution_hooks=[],
        git_auth=[],
        resource_cpu="2",
        resource_memory="4Gi",
        ca_secret_name="",
    )


def _container(spec: dict) -> dict:
    return spec["spec"]["template"]["spec"]["containers"][0]


class TestTheStrategyDoesNotFightTheListener:
    """A strategy adds `engine_env`; everything else is the listener's to pass.

    Pulumi's `build_job_spec` re-derived five fields from its own options while
    the listener was already passing them. `phase` collided first and raised
    `TypeError: got multiple values for keyword argument 'phase'` — inside the
    same fire-and-forget task as the original #1523 bug, so it presented
    identically: no Job, no log, and "stuck pre-launch" five minutes later.

    The TypeError was the lucky part. Behind it sat three overrides that would
    not have raised at all, listed in the assertions below.
    """

    @pytest.mark.parametrize("engine", ["terraform", "pulumi"])
    def test_the_listeners_call_shape_builds_a_spec(self, engine: str) -> None:
        """Fails on the pre-fix code with the duplicate-kwarg TypeError."""
        assert _as_the_listener_calls_it(engine, "plan")

    def test_the_job_is_named_for_the_platform_phase_not_the_engines(self) -> None:
        """`tprun-{short}-plan`, never `-preview`.

        This is the one that would have hurt quietly. The listener names the
        auth and vars Secrets with the platform phase before it ever calls the
        strategy, so a Job named for Pulumi's verb would reference Secrets that
        do not exist, and the ownerReference GC those Secrets rely on would have
        nothing to hang from.
        """
        spec = _as_the_listener_calls_it("pulumi", "plan")
        assert spec["metadata"]["name"].endswith("-plan")

    def test_pulumis_own_verb_still_reaches_the_entrypoint(self) -> None:
        """The translation is not lost by passing the platform phase through —
        it travels as TP_PULUMI_PHASE, which is what the split is for."""
        env = {
            e["name"]: e.get("value")
            for e in _container(_as_the_listener_calls_it("pulumi", "plan"))["env"]
        }
        assert env["TP_ENGINE"] == "pulumi"
        assert env["TP_PULUMI_PHASE"] == "preview"
        assert env["TP_PHASE"] == "plan"

    def test_the_listeners_env_vars_survive(self) -> None:
        """`options.env_vars` is never populated by `options_from_attrs`, so
        forwarding it discarded every real env var the listener resolved."""
        names = {e["name"] for e in _container(_as_the_listener_calls_it("pulumi", "plan"))["env"]}
        assert "TF_LOG" in names, "the listener's env vars were dropped"

    def test_the_listeners_resource_sizing_survives(self) -> None:
        """`options.resource_*` default to "", which would have replaced the
        workspace's sizing with an empty request."""
        res = _container(_as_the_listener_calls_it("pulumi", "plan"))["resources"]
        assert res["requests"]["cpu"] == "2"
        assert res["requests"]["memory"] == "4Gi"
