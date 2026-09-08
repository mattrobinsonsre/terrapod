"""The Terraform/OpenTofu strategy — today's behaviour, byte for byte.

#1407 phases 1 and 2. `TerraformRunOptions` collects the twenty run options that
used to be twenty positional parameters on a general-purpose Job builder, and the
env translation that reads them now lives here rather than inside that builder.

The block below was **moved verbatim** rather than rewritten. The only changes are
the name of the accumulator and the indent — the options are rebound as locals at
the top precisely so the transplanted lines could stay identical. For a refactor
whose failure mode is a Job that launches with a subtly different spec, a
transformation you can read as "unchanged" beats one you have to re-derive.

Everything here is Terraform's, including the parts that look generic:
`TP_VERIFY_BINARIES` and `TP_SIGNING_KEY_*` verify the terraform/tofu/terragrunt
binaries specifically, and `TP_COST_*` prices a terraform plan. A second engine
gets its own translation beside this one, not another branch inside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from terrapod.config import settings

#: Mirrors `db.models.ONBOARDING_DISCOVERY_SOURCE`. Duplicated as a literal rather
#: than imported: this module is loaded by the listener image, which ships no DB
#: layer, and a test pins the two in step.
ONBOARDING_DISCOVERY_SOURCE = "onboarding-discovery"

if TYPE_CHECKING:  # the listener image ships no DB layer — see tests/meta
    from terrapod.config import RunnerConfig


@dataclass(frozen=True)
class TerraformRunOptions:
    """How one Terraform run differs from the default.

    Frozen because a Job spec is built once from it; a builder that could mutate
    its own inputs halfway through is a bug waiting for a second caller.
    """

    terraform_version: str = ""
    execution_backend: str = ""
    terragrunt_enabled: bool = False
    terragrunt_version: str = ""
    plan_only: bool = False
    var_files: list[str] | None = None
    target_addrs: list[str] | None = None
    replace_addrs: list[str] | None = None
    refresh_only: bool = False
    refresh: bool = True
    allow_empty_apply: bool = False
    is_destroy: bool = False
    parallelism: int = 10
    cost_estimation: bool = True
    cost_default_region: str = "us-east-1"
    working_directory: str = ""
    onboard_session_id: str = ""
    onboard_provider: str = ""
    onboard_provider_version: str = ""
    onboard_types: list[str] | None = None


class TerraformStrategy:
    """Terraform and OpenTofu, which share one engine and differ only in binary.

    `execution_backend` picks between them per workspace — a choice *within* this
    engine, not a different engine, which is why both are served here rather than
    by two strategies.
    """

    name = "terraform"

    #: `terraform plan` then `terraform apply`. Naming the vocabulary here is what
    #: lets a later engine say `preview`/`update` without the platform hard-coding
    #: Terraform's words.
    phases = ("plan", "apply")

    #: OpenTofu, matching the column default on workspaces.
    default_execution_backend = "tofu"

    #: Resolves to `phases.terraform.*` in the message catalogues, which hold
    #: exactly the words shown today — this restructures where they live, not
    #: what they say.
    vocabulary = "terraform"

    #: Which of this engine's phases each internal run status belongs to.
    #:
    #: The status names are the platform's and never change — a run is `planning`
    #: whatever engine it belongs to. This says what `planning` *is* for this
    #: engine, so an API consumer can report "previewing" for a Pulumi run
    #: without having to know by convention that `planning` means preview there.
    status_phases = {
        "planning": "plan",
        "planned": "plan",
        "applying": "apply",
        "applied": "apply",
    }

    def container_env(
        self, options: TerraformRunOptions, runner_config: RunnerConfig
    ) -> list[dict[str, Any]]:
        """The TP_* instructions this engine gives its entrypoint.

        Returned as a list and spliced into the container env at exactly the
        position these lines occupied before, so the rendered order is unchanged —
        which the golden spec matrix pins.
        """
        env: list[dict[str, Any]] = []
        terraform_version = options.terraform_version
        execution_backend = options.execution_backend
        terragrunt_enabled = options.terragrunt_enabled
        terragrunt_version = options.terragrunt_version
        plan_only = options.plan_only
        var_files = options.var_files
        target_addrs = options.target_addrs
        replace_addrs = options.replace_addrs
        refresh_only = options.refresh_only
        refresh = options.refresh
        allow_empty_apply = options.allow_empty_apply
        is_destroy = options.is_destroy
        parallelism = options.parallelism
        cost_estimation = options.cost_estimation
        cost_default_region = options.cost_default_region
        working_directory = options.working_directory
        onboard_session_id = options.onboard_session_id
        onboard_provider = options.onboard_provider
        onboard_provider_version = options.onboard_provider_version
        onboard_types = options.onboard_types

        # Terraform version + backend
        version = terraform_version or runner_config.default_terraform_version
        backend = execution_backend or runner_config.default_execution_backend
        env.append({"name": "TP_VERSION", "value": version})
        env.append({"name": "TP_BACKEND", "value": backend})
        # Runner-side executable verification level (#607): the runner re-verifies
        # the terraform/tofu/terragrunt binary against the publisher's signed
        # SHA256SUMS with its own pinned key before executing it. Mirrors the
        # server's binary_cache.verify so operators control it in one place.
        env.append({"name": "TP_VERIFY_BINARIES", "value": settings.registry.binary_cache.verify})
        # Operator-overridden publisher keys (#607): propagate the configured trust
        # set to the Job so runner-side verification uses the same keys as the API
        # (set at Job-creation from config, not fetched at request time → not an
        # attacker-controllable trust anchor). Empty (default) → runner uses bundled.
        for _tool, _armor in settings.registry.binary_cache.signing_keys.items():
            if _armor:
                env.append({"name": f"TP_SIGNING_KEY_{_tool.upper()}", "value": _armor})
        # Terragrunt (#534): the runner wraps tofu/terraform with terragrunt when
        # enabled. Version is partial (e.g. "1.0") — the binary cache resolves it.
        if terragrunt_enabled:
            env.append({"name": "TP_TERRAGRUNT_ENABLED", "value": "true"})
            env.append({"name": "TP_TERRAGRUNT_VERSION", "value": terragrunt_version or "1.0"})
        if plan_only:
            env.append({"name": "TP_PLAN_ONLY", "value": "true"})
        if var_files:
            env.append({"name": "TP_VAR_FILES", "value": json.dumps(var_files)})
        if target_addrs:
            env.append({"name": "TP_TARGET_ADDRS", "value": json.dumps(target_addrs)})
        if replace_addrs:
            env.append({"name": "TP_REPLACE_ADDRS", "value": json.dumps(replace_addrs)})
        if refresh_only:
            env.append({"name": "TP_REFRESH_ONLY", "value": "true"})
        if not refresh:
            env.append({"name": "TP_REFRESH", "value": "false"})
        if allow_empty_apply:
            env.append({"name": "TP_ALLOW_EMPTY_APPLY", "value": "true"})
        if is_destroy:
            env.append({"name": "TP_DESTROY", "value": "true"})
        # Always emitted, never only-when-non-default: the runner has no way to know
        # the workspace's setting otherwise, and a default that drifts between API
        # and runner across a version skew would be invisible (#1431).
        env.append({"name": "TP_PARALLELISM", "value": str(parallelism)})
        # Cost estimation (#871). The runner defaults to enabled, so only emit the
        # env when the API instructs OFF; always ship the fallback region so a
        # resource whose region can't be resolved is priced consistently.
        if not cost_estimation:
            env.append({"name": "TP_COST_ESTIMATION", "value": "false"})
        elif cost_default_region:
            env.append({"name": "TP_COST_DEFAULT_REGION", "value": cost_default_region})
        if working_directory:
            env.append({"name": "TP_WORKING_DIR", "value": working_directory})

        # Onboarding discovery (#824 P2): a non-empty session id makes the entrypoint
        # run D2/D3 (terrapod-query) instead of a workspace plan. The run stays
        # plan-phase for all infra (Job name / Redis keys / reconciler).
        if onboard_session_id:
            env.append({"name": "TP_ONBOARD_SESSION_ID", "value": onboard_session_id})
            env.append({"name": "TP_ONBOARD_PROVIDER", "value": onboard_provider})
            env.append({"name": "TP_ONBOARD_PROVIDER_VERSION", "value": onboard_provider_version})
            env.append({"name": "TP_ONBOARD_TYPES", "value": json.dumps(onboard_types or [])})

        return env

    def resolve_terminal(
        self, *, run_status: str, run_source: str, job_status: str
    ) -> TerminalOutcome:
        """What a finished Job means for Terraform (#1407 phase 3).

        The rules are today's, moved rather than rewritten:

        * onboarding discovery settles the run and the session, skipping the plan
          machinery entirely — there is no plan to complete;
        * a succeeded Job completes the plan or the apply, chosen by which state
          the run is in;
        * a failed or deleted Job errors the run.

        A second engine answers this differently and that is the point: a
        non-zero exit means something else for a playbook with `ignore_errors`,
        and Ansible has no artifact binding approval to application at all.
        """
        phase = "plan" if run_status == "planning" else "apply"

        if job_status == "succeeded":
            if run_source == ONBOARDING_DISCOVERY_SOURCE:
                return TerminalOutcome(action="discovery_succeeded", phase="plan")
            if run_status == "planning":
                return TerminalOutcome(action="complete_plan", phase=phase)
            if run_status == "applying":
                return TerminalOutcome(action="complete_apply", phase=phase)
            # Neither planning nor applying: the runner's own POST already drove
            # the transition. The helpers are idempotent, but there is nothing
            # left to complete, so say so rather than calling one anyway.
            return TerminalOutcome(action="none", phase=phase)

        if job_status in ("failed", "deleted"):
            return TerminalOutcome(action="error", phase=phase)

        return TerminalOutcome(action="none", phase=phase)

    def build_job_spec(
        self,
        *,
        options: TerraformRunOptions | None = None,
        **kwargs: Any,
    ) -> dict:
        """Build the Job spec: engine env from `options`, the rest from the
        neutral builder.

        The strategy composes rather than being called by the builder — the
        builder knows nothing about Terraform, and this is the only place that
        knows both.
        """
        from terrapod.runner.job_template import build_job_spec

        opts = options or TerraformRunOptions()
        runner_config = kwargs["runner_config"]
        return build_job_spec(engine_env=self.container_env(opts, runner_config), **kwargs)


@dataclass(frozen=True)
class TerminalOutcome:
    """What a finished Job means, decided by the engine and executed by the caller.

    Deliberately a *decision*, not the act. The reconciler owns the database
    work — `run_service`, the onboarding session, unlocking the workspace — and
    this says only which of those applies. Two reasons that split is the right
    one, and neither is stylistic:

    Terminal resolution is imported by the listener image, which ships no DB
    layer (see `tests/meta/test_engines_stay_listener_safe.py`). A resolver that
    took a `Run` would pull SQLAlchemy in, and the failure would be a
    crash-looping listener in a deployment rather than a red test.

    And a decision made from plain values is testable without a database, which
    is what lets the rules be pinned exhaustively rather than sampled.
    """

    #: What the caller should do: complete_plan | complete_apply | error |
    #: discovery_succeeded | discovery_failed | none.
    action: str
    #: Which phase's live log to persist before acting — the engine's vocabulary.
    phase: str
