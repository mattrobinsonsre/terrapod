#!/usr/bin/env python3
"""Countable claims in the docs must match the code (#1389).

Axis 1's mechanisable half. "N resources", "N data sources", "N languages" and
"N tools" drift silently: the number is written once, the code grows, and
nothing complains — AGENTS.md said 7 data sources while the code and five other
docs said 9, which is exactly the shape this catches.

Each ground truth is derived from the artefact that defines it, never from
another doc, so the docs cannot agree with each other and all be wrong.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def provider_counts() -> tuple[int, int]:
    """Resources and data sources, from the provider's own registration."""
    s = (ROOT / "provider/internal/provider/provider.go").read_text()

    def block(marker: str) -> str:
        i = s.index(marker)
        return s[i : s.index("\n}", i)]

    return (
        len(re.findall(r"New\w+", block("func (p *terrapodProvider) Resources"))),
        len(re.findall(r"New\w+", block("func (p *terrapodProvider) DataSources"))),
    )


def locale_counts() -> tuple[int, int]:
    """(real, novelty). The docs advertise the real ones as "languages" —
    Klingon and the en-x-* dialects are deliberately not counted as such."""
    s = (ROOT / "web/src/i18n/config.ts").read_text()
    locs = re.findall(r"'([^']+)'", re.search(r"locales\s*=\s*\[(.*?)\]", s, re.S).group(1))
    novelty = [x for x in locs if x.startswith("en-x-") or x == "tlh"]
    return len(locs) - len(novelty), len(novelty)


def capability_counts() -> tuple[int, int]:
    """(capabilities, resource kinds), from the capability module's own source.

    Parsed with `ast` rather than matched with a regex, and rather than imported.
    Not imported because this job runs against a bare checkout with the services
    package uninstalled; not regexed because `ruff format` wraps a long
    assignment onto its own line, and a line-anchored pattern silently missed
    `PLATFORM_REGISTRY_ADMIN` that way -- 49 against the real 50, which is the
    quiet direction for a gate to be wrong in.

    The counts are advertised because the granularity is the feature: an
    evaluator asking "can I grant plan without apply" wants a number, and a
    number in prose with nothing deriving it goes stale the first time a
    capability is added.
    """
    import ast

    tree = ast.parse((ROOT / "services/terrapod/auth/capabilities.py").read_text())
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not all(isinstance(x, ast.Name) and x.id.isupper() for x in targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            values.add(node.value.value)
    pairs = {v for v in values if ":" in v}
    return len(pairs), len({v.split(":", 1)[0] for v in pairs})


def mcp_tool_count() -> int:
    d = json.loads((ROOT / "mcp/internal/mcpserver/tool_catalogue.json").read_text())
    return len(d)


def main() -> int:
    res, ds = provider_counts()
    real_locales, _ = locale_counts()
    tools = mcp_tool_count()
    caps, kinds = capability_counts()

    # (human name, expected, regex). Each pattern is deliberately specific:
    # a loose one would match unrelated prose and make the gate untrustworthy.
    checks = [
        ("provider resources", res, re.compile(r"(\d+)\s+`?terrapod_\*?`?\s+resources\b")),
        ("provider data sources", ds, re.compile(r"(\d+)\s+data sources?\b")),
        ("UI languages", real_locales, re.compile(r"(\d+)\s+languages\b")),
        ("MCP tools", tools, re.compile(r"(\d+)\s+`?terrapod_\*?`?\s+tools\b")),
        # Deliberately requires the literal word. A first attempt also accepted
        # "N of them", which matched "75 of them loop items" in llms.txt -- the
        # loose-pattern failure this file's own comment warns about, found by
        # the gate immediately disagreeing with unrelated prose.
        ("RBAC capabilities", caps, re.compile(r"(\d+)\s+capabilities\b")),
        ("RBAC resource kinds", kinds, re.compile(r"(\d+)\s+resource kinds\b")),
    ]

    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    problems, seen = [], 0
    for f in files:
        for i, line in enumerate((ROOT / f).read_text(errors="replace").splitlines(), 1):
            for name, expected, pat in checks:
                for m in pat.finditer(line):
                    seen += 1
                    if int(m.group(1)) != expected:
                        problems.append(f"{f}:{i}: says {m.group(1)} {name}, code has {expected}")

    print(f"  ground truth: {caps} capabilities over {kinds} resource kinds")
    print(
        f"  ground truth: {res} resources, {ds} data sources, "
        f"{real_locales} languages, {tools} MCP tools"
    )
    print(f"  {seen} countable claim(s) checked across {len(files)} docs")
    if problems:
        print(f"\nFAIL — {len(problems)} count(s) disagree with the code:\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print("PASS — every countable claim matches the code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
