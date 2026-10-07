"""Every engine strategy answers every capability the pipeline asks it (#2006).

The `EngineStrategy` Protocol is a `Protocol`, so nothing checks a strategy
against it at import time -- a missing member surfaces as an `AttributeError`
from whichever call site happens to read it first, in a request. The gate below
is the structural check the Protocol cannot perform on its own.

It exists because of exactly the shape this file was added alongside: adding a
capability means editing the Protocol and then remembering to edit every
strategy, and the registry is small enough that forgetting one is easy and
invisible until a run of that engine reaches the call site.
"""

from __future__ import annotations

import typing

import pytest

from terrapod import engines


def _declared_members() -> set[str]:
    """Every annotated member of the Protocol, methods excluded.

    Read off `__annotations__` rather than listed here, so a capability added to
    the Protocol is covered by this gate without anyone updating it.
    """
    return {
        name for name in typing.get_type_hints(engines.EngineStrategy) if not name.startswith("_")
    }


def _strategies() -> dict[str, engines.EngineStrategy]:
    # The registry the API reads, built the way production builds it.
    return engines._build_registry()


def test_every_strategy_declares_every_protocol_member():
    declared = _declared_members()
    assert declared, "the Protocol has no annotated members — has it moved?"

    missing: dict[str, list[str]] = {}
    for name, strategy in _strategies().items():
        absent = sorted(m for m in declared if not hasattr(strategy, m))
        if absent:
            missing[name] = absent

    assert not missing, (
        f"these engine strategies do not answer every capability the pipeline asks: "
        f"{missing}. A missing member is an AttributeError in a request, not at import."
    )


def test_every_capability_predicate_is_a_real_bool():
    """Not merely present — actually a bool.

    A `MagicMock` or a `None` left on a strategy is truthy or falsy by accident,
    and a capability predicate decides whether a gate runs at all.
    """
    bools = {m for m, t in typing.get_type_hints(engines.EngineStrategy).items() if t is bool}
    assert "discovers_provider_configurations" in bools, (
        "the predicate this gate was written for is no longer a bool on the Protocol"
    )

    wrong: dict[str, dict[str, object]] = {}
    for name, strategy in _strategies().items():
        bad = {
            m: getattr(strategy, m)
            for m in sorted(bools)
            if not isinstance(getattr(strategy, m, None), bool)
        }
        if bad:
            wrong[name] = bad
    assert not wrong, f"capability predicates that are not bools: {wrong}"


class TestDiscoversProviderConfigurations:
    """Which engines can enumerate a run's provider configurations (#2006)."""

    def test_terraform_discovers(self):
        # `graph` is a static walk of the configuration.
        assert engines.discovers_provider_configurations("terraform") is True

    def test_pulumi_does_not(self):
        # A Pulumi program is arbitrary code; its provider instances are built
        # at runtime, and the thing that would run it needs the credentials.
        assert engines.discovers_provider_configurations("pulumi") is False

    @pytest.mark.parametrize("engine", [None, "", "   "])
    def test_absent_means_the_default_engine(self, engine):
        # The column is nullable and the default is Terraform, which discovers.
        assert engines.discovers_provider_configurations(engine) is True

    def test_an_unknown_engine_claims_to_discover(self):
        """True, which is the conservative direction even though it is noisier.

        True sends the runner to a graph command that an unvouched-for engine
        does not have, so discovery reports `failed` and the mint is refused --
        loud, and only for a workspace that actually holds identity. False
        would hand it a token for every identity the workspace resolves on the
        strength of a registry entry nobody has reviewed, and the failure mode
        of this credential is escalation rather than absence (#1442).
        """
        assert engines.discovers_provider_configurations("nonesuch") is True

    def test_case_and_whitespace_do_not_change_the_answer(self):
        assert engines.discovers_provider_configurations("  PULUMI ") is False
