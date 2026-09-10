"""Pulumi as an execution engine (#1523, #1407 phase 5).

The second implementation of the seam #1487–#1489 built, and the first time
engine gating applies to a *strategy* rather than a surface.

**What differs from Terraform, and why it is not an `if` somewhere.** Terraform
writes a plan file and applies it; Pulumi does the same shape with different
words — `preview --save-plan` then `up --plan` — which #1501 verified end to end.
Both are a single container, so the Job is a sibling of Terraform's rather than a
new Pod shape. Where they genuinely diverge is what a finished Job *means*, and
that is what `resolve_terminal` below answers for Pulumi rather than Terraform
answering for both.

**Listener-safe.** This module is imported by the listener, whose image ships no
DB layer (`tests/meta/test_engines_stay_listener_safe.py` pins that). Nothing
here imports a model or opens a session; the strategy takes plain values and
returns plain values. A violation is not a red test in CI — it is a
crash-looping listener in somebody's cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terrapod.engines.terraform import TerminalOutcome

#: Pulumi writes its plan here inside the Job, and reads it back on the update.
#: One constant because the two phases must agree, and a typo would surface as
#: "no plan file" on the update rather than anywhere near the preview.
PLAN_FILE = "/workspace/plan.json"


@dataclass(frozen=True)
class PulumiRunOptions:
    """How one Pulumi run differs from the default.

    Frozen for the same reason Terraform's is: a Job spec is built once from it,
    and a builder that can mutate its own inputs halfway through is a bug waiting
    for a second caller.
    """

    phase: str = "preview"
    #: The stack this run targets, as `{project}/{stack}`.
    stack: str = ""
    #: Pinned CLI version; empty means the image's own.
    pulumi_version: str = ""
    #: Working directory within the fetched configuration.
    working_directory: str = ""
    #: A destroy rather than an update.
    is_destroy: bool = False
    #: A refresh before the operation, matching Terraform's `-refresh`.
    refresh: bool = True
    #: `--target` equivalents; Pulumi spells them URNs.
    target_urns: list[str] | None = None
    resource_cpu: str = ""
    resource_memory: str = ""
    parallelism: int = 0
    timeout_minutes: int = 0
    env_vars: list[dict[str, Any]] = field(default_factory=list)
    #: Save the preview's plan and bind the update to it (#1553). Off by default.
    bind_plan: bool = False


class PulumiStrategy:
    """Pulumi.

    `execution_backend` has no meaning here — there is one binary, unlike the
    tofu/terraform split *within* the Terraform engine — so it names the CLI
    itself rather than a choice.
    """

    name = "pulumi"

    #: `pulumi preview` then `pulumi up`. Terraform's plan/apply in Pulumi's own
    #: words, which is exactly why the vocabulary is per-engine rather than a
    #: platform-wide label (#1521).
    phases = ("preview", "update")

    #: One binary; no equivalent of the tofu/terraform choice.
    default_execution_backend = "pulumi"

    #: Resolves to `phases.pulumi.*` in the message catalogues.
    vocabulary = "pulumi"

    #: Which phase each internal run status belongs to. The platform's status
    #: names never change — a run is `planning` whatever engine it belongs to —
    #: and this is what stops a Pulumi run being described as "planning" to a
    #: user who has only ever seen the word "preview".
    status_phases = {
        "planning": "preview",
        "planned": "preview",
        "applying": "update",
        "applied": "update",
    }

    def container_env(self, options: PulumiRunOptions, runner_config: Any) -> list[dict[str, Any]]:
        """The TP_* instructions this engine gives its entrypoint.

        Returned as a list and spliced into the container env at the same
        position Terraform's occupies, so the two Job specs stay siblings.
        """
        env: list[dict[str, Any]] = [
            {"name": "TP_ENGINE", "value": "pulumi"},
            {"name": "TP_PULUMI_PHASE", "value": options.phase},
            {"name": "TP_PULUMI_PLAN_FILE", "value": PLAN_FILE},
        ]
        if options.stack:
            env.append({"name": "TP_PULUMI_STACK", "value": options.stack})
        if options.pulumi_version:
            env.append({"name": "TP_PULUMI_VERSION", "value": options.pulumi_version})
        if options.working_directory:
            env.append({"name": "TP_WORKING_DIR", "value": options.working_directory})
        if options.is_destroy:
            env.append({"name": "TP_DESTROY", "value": "true"})
        if not options.refresh:
            env.append({"name": "TP_REFRESH", "value": "false"})
        if options.target_urns:
            import json

            env.append({"name": "TP_TARGET_URNS", "value": json.dumps(options.target_urns)})
        if options.parallelism:
            env.append({"name": "TP_PARALLELISM", "value": str(options.parallelism)})
        if options.bind_plan:
            env.append({"name": "TP_PULUMI_BIND_PLAN", "value": "true"})
        return env

    def options_from_attrs(self, attrs: dict, phase: str) -> PulumiRunOptions:
        """Build this engine's run options from the wire payload (#1523).

        `phase` is the Terraform-shaped one the platform sends — `plan`/`apply`,
        which is what the run status is called for every engine — translated
        here into Pulumi's own words. That translation lives with the engine
        rather than in the listener for the same reason the vocabulary does
        (#1521): the platform's statuses stay engine-neutral and each engine
        says what they mean to it.
        """
        return PulumiRunOptions(
            phase="preview" if phase == "plan" else "update",
            stack=attrs.get("pulumi-stack", ""),
            pulumi_version=attrs.get("pulumi-version", ""),
            working_directory=attrs.get("working-directory", ""),
            is_destroy=attrs.get("is-destroy", False),
            refresh=attrs.get("refresh", True),
            target_urns=attrs.get("target-urns"),
            resource_cpu=attrs.get("resource-cpu", ""),
            resource_memory=attrs.get("resource-memory", ""),
            parallelism=attrs.get("parallelism", 0),
            timeout_minutes=attrs.get("timeout-minutes", 0),
            bind_plan=bool(attrs.get("pulumi-bind-plan", False)),
        )

    def build_job_spec(self, **kwargs: Any) -> dict:
        """Compose the neutral builder with this engine's env.

        The builder is engine-agnostic by #1488; everything Pulumi-specific
        arrives through `engine_env`. That is what keeps adding an engine from
        touching the Job-construction code every other engine shares — and why
        Terraform's golden spec matrix does not move when this lands.
        """
        from terrapod.runner.job_template import build_job_spec as _build

        options: PulumiRunOptions = kwargs.pop("options")
        # Subscript, not `.get`, exactly as Terraform's strategy does: the
        # listener always passes it, so a missing one is a bug worth raising on
        # rather than a None to carry into the builder. It also keeps the key out
        # of the wire-contract gate, which reads every `.get("...")` in this
        # package as a payload key and would otherwise freeze an internal kwarg
        # name into the runner protocol.
        runner_config = kwargs["runner_config"]
        # Everything except `engine_env` comes from the listener's kwargs, as it
        # does for Terraform. Re-deriving those fields from `options` here was
        # wrong four times over, and only the first was visible:
        #
        #   phase            `options.phase` is Pulumi's word ("preview"), but the
        #                    builder's is the platform's ("plan") -- it names the
        #                    Job `tprun-{short}-{phase}`, which must match the
        #                    auth/vars Secrets the listener already named with the
        #                    platform phase. Pulumi's verb reaches the entrypoint
        #                    as TP_PULUMI_PHASE in `engine_env`, which is the whole
        #                    point of the split.
        #   env_vars         `options_from_attrs` never populates it, so this sent
        #                    an empty list and discarded every real env var.
        #   timeout_minutes  defaults to 0, overriding the builder's 60.
        #   resource_*       default to "", overriding "1" / "2Gi".
        #
        # Passing them twice raised TypeError before any of the rest could bite,
        # which is the only reason this was caught as a launch failure rather
        # than as a Pulumi run that quietly had no environment.
        return _build(engine_env=self.container_env(options, runner_config), **kwargs)

    def resolve_terminal(
        self, *, run_status: str, run_source: str, job_status: str
    ) -> TerminalOutcome:
        """What a finished Job means for Pulumi.

        **Pulumi asks about the checkpoint** (#1407 §11), and that is the real
        divergence from Terraform rather than a rewording of it. Terraform's plan
        is an artifact the apply consumes, so a succeeded plan Job means "a plan
        exists to approve". Pulumi's `preview --save-plan` writes a plan file
        too, but the authoritative record of what happened is the checkpoint the
        CLI pushes to the service — which arrives over the #1522 surface *during*
        the run, not at the end of it.

        The consequence: a succeeded Job is the same signal in both engines, but
        for Pulumi the state is already durable by the time the Job exits,
        because the checkpoint was written mid-run. There is nothing to collect
        afterwards, so completion is a state transition and nothing more.

        A failed or deleted Job errors the run, as for Terraform. Pulumi has no
        equivalent of Ansible's `ignore_errors`, so a non-zero exit means the
        same thing here as there.
        """
        phase = "preview" if run_status == "planning" else "update"

        if job_status == "succeeded":
            if run_status == "planning":
                return TerminalOutcome(action="complete_plan", phase=phase)
            if run_status == "applying":
                return TerminalOutcome(action="complete_apply", phase=phase)
            # Neither planning nor applying: the checkpoint already drove the
            # transition. The completion helpers are idempotent, but there is
            # nothing left to complete, so say so rather than calling one anyway.
            return TerminalOutcome(action="none", phase=phase)

        if job_status in ("failed", "deleted"):
            return TerminalOutcome(action="error", phase=phase)

        # A status this engine does not recognise is not guessed at. Treating an
        # unfamiliar Job status as success would apply infrastructure on the
        # strength of a signal nobody defined.
        return TerminalOutcome(action="none", phase=phase)
