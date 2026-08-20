#!/usr/bin/env python3
"""Flag sentences that describe a peer project by what it lacks (#1389).

AGENTS.md is unambiguous: peers are described by what they ARE, never by what
they are missing. Eight violations turned up in three files, which is why this
is a lint rather than a review item.

It does NOT fail the build, deliberately. "Terrakube has no X" and "Terrapod
needs no X" are the same words in different frames, and only a human can tell a
put-down from a factual boundary. A gate that blocks on that would either be
wrong often or tuned until it caught nothing. This prints and exits 0; the
release audit's peer-respect step is where the judgement happens.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PEERS = re.compile(
    r"\b(Terrakube|Atlantis|Spacelift|env0|Scalr|Digger|Terrateam|Burrito|tf-controller)\b"
)

# Framings that describe a thing by its absence.
ABSENCE = re.compile(
    r"\b(no UI|lacks?|only does|only offers|only supports|does ?n[o']t (?:have|support|offer|do)"
    r"|not present in|listed for completeness|missing|has no|there is no|without a"
    r"|unlike|whereas .{0,40}(?:cannot|can't)|limited to|falls short|fails to)\b",
    re.I,
)

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def sentences(text: str):
    for s in re.split(r"(?<=[.!?])\s+", text):
        yield s


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()

    flagged = []
    for f in files:
        marker = None
        for i, line in enumerate((ROOT / f).read_text(errors="replace").splitlines(), 1):
            m = FENCE_RE.match(line)
            if m:
                run, info = m.group(1), m.group(2).strip()
                if marker is None:
                    if not ("`" in info and run[0] == "`"):
                        marker = run
                elif run[0] == marker[0] and len(run) >= len(marker) and info == "":
                    marker = None
                continue
            if marker is not None:
                continue
            for s in sentences(line):
                if PEERS.search(s) and ABSENCE.search(s):
                    flagged.append((f, i, s.strip()[:150]))

    print(f"  {len(files)} docs scanned for absence-framing near a peer's name")
    if flagged:
        print(f"\n  {len(flagged)} sentence(s) for human review "
              f"(not a failure — check each reads as a boundary, not a put-down):\n")
        for f, i, s in flagged:
            print(f"    {f}:{i}\n      {s}")
    else:
        print("  Nothing to review — no peer is described by an absence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
