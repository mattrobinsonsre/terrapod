#!/usr/bin/env python3
"""The engine version has one source of truth, and the provider is not it.

Two properties, and the second is the one that keeps the first true.

**config.py and values.yaml must agree** on `default_terraform_version`. They
are the two places an answer comes from -- what the API runs on, and what an
operator reads -- and a disagreement is invisible because each is internally
consistent.

**No provider attribute may state an engine version default.** This is how the
drift is prevented rather than detected: every engine-version attribute is
`Optional + Computed` with no `stringdefault.StaticString`, so an unset value
is supplied by the server instead of being restated in the provider. Restating
one is exactly what went wrong before #1559 -- the provider's
`terraform_version` default sat at `1.11` while the server's was `1.12` for a
whole release, and every workspace a rule created ran the wrong version while
both sides looked internally consistent.

Nothing else catches that. `provider/internal/provider/schema.golden` records
each attribute's required/optional/computed/sensitive/type and **not** its
default -- `internal/provider/auto_apply_default_test.go` says so in its own
comment -- and a golden would only catch a *change* anyway, where the property
that matters is *agreement*.

The subjects are **derived from the tree**, not listed here, so a new resource
with an engine-version attribute is covered the day it lands rather than the
day someone remembers to add it.

Lives here rather than in the pytest tiers because `docker/Dockerfile.test`
does not copy `provider/`, so a Python test there cannot read it. The
docs-audit job runs against a full checkout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The server's own default, and where an operator reads it.
SERVER_SETTING = "default_terraform_version"

#: The attribute names that carry an engine version. A `version_pin` or a
#: `version_id` is a different thing -- a module pin, a row id -- and is
#: deliberately out of scope.
ENGINE_VERSION_ATTRS = frozenset(
    {"engine_version", "terraform_version", "ansible_version", "terragrunt_version"}
)

#: Below this the derivation has lost its subjects and the check would pass
#: vacuously. Eight attributes across two resources at the time of writing, so
#: this leaves headroom for a reformat without tolerating the pattern breaking.
MIN_ATTRS = 6


def _server_default() -> str:
    """From `config.py`, which is what the API actually runs on."""
    src = (ROOT / "services/terrapod/config.py").read_text()
    m = re.search(rf'^\s+{SERVER_SETTING}: str = Field\(default="([^"]+)"', src, re.M)
    if not m:
        raise SystemExit(
            f"could not find `{SERVER_SETTING}` in config.py -- this check has lost "
            "its subject and would pass vacuously"
        )
    return m.group(1)


def _chart_default() -> str:
    src = (ROOT / "helm/terrapod/values.yaml").read_text()
    m = re.search(rf'^\s+{SERVER_SETTING}:\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit(f"could not find `{SERVER_SETTING}` in values.yaml")
    return m.group(1)


def _engine_version_attrs() -> list[tuple[str, str, str | None]]:
    """Every engine-version attribute in the provider, with its default if any.

    Returns `(resource, attribute, default)`, where `default` is the
    `StaticString` literal or None when the attribute states none.
    """
    found: list[tuple[str, str, str | None]] = []
    for path in sorted(ROOT.glob("provider/internal/resources/*/resource.go")):
        src = path.read_text()
        for m in re.finditer(
            r'"([a-z_]+)": schema\.StringAttribute\{(.*?)\n\t\t\t\},', src, re.S
        ):
            name, body = m.group(1), m.group(2)
            if name not in ENGINE_VERSION_ATTRS:
                continue
            d = re.search(r'stringdefault\.StaticString\("([^"]*)"\)', body)
            found.append((path.parent.name, name, d.group(1) if d else None))
    return found


def main() -> int:
    failures: list[str] = []

    server = _server_default()
    chart = _chart_default()
    if server != chart:
        failures.append(
            f"config.py says {SERVER_SETTING}={server!r} and values.yaml says {chart!r}. "
            "An operator reading the chart gets a different answer from the one the API runs on."
        )

    attrs = _engine_version_attrs()
    if len(attrs) < MIN_ATTRS:
        failures.append(
            f"found only {len(attrs)} engine-version attribute(s) in the provider, "
            f"expected at least {MIN_ATTRS}. Either the attribute-block pattern no "
            "longer matches or the resources moved -- either way this check is no "
            "longer reading what it claims to."
        )

    for resource, attribute, default in attrs:
        if default is not None:
            failures.append(
                f"provider {resource}.{attribute} states a default of {default!r}. "
                "Engine versions are supplied by the server, so a provider-side "
                "default is a second source of truth that drifts silently: it sat at "
                "1.11 against a server default of 1.12 for a whole release. Make the "
                "attribute Optional + Computed with no default, or -- if a "
                "provider-side default is genuinely wanted -- add it to this check as "
                "a mirrored value and assert it equals the server's."
            )

    if failures:
        print("FAIL: engine version default drift\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(
        f"OK: {SERVER_SETTING}={server!r} agrees between config.py and values.yaml, "
        f"and {len(attrs)} provider engine-version attribute(s) state no default of their own"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
