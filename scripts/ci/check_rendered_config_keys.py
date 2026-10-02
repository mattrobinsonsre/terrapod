#!/usr/bin/env python3
"""Every key the chart renders into `config.yaml` must be one `Settings` accepts.

Run from the repo root with the rendered `helm template` output on stdin.

**Why this is a CI script and not a pytest.** The check needs BOTH a helm binary
and the committed key snapshot. `docker/Dockerfile.test` deliberately ships no
helm binary ("Chart only (values + templates); no helm binary is invoked in the
Python image"), and CI runs `tests/helm` inside that image — so a pytest that
shells out to helm can only ever skip there, which is how a gate becomes
decorative. `helm-smoke` has helm, and the house pattern is to grep the RENDERED
output in that job. This is that pattern with a parser instead of a grep, because
"is every key known" is not a grep.

It reads `services/tests/config/config_key_contract.json` rather than importing
`Settings`, so it needs no Python dependencies at all — the snapshot IS the
committed record of what `Settings` accepts, and `test_config_contract.py` is
what keeps the two in step.

The failure it exists to catch has happened: a `| default true` cleanup deleted
the render line for `vcs.require_connection_authorization` as collateral. The key
stayed in `values.yaml` and in the values snapshot, so every gate stayed green
while an operator's explicit `false` silently stopped reaching the pod — and they
would then have met a 403 whose own text told them to set the key that had
stopped working. The reverse has happened too: four keys carried in from the 2.0
line, modelled nowhere, shipped to every deployment and silently dropped.
"""

from __future__ import annotations

import json
import pathlib
import sys

#: Keys the chart renders on purpose that `Settings` does not model. Each entry
#: is a claim that the API is MEANT to ignore it, so each needs a reason —
#: otherwise this becomes the place dead config goes to hide.
RENDERED_BUT_NOT_SETTINGS: dict[str, str] = {}


def extract_config_yaml(rendered: str) -> list[str]:
    """The lines of the `config.yaml` literal block, dedented.

    Deliberately a text scan rather than a YAML parse: this runs on a bare
    runner with no third-party packages, and a missing `import yaml` would turn
    the gate into an error nobody reads. The block is a plain nested mapping
    under `config.yaml: |`, which is all this needs to understand.
    """
    out: list[str] = []
    indent: int | None = None
    for line in rendered.splitlines():
        if indent is None:
            if line.strip() in ("config.yaml: |", "config.yaml: |-"):
                indent = -1  # next non-blank line sets the real indent
            continue
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip())
        if indent == -1:
            indent = lead
        if lead < indent:
            break  # dedented out of the block
        out.append(line[indent:])
    return out


def leaf_paths(block: list[str]) -> list[str]:
    """Dotted paths of every scalar leaf, from indentation alone.

    A key whose value is empty on its own line is a PARENT and contributes no
    leaf; a key with a value after the colon is a leaf. A list item (`- `) is
    part of its parent's value, not a key.
    """
    stack: list[tuple[int, str]] = []
    leaves: list[str] = []
    for line in block:
        if line.lstrip().startswith(("#", "-")):
            continue
        lead = len(line) - len(line.lstrip())
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key or key.startswith('"') and not key.endswith('"'):
            continue
        while stack and stack[-1][0] >= lead:
            stack.pop()
        path = ".".join([p for _, p in stack] + [key])
        if value.strip():
            leaves.append(path)
        else:
            stack.append((lead, key))
    return leaves


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    snap = json.loads(
        (root / "services" / "tests" / "config" / "config_key_contract.json").read_text()
    )
    known = {k for k in snap if not k.startswith("[runners.yaml]")}

    block = extract_config_yaml(sys.stdin.read())
    if not block:
        print("FAIL: no `config.yaml` block in the rendered output", file=sys.stderr)
        return 1

    paths = leaf_paths(block)
    if not paths:
        print("FAIL: the `config.yaml` block yielded no keys", file=sys.stderr)
        return 1

    # A path is accepted when it is modelled, or sits UNDER something modelled —
    # a dict- or list-valued field's contents are that field's own business, so
    # `auth.sso.oidc.0.name` is covered by `auth.sso.oidc`.
    # The snapshot spells a list-valued field's children `vault.instances[].address`,
    # so the PARENT path `vault.instances` is never an exact entry. Strip the marker
    # and compare on the bare path, and accept a parent that any entry sits under.
    bare = {k.replace("[]", "") for k in known}
    prefixes = {k.rsplit(".", 1)[0] for k in bare if "." in k}

    def accepted(p: str) -> bool:
        if p in bare or p in prefixes or p in RENDERED_BUT_NOT_SETTINGS:
            return True
        parts = p.split(".")
        return any(".".join(parts[:i]) in bare for i in range(1, len(parts)))

    unknown = sorted({p for p in paths if not accepted(p)})
    if unknown:
        print(
            "FAIL: the chart renders config the API does not model. It is shipped "
            "to every deployment and silently dropped — and because "
            "values.schema.json is additionalProperties:false, an operator cannot "
            "even override it:",
            file=sys.stderr,
        )
        for u in unknown:
            print(f"  {u}", file=sys.stderr)
        print(
            "\nRemove the render line, or add a Settings field. If it really is "
            "meant to be ignored, add it to RENDERED_BUT_NOT_SETTINGS with a reason.",
            file=sys.stderr,
        )
        return 1

    print(f"ok: {len(paths)} rendered config keys, all modelled by Settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
