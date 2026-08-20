#!/usr/bin/env python3
"""Docs that describe work as pending must not cite a CLOSED issue (#1389).

Axis 2 of the corpus audit, and the other gate that needs no judgement. The
failure it catches is specific and recurring: a feature ships, its issue
closes, and three docs go on calling it future work. A reader believes the doc.

Only *forward-looking* references are checked. A doc saying "fixed in #1234" or
"see #1234 for why" is history and a closed issue is correct there — flagging
those would make the gate noise. The trigger is a phrase that promises the work
is still to come.

Requires `gh` (already a dependency of the release tooling) and network access,
so it runs as a scheduled/CI check rather than in the pre-commit path.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "mattrobinsonsre/terrapod"

# Phrases that promise the work has NOT happened yet. Deliberately narrow: each
# one has to be wrong for the sentence to be wrong.
PENDING = re.compile(
    r"(?:"
    r"tracked (?:in|by)|planned|coming|will be|to be |not yet|follow-?up"
    # No bare "future"/"remaining": they usually modify a NOUN the doc is
    # describing ("applies to all future workspaces (#318)"), not the work.
    r"|deferred|pending|upcoming|roadmap"
    r")\b[^.\n]{0,90}?#(\d+)",
    re.I,
)

# "Follow-up chat" is the NAME of a shipped feature, so the word there is a
# noun phrase rather than a promise. Neutralise those before matching.
FEATURE_NAMES = re.compile(
    r"follow-?up (?=chat|turn|conversation|thread|message|prompt|repl(?:y|ies))",
    re.I,
)

# History, not a promise. "Fixed in #123" SHOULD cite a closed issue, and
# flagging it would make the gate noise the first time someone read it.
HISTORY = re.compile(
    r"\b(?:fixed|shipped|landed|closed|delivered|implemented|added|introduced|resolved)"
    r"\s+(?:in|by|via)\b",
    re.I,
)


def refs() -> dict[int, list[str]]:
    """Issue number -> the places that speak of it as pending."""
    out: dict[int, list[str]] = {}
    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    for f in files:
        p = ROOT / f
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        # Scan PARAGRAPHS, not lines. The corpus is hard-wrapped at ~80 columns,
        # so "Extending coverage is tracked in\n[#709](...)" puts the marker and
        # the reference on different lines — a line-based scan misses most real
        # cases, which is exactly how #709 slipped past the first version.
        para: list[tuple[int, str]] = []

        def flush(para=para, f=f, out=out):
            if not para:
                return
            text = " ".join(t for _, t in para)
            first = para[0][0]
            para.clear()
            # Headings NAME things ("## Follow-up chat (#463)"); prose makes
            # claims. Only prose is a promise worth checking.
            if text.lstrip().startswith("#") or HISTORY.search(text):
                return
            for m in PENDING.finditer(FEATURE_NAMES.sub("", text)):
                n = int(m.group(1))
                out.setdefault(n, []).append(f"{f}:{first}: {text.strip()[:130]}")

        for i, line in enumerate(lines, 1):
            if not line.strip():
                flush()
                continue
            para.append((i, line))
        flush()
    return out


def states(numbers: list[int]) -> dict[int, str]:
    """One gh call per issue, but only for issues actually referenced."""
    found: dict[int, str] = {}
    for n in numbers:
        r = subprocess.run(
            ["gh", "issue", "view", str(n), "--repo", REPO, "--json", "state,title"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            # A PR number, or an issue in another repo — not something to fail on.
            continue
        d = json.loads(r.stdout)
        found[n] = f"{d['state']}\t{d['title']}"
    return found


def main() -> int:
    found = refs()
    if not found:
        print("PASS — no forward-looking issue references.")
        return 0
    st = states(sorted(found))
    stale = []
    for n, sites in sorted(found.items()):
        info = st.get(n)
        if info and info.split("\t")[0] == "CLOSED":
            stale.append((n, info.split("\t")[1], sites))

    print(f"  {len(found)} issue(s) referenced as pending; {len(st)} resolved")
    if stale:
        print(f"\nFAIL — {len(stale)} closed issue(s) described as pending work:\n")
        for n, title, sites in stale:
            print(f"  #{n} is CLOSED — {title}")
            for s in sites:
                print(f"      {s}")
        return 1
    print("PASS — every issue described as pending is still open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
