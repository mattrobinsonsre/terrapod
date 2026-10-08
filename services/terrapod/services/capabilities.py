"""Whether an optional capability serves, from its own flag alone (#1986).

Terrapod carries surfaces that exist to serve one engine: a container registry
for Ansible execution environments, PyPI, npm, Go, NuGet and Galaxy proxies for
Pulumi programs and Ansible collections. Each has an `enabled` flag an operator
can turn off, and **this module is the only thing that reads them.**

**There is no engine on/off switch, and that is deliberate.** This module used to
answer two questions at once — is an engine enabled, and is this capability's own
flag on — because `engines.{ansible,pulumi}.enabled` outranked the flag. That
switch is withdrawn (#1986): the platform should just work, so no operator has to
decide which engines they are allowed to use, and a terraform/tofu-only
deployment pays nothing because nothing surfaces what it does not contain rather
than because it switched something off.

**What survived is the half that was load-bearing.** The engine gate existed for
one release; the flag read predates it and fixes a real defect, recorded here so
nobody deletes it as leftovers: the OCI registry originally shipped with an
`enabled` flag **that nothing in the code read**, so `enabled: false` disabled two
scheduled tasks while `/v2/` happily served push, pull and mirror. Every flag
below therefore has exactly one reader and a test proving the read
(`test_capabilities.py::TestEveryFlagIsRead`), derived from this table rather than
listed, so a new capability cannot arrive unread.

Turning a capability off is never destructive. It stops serving and stops its
background work; stored images and cached artifacts stay where they are and come
back untouched when it is turned back on.
"""

from __future__ import annotations

from terrapod.config import settings

#: Every capability whose `enabled` flag this module reads.
#:
#: Terraform/OpenTofu's own caches — the provider mirror, the CLI binary cache,
#: the module registry — are deliberately absent. They are what Terrapod is, not
#: an optional engine's supporting cast, and must never become gateable. (They
#: have their own `enabled` flags, read at their own call sites.)
CAPABILITIES: tuple[str, ...] = (
    # Execution-environment images.
    "oci",
    # Pulumi Python programs; Ansible collection dependencies and `ansible-builder`.
    "pypi",
    # Pulumi TypeScript programs.
    "npm",
    # Ansible collections.
    "galaxy",
    # Pulumi resource and language plugins.
    "pulumi",
    # A Pulumi program is written in a real language, so its dependencies come
    # from that language's registry.
    "go",
    "nuget",
)


def capability_enabled(capability: str) -> bool:
    """Whether an optional capability should serve at all.

    Raises on an unknown name rather than returning False. A typo that read as
    "off" would mount nothing and look like a deliberate configuration, which is
    the quietest possible way to lose a surface.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability: {capability}")
    registry = settings.registry
    if capability == "oci":
        return bool(registry.oci.enabled)
    # An ecosystem proxy needs the package cache as a whole to be on as well.
    cache = registry.package_cache
    if not cache.enabled:
        return False
    return bool(getattr(cache, capability).enabled)


def gated_capabilities() -> dict[str, bool]:
    """Every optional capability and whether it currently serves.

    The whole picture at once, for anything that needs it rather than asking one
    at a time — a UI feature probe or an operator-facing health surface would.
    **Nothing in the product reads it yet**, said plainly because this module's
    whole subject is a flag with no reader: `test_capabilities.py` is its only
    caller, where it is how the derived tests enumerate the table.
    """
    return {name: capability_enabled(name) for name in CAPABILITIES}
