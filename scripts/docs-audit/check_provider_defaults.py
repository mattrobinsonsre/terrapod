#!/usr/bin/env python3
"""The provider's hard-coded server defaults must agree with the server's.

The provider carries `stringdefault.StaticString(...)` copies of values the
server also defines -- the engine version being the one that matters, because it
decides what every workspace a rule creates will run.

Nothing kept the two in step. `provider/internal/provider/schema.golden` records
each attribute's required/optional/computed/sensitive/type and **not its
default**, which `internal/provider/auto_apply_default_test.go` already says in
its own comment. So the provider's `terraform_version` default sat at `1.11`
while the server's was `1.12`, through a whole release, and the first anyone
noticed was a review.

A golden would only catch a CHANGE. The property that actually matters is
AGREEMENT, which is what this checks: a drift is invisible precisely because
both sides look internally consistent.

Lives here rather than in the pytest tiers because `docker/Dockerfile.test`
does not copy `provider/`, so a Python test there cannot read it. The
docs-audit job runs against a full checkout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Provider resource -> the attribute whose default mirrors a server setting.
#: `workspace` is deliberately absent: it carries no version default at all, so
#: an unset value inherits the server's rather than restating it, which is the
#: shape that cannot drift. Adding a default there should fail this check.
MIRRORED = {
    "autodiscovery_rule": "terraform_version",
}

#: The server's own default, and where an operator reads it.
SERVER_SETTING = "default_terraform_version"


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


def _provider_default(resource: str, attribute: str) -> str | None:
    """The `StaticString` on that attribute, or None if it carries no default."""
    src = (ROOT / f"provider/internal/resources/{resource}/resource.go").read_text()
    m = re.search(
        rf'"{attribute}": schema\.StringAttribute\{{(.*?)\n\t\t\t\}},',
        src,
        re.S,
    )
    if not m:
        raise SystemExit(
            f"could not find the `{attribute}` attribute block in {resource}/resource.go"
        )
    d = re.search(r'stringdefault\.StaticString\("([^"]*)"\)', m.group(1))
    return d.group(1) if d else None


def main() -> int:
    failures: list[str] = []

    server = _server_default()
    chart = _chart_default()
    if server != chart:
        failures.append(
            f"config.py says {SERVER_SETTING}={server!r} and values.yaml says {chart!r}. "
            "An operator reading the chart gets a different answer from the one the API runs on."
        )

    for resource, attribute in MIRRORED.items():
        got = _provider_default(resource, attribute)
        if got is None:
            failures.append(
                f"provider {resource}.{attribute} carries no default, but this check "
                f"expects it to mirror {SERVER_SETTING}. If it was deliberately "
                "removed so the value is inherited, drop it from MIRRORED."
            )
        elif got != server:
            failures.append(
                f"provider {resource}.{attribute} defaults to {got!r} and the server "
                f"defaults to {server!r}. Every workspace that rule creates would run "
                f"{got} while a workspace created any other way runs {server}, and a "
                "practitioner who never set the attribute sees a plan diff on upgrade. "
                "schema.golden records types, not defaults, so nothing else catches this."
            )

    # A guard that loses its subject must fail, not pass.
    if not MIRRORED:
        failures.append("MIRRORED is empty -- this check would pass vacuously")

    if failures:
        print("FAIL: provider/server default drift\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(
        f"OK: {SERVER_SETTING}={server!r} agrees across config.py, values.yaml "
        f"and {len(MIRRORED)} provider attribute(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
