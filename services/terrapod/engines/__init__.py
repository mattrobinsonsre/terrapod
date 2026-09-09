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
