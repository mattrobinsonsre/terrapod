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


def mcp_tool_count() -> int:
    d = json.loads((ROOT / "mcp/internal/mcpserver/tool_catalogue.json").read_text())
    return len(d)


def main() -> int:
    res, ds = provider_counts()
    real_locales, _ = locale_counts()
    tools = mcp_tool_count()

    # (human name, expected, regex). Each pattern is deliberately specific:
    # a loose one would match unrelated prose and make the gate untrustworthy.
    checks = [
        ("provider resources", res, re.compile(r"(\d+)\s+`?terrapod_\*?`?\s+resources\b")),
        ("provider data sources", ds, re.compile(r"(\d+)\s+data sources?\b")),
        ("UI languages", real_locales, re.compile(r"(\d+)\s+languages\b")),
        ("MCP tools", tools, re.compile(r"(\d+)\s+`?terrapod_\*?`?\s+tools\b")),
    ]

    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()

    problems, seen = [], 0
    for f in files:
        for i, line in enumerate((ROOT / f).read_text(errors="replace").splitlines(), 1):
            for name, expected, pat in checks:
                for m in pat.finditer(line):
                    seen += 1
                    if int(m.group(1)) != expected:
                        problems.append(
                            f"{f}:{i}: says {m.group(1)} {name}, code has {expected}"
                        )

    print(f"  ground truth: {res} resources, {ds} data sources, "
          f"{real_locales} languages, {tools} MCP tools")
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
