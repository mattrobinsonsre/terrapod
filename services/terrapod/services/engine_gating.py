"""Which capabilities are available, given which engines are enabled (#1429).

Terrapod carries surfaces that exist only to serve a particular engine: a
container registry for Ansible execution environments, PyPI and npm proxies for
Pulumi programs. A deployment that only runs terraform/tofu wants none of them,
and `engines.ansible` / `engines.pulumi` switch them off wholesale.

Two rules, and they live only here:

**An engine gate outranks a capability flag.** A capability serves only when its
own flag is on *and* an engine that needs it is enabled. `registry.oci.enabled`
left true does not resurrect the registry once Ansible is off — otherwise turning
an engine off would be a suggestion rather than a decision, and an operator would
have to know which capability belongs to which engine to make it stick. Which is
the knowledge this module exists to hold on their behalf.

**A shared capability survives while any of its engines is enabled.** PyPI serves
Pulumi's Python programs *and* Ansible's collection dependencies, so it goes away
only when both are off. This is the whole reason the mapping is a table rather
than a boolean, and the case worth testing.

Gating is never destructive. A gated-off capability stops serving and stops its
background work; stored images and cached artifacts stay exactly where they are
and come back untouched when the engine is re-enabled.
"""

from __future__ import annotations

from terrapod.config import settings

#: Which engines each gated capability serves. A capability is reachable while at
#: least one of its engines is enabled.
#:
#: Terraform/OpenTofu's own caches — the provider mirror, the CLI binary cache,
#: the module registry — are deliberately absent. They are what Terrapod is, not
#: an optional engine's supporting cast, and must never become gateable.
_CAPABILITY_ENGINES: dict[str, frozenset[str]] = {
    # Execution-environment images.
    "oci": frozenset({"ansible"}),
    # Pulumi Python programs; Ansible collection dependencies and `ansible-builder`.
    "pypi": frozenset({"ansible", "pulumi"}),
    # Pulumi TypeScript programs.
    "npm": frozenset({"pulumi"}),
}


def engine_enabled(engine: str) -> bool:
    """Whether an engine is offered by this deployment."""
    config = getattr(settings.engines, engine, None)
    if config is None:
        raise ValueError(f"unknown engine: {engine}")
    return bool(config.enabled)


def capability_enabled(capability: str) -> bool:
    """Whether a gated capability should serve at all.

    Answers both halves at once — the engine gate and the capability's own flag —
    so a caller cannot check one and forget the other. That is the failure this
    replaces: `registry.oci.enabled` was consulted nowhere, and the registry served
    push, pull and mirror regardless of what the operator had set.
    """
    engines = _CAPABILITY_ENGINES.get(capability)
    if engines is None:
        raise ValueError(f"unknown gated capability: {capability}")
    if not any(engine_enabled(engine) for engine in engines):
        return False
    return _capability_flag(capability)


def _capability_flag(capability: str) -> bool:
    """The capability's own `enabled` flag, independent of any engine."""
    registry = settings.registry
    if capability == "oci":
        return bool(registry.oci.enabled)
    # An ecosystem proxy needs the package cache as a whole to be on as well.
    cache = registry.package_cache
    if not cache.enabled:
        return False
    return bool(getattr(cache, capability).enabled)


def gated_capabilities() -> dict[str, bool]:
    """Every gated capability and whether it currently serves.

    For the places that need the whole picture at once — the UI's feature probe
    and the operator-facing health surface — rather than asking one at a time.
    """
    return {name: capability_enabled(name) for name in _CAPABILITY_ENGINES}
