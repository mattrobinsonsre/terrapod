"""The engine seam: which IaC system a workspace, run and config version belong to.

#1407 phase 1. Terraform is the only implementation, and its behaviour is
preserved exactly — the point of this package is that the seam exists and is
exercised by the one caller there is, not that it anticipates what Pulumi or
Ansible will need. Guessed extension points are how an abstraction ends up
fitting nothing.

**`engine` is not `execution_backend`.** They sit next to each other on the same
tables and mean different things:

    engine              which IaC system:  terraform | (later) pulumi, ansible
    execution_backend   which BINARY inside the Terraform family: tofu | terraform

So a workspace is `engine=terraform, execution_backend=tofu` — one names the
family, the other picks the tool within it. Two unrelated `engine` columns also
exist on `SecurityScanResult` and `OnboardingSession` meaning something else
again (checkov/trivy, and onboarding source); they are untouched.

**This package must stay dependency-light.** It is imported by the API *and* by
the listener, whose image carries only `config`, `logging_config`, `http_retry`
and `runner/` — so pulling SQLAlchemy or the DB models in here would either break
that image or force it to grow a dependency it has no use for. Anything needing a
model should take plain values, or import it under `TYPE_CHECKING`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: The value stored in the `engine` column for Terraform/OpenTofu workspaces, and
#: the default for every row that does not say otherwise.
TERRAFORM = "terraform"

DEFAULT_ENGINE = TERRAFORM


@runtime_checkable
class EngineStrategy(Protocol):
    """What the run pipeline needs to know that differs between engines.

    Deliberately small. Phases 2 and 3 of #1407 move `build_job_spec`'s parameter
    list and terminal-run resolution behind this; until then it carries only what
    is already engine-specific in the code today.
    """

    #: The discriminator value this strategy serves — matches the `engine` column.
    name: str

    #: The phases a run performs, in order. Terraform plans then applies; Pulumi
    #: previews then updates; an Ansible run has no separable plan. Kept here
    #: because the vocabulary is the engine's, not the platform's.
    phases: tuple[str, ...]

    #: The binary used when a workspace expresses no preference.
    default_execution_backend: str

    #: Which i18n namespace holds this engine's *display* vocabulary
    #: (`phases.<vocabulary>.…`). Internal state names never change — a run is
    #: `planning` whatever engine it belongs to — but what a person is shown does:
    #: Terraform plans and applies, Pulumi previews and updates, Ansible checks
    #: and runs. #1407 §3 requires that difference stay visible rather than be
    #: smoothed over, and a key namespace is how it survives translation.
    vocabulary: str

    #: Whether this engine's runner evaluates OPA policy sets against its plan.
    #: The post-plan policy gate fails closed on a missing evaluation, so an
    #: engine that never evaluates must say so, or every apply is held (#1567).
    evaluates_policy_sets: bool

    #: Whether this engine's runner performs the IaC security scan (Checkov and
    #: Trivy read Terraform plan JSON). Same reason as above.
    evaluates_security_scans: bool

    #: Whether the AI policy gate rules on a run of this engine (#1766). The
    #: gate reads the structured plan the summariser was given, so an engine
    #: whose uploaded plan artifact is a TRUNCATED summary must answer False:
    #: a gate that decides over a capped list of resources can allow a plan
    #: because the offending resource fell off the end, which is a worse
    #: failure than not gating at all.
    evaluates_ai_policy: bool

    #: Whether a run of this engine is cost-estimated (#1569). The engine is
    #: what decides, not the deployment's `cost_estimation.enabled`, because
    #: the answer is about whether this engine's plan can be priced at all —
    #: an engine that describes no resources (Ansible has no separable plan)
    #: has nothing to price, and instructing its runner to try would spend a
    #: pricesheet download on producing an empty estimate that reads as "this
    #: change costs nothing".
    estimates_cost: bool

    #: Whether the architecture critic may reason over this engine's state
    #: (#1911). The critic compacts a Terraform **state v4** document into a
    #: resource graph and grounds its findings in a cost estimate built the same
    #: way, so an engine whose state is not that document must answer False.
    #:
    #: This one fails the way `honours_drift_ignore_rules` does, not the way the
    #: gates do: `build_graph_from_state` handed a Pulumi deployment does not
    #: raise -- it finds no `mode`/`name`/`instances` and returns an EMPTY graph.
    #: The critic would then describe an architecture of nothing, in confident
    #: prose, and present it to an operator as a review of their stack. A wrong
    #: answer that reads as a right one, which is worse than no critique.
    critiques_architecture: bool

    #: Whether `drift_ignore_rules` may be applied to this engine's drift run
    #: (#1561). The rules are globs over Terraform attribute PATHS, matched by
    #: `drift_ignore_classifier` against `resource_changes`/`resource_drift` in
    #: an OpenTofu-format plan. An engine whose uploaded plan artifact is not
    #: that document must answer False -- and this one fails OPEN, which is why
    #: it needs its own flag rather than reusing one above. Handed a document it
    #: cannot read, the classifier finds nothing drifted and reports the
    #: workspace CLEAN; none of `_apply_drift_ignore_rules`' conservative
    #: fallbacks fire, because nothing errored.
    honours_drift_ignore_rules: bool

    #: Whether this engine can tell the platform which provider configurations a
    #: run uses, before the run executes (#2006). Terraform can: `graph` is a
    #: static walk of the configuration, so the cloud-identity phase enumerates
    #: the provider configurations and prunes the ones nothing references.
    #:
    #: Pulumi cannot, and the reason is structural rather than a missing feature:
    #: a Pulumi program is arbitrary code and provider instances are constructed
    #: at runtime, so there is nothing to walk before the program runs -- and the
    #: thing that would run it, `preview`, is precisely what needs the
    #: credentials. `pulumi stack graph` reads an existing stack's STATE, not the
    #: program, so it cannot answer on a first run and never enumerates aliased
    #: instances the program builds.
    #:
    #: False therefore means "mint every identity this workspace resolves",
    #: which is a widening and is deliberate: Terrapod keeps no central
    #: restriction on which targets a workspace may mint for, and the cloud-side
    #: trust policy is the gate. Discovery was always described as a filter
    #: rather than the source of truth -- an engine that cannot discover simply
    #: does not get the filter.
    #:
    #: **Read by the API, not the runner.** The runner image does not ship
    #: `terrapod.engines` (see `Dockerfile.runner`), and the API has the better
    #: answer anyway: it reads the engine off the workspace row rather than
    #: trusting a runner's claim about which engine it is.
    discovers_provider_configurations: bool

    def build_job_spec(self, **kwargs: Any) -> dict:
        """Build the Kubernetes Job spec for one phase of a run."""
        ...


def engine_enabled(engine: str) -> bool:
    """Whether an engine is offered by this deployment (#1429).

    Defined here rather than in `services/engine_gating.py` because the listener
    image ships `engines/` and no `services/` — so a gate living there could not
    be consulted by the strategy registry, and the registry is where gating a
    *strategy* has to happen. `engine_gating` imports this rather than declaring
    a second copy, so there is one answer to "is this engine on".

    Terraform is always enabled: it is not an optional engine, it is what
    Terrapod is.
    """
    if engine == DEFAULT_ENGINE:
        return True
    from terrapod.config import settings

    config = getattr(settings.engines, engine, None)
    if config is None:
        raise ValueError(f"unknown engine: {engine}")
    return bool(config.enabled)


def strategy_for(engine: str | None) -> EngineStrategy:
    """Resolve the strategy for an engine value.

    `None` and the empty string resolve to Terraform rather than raising: rows
    written before the column existed default to it, and a caller reading an
    older record should get the same answer the database would.
    """
    key = (engine or DEFAULT_ENGINE).strip().lower()
    strategy = _REGISTRY.get(key)
    if strategy is not None and not engine_enabled(key):
        # Distinguished from "unknown" deliberately. An operator who turned the
        # engine off wants to be told that, not that Terrapod has never heard of
        # it — the two have completely different fixes.
        raise ValueError(
            f"engine {key!r} is not enabled on this deployment "
            f"(set engines.{key}.enabled to turn it on)"
        )
    if strategy is None:
        # Deliberately not a silent fallback. An unknown engine means a row was
        # written by a newer replica mid-rollout, or by hand; running it as
        # Terraform would execute the wrong tool against real infrastructure.
        raise ValueError(
            f"unknown engine {engine!r} — known engines: {', '.join(sorted(_REGISTRY))}"
        )
    return strategy


def evaluates_policy_sets(engine: str | None) -> bool:
    """Whether a run of this engine is evaluated against OPA policy sets (#1567).

    Deliberately not gated: a Pulumi workspace still exists, and still reaches
    the post-plan gate, after `engines.pulumi` is switched off. An unknown engine
    answers True, so the gate keeps failing closed for a row nobody can vouch for.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return True if strategy is None else strategy.evaluates_policy_sets


def evaluates_security_scans(engine: str | None) -> bool:
    """Whether a run of this engine is security-scanned (#1567). As above."""
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return True if strategy is None else strategy.evaluates_security_scans


def evaluates_ai_policy(engine: str | None) -> bool:
    """Whether the AI policy gate rules on a run of this engine (#1766).

    An unknown engine answers True, as the other two do, so the gate keeps
    failing closed for a row nobody can vouch for.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return True if strategy is None else strategy.evaluates_ai_policy


def estimates_cost(engine: str | None) -> bool:
    """Whether a run of this engine is cost-estimated (#1569).

    **An unknown engine answers False**, with the three gate predicates above —
    which answer True — and with `honours_drift_ignore_rules`, which does not.
    The direction is set by what the wrong answer costs, not by consistency:
    those three are gates, so answering True keeps them failing closed. Here
    the wrong answer is a NUMBER shown to an operator. A plan from an engine
    nobody can vouch for prices nothing, and an estimate of nothing is
    indistinguishable from a change that is genuinely free. Saying "not
    estimated" is the honest answer for a row we cannot read.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return False if strategy is None else strategy.estimates_cost


def critiques_architecture(engine: str | None) -> bool:
    """Whether the architecture critic may reason over this engine's state (#1911).

    **An unknown engine answers False**, for the same reason as `estimates_cost`:
    the wrong answer here is not a held apply, it is a confident description of a
    stack shown to an operator. A state document nobody can vouch for compacts to
    an empty graph, and a critique of an empty graph is indistinguishable from a
    critique of a simple system.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return False if strategy is None else strategy.critiques_architecture


def honours_drift_ignore_rules(engine: str | None) -> bool:
    """Whether a drift run of this engine may be filtered by `drift_ignore_rules`.

    **An unknown engine answers False, unlike the three predicates above.** They
    fail closed by answering True because gating is the safe direction for them.
    Here the safe direction is the opposite: answering True hands the classifier
    a document it may not understand, and being unable to read it looks exactly
    like "nothing drifted". For a row nobody can vouch for, reporting drift is
    the conservative answer -- the same choice `_apply_drift_ignore_rules`
    already makes on every one of its own failure paths.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return False if strategy is None else strategy.honours_drift_ignore_rules


def discovers_provider_configurations(engine: str | None) -> bool:
    """Whether this engine can enumerate its run's provider configurations.

    **An unknown engine answers True**, which is the conservative direction
    here even though it is the noisier one. True sends the runner to the
    engine's graph command; for an engine nobody has vouched for that command
    does not exist, so discovery reports `failed` and the mint is refused with a
    409 -- loud, and only for a workspace that actually holds identity, because
    the endpoint answers 204 on an empty mapping before it looks at the outcome.

    Answering False instead would hand an unvouched-for engine a token for every
    identity the workspace resolves, on the strength of a registry entry nobody
    has reviewed. Between a run that fails with a reason and a run that quietly
    gets more credentials than anyone chose, #1442 settles it: the failure mode
    of this credential is escalation, not absence.
    """
    strategy = _REGISTRY.get((engine or DEFAULT_ENGINE).strip().lower())
    return True if strategy is None else strategy.discovers_provider_configurations


def known_engines() -> tuple[str, ...]:
    """Every engine this deployment can run, for validation and error messages.

    Filtered by the gate, so a gated-off engine is absent rather than listed and
    then refused — the same rule the surfaces follow.
    """
    return tuple(sorted(name for name in _REGISTRY if engine_enabled(name)))


def _build_registry() -> dict[str, EngineStrategy]:
    """Every strategy this build CONTAINS, before gating.

    Built once at import and filtered at resolve time. Reading config here
    instead would freeze the answer for the life of the process, which no test
    could vary and no operator could change without a restart.
    """
    from terrapod.engines.pulumi import PulumiStrategy
    from terrapod.engines.terraform import TerraformStrategy

    strategies: list[EngineStrategy] = [TerraformStrategy(), PulumiStrategy()]
    return {s.name: s for s in strategies}


_REGISTRY: dict[str, EngineStrategy] = _build_registry()
