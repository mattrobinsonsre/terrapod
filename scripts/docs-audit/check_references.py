#!/usr/bin/env python3
"""Things the docs name must exist (#1389).

The rest of axis 1: a `make` target, a Helm value or a repository path that a
doc tells you to use should resolve to something real. A reader who types it
and gets "No rule to make target" learns not to trust the page.

Three classes, each with an authoritative source:

  * `make X`            -> a target in the Makefile
  * Helm values         -> a key path in helm/terrapod/values.yaml
  * repo paths          -> a file or directory that exists

Endpoint paths are deliberately NOT checked here: the route contract stores
FastAPI templates ({id}) while docs write concrete examples, and normalising
between them reliably is a bigger job than its yield. The route contract test
already fails on route removal, which is the risk that matters.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
MAKE = re.compile(r"`make\s+([a-z][a-z0-9-]{2,})`")
# A backticked dotted path that looks like a Helm value: at least two segments,
# lowercase/underscore, no spaces. Anchored on the roots the chart actually has
# so that arbitrary dotted prose is not dragged in.
HELM = re.compile(r"`((?:api\.config|api|web|listener|runners|registry|storage|postgresql|redis|ingress|webhookIngress)\.[a-zA-Z0-9_.]+)`")
# Repo-relative paths in backticks: must contain a slash and look like a file.
REPO_PATH = re.compile(r"`((?:services|web|helm|docs|scripts|provider|go-terrapod|migrate|publish|query|mcp|e2e|alembic|docker|pentest|loadtest|pricegen)/[A-Za-z0-9_./-]+)`")


# Keys whose children are operator-supplied, so an unknown leaf is data rather
# than a typo.
# Dotted names that collide with a chart root but are something else entirely.
NOT_CHART_KEYS = {"redis.asyncio"}  # the Python client library

FREEFORM = {
    "annotations", "labels", "podAnnotations", "podLabels", "selectorLabels",
    "nodeSelector", "tolerations", "affinity", "topologySpreadConstraints",
    "podSecurityContext", "securityContext", "resources", "extraEnv",
    "extraVolumes", "extraVolumeMounts", "oidc", "saml", "claims_to_roles",
}


def outside_fences(path: Path):
    marker = None
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        m = FENCE_RE.match(line)
        if m:
            run, info = m.group(1), m.group(2).strip()
            if marker is None:
                if not ("`" in info and run[0] == "`"):
                    marker = run
                    continue
            elif run[0] == marker[0] and len(run) >= len(marker) and info == "":
                marker = None
                continue
        if marker is None:
            yield i, line


def make_targets() -> set[str]:
    s = (ROOT / "Makefile").read_text()
    return set(re.findall(r"^([a-zA-Z0-9_-]+):", s, re.M))


def schema_keys() -> set[str]:
    """Keys the values schema declares — the authority on what an operator may
    SET, which is broader than what values.yaml happens to show a default for
    (redis.existingSecret is schema-declared but has no default)."""
    import json

    out: set[str] = set()

    def walk(node, prefix=""):
        for k, v in (node.get("properties") or {}).items():
            out.add(f"{prefix}{k}")
            if isinstance(v, dict):
                walk(v, f"{prefix}{k}.")

    walk(json.loads((ROOT / "helm/terrapod/values.schema.json").read_text()))
    return out


def template_keys() -> set[str]:
    """Keys the chart templates actually read.

    Template-only keys are real: `api.config.vcs.tmpdir` has no default in
    values.yaml and no schema entry (api.config is unconstrained), but the
    ConfigMap renders it with `| default`, so setting it works and documenting
    it is correct.
    """
    out: set[str] = set()
    for f in (ROOT / "helm/terrapod/templates").rglob("*.yaml"):
        out.update(re.findall(r"\.Values\.([A-Za-z0-9_.]+)", f.read_text(errors="replace")))
    return out


def helm_keys() -> set[str]:
    """Every dotted key path in values.yaml, from indentation alone.

    Deliberately not a YAML parse: the file is full of Go templating in
    comments and the structure is all that matters here.
    """
    keys: set[str] = set()
    stack: list[tuple[int, str]] = []
    for line in (ROOT / "helm/terrapod/values.yaml").read_text().splitlines():
        m = re.match(r"^(\s*)([a-zA-Z_][a-zA-Z0-9_-]*):", line)
        if not m:
            continue
        indent, key = len(m.group(1)), m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        keys.add(".".join(k for _, k in stack))
    return keys


def main() -> int:
    # Three authorities, union: a default in values.yaml, a declaration in the
    # schema, or a read in a template. Any one of them makes the key real.
    targets = make_targets()
    hkeys = helm_keys() | schema_keys() | template_keys()
    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()

    problems: list[str] = []
    counts = {"make": 0, "helm": 0, "path": 0}

    for f in files:
        visible = list(outside_fences(ROOT / f))
        window = {ln: " ".join(t for _, t in visible[max(0, i - 1) : i + 2])
                  for i, (ln, _) in enumerate(visible)}
        for lineno, line in visible:
            for t in MAKE.findall(line):
                counts["make"] += 1
                if t not in targets:
                    problems.append(f"{f}:{lineno}: `make {t}` — no such Makefile target")
            for k in HELM.findall(line):
                counts["helm"] += 1
                # A leaf under a free-form map (annotations, labels, nodeSelector)
                # is operator data, not a chart key.
                last = k.rsplit(".", 1)[-1]
                if last in {"io", "org", "com", "net", "dev", "local"}:
                    continue  # a hostname, not a chart key
                if last in {"yaml", "yml", "json", "py", "ts", "go", "tf", "md"}:
                    continue  # a filename (runners.yaml, ingress.yaml)
                if k in NOT_CHART_KEYS:
                    continue
                candidates = (k, f"api.config.{k}")
                # Exact match, or a leaf under a genuinely free-form map where
                # the operator supplies their own keys. Allowing ANY leaf under
                # ANY known parent (the first version) meant a typo in
                # api.config.registry.* sailed through, which is most of what
                # this class exists to catch.
                if any(c in hkeys for c in candidates):
                    continue
                if any(seg in FREEFORM for c in candidates for seg in c.split(".")):
                    continue
                problems.append(f"{f}:{lineno}: `{k}` — not a key in values.yaml")
            for p in REPO_PATH.findall(line):
                counts["path"] += 1
                if re.search(r"must \*?\*?not\*?\*? exist|does not exist|absence of",
                             window[lineno], re.I):
                    continue
                if not (ROOT / p).exists():
                    problems.append(f"{f}:{lineno}: `{p}` — no such file or directory")

    print(f"  checked: {counts['make']} make targets, {counts['helm']} helm values, "
          f"{counts['path']} repo paths")
    if problems:
        print(f"\nFAIL — {len(problems)} reference(s) do not resolve:\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print("PASS — every referenced target, value and path exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
