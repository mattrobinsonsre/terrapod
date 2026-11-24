#!/usr/bin/env python3
"""Derive the fix/bug corpus for a defect retrospective. Input, not judgement.

**Why this is a committed script and not a query composed each time.** The
retrospective's whole value is that it sees *classes* across a release rather
than individual defects, and that only works if the corpus is the real
population. A query I write fresh each time is the set I happen to remember --
which is precisely how the release-scope gate failed twice, its input being my
own framing rather than a derivation.

So this script is the input. It makes no judgement: classifying a defect into a
class is reading comprehension and belongs to the agent running the
retrospective. What this does is produce the thing to read, exhaustively and the
same way every time.

**What it selects**, since the boundary matters and should not be re-litigated:

- **issues** whose title begins `fix` or `bug`, OR which carry a label
  containing `bug`. The label clause catches reports that do not follow the
  title convention.
- **pull requests** whose title begins `fix(` or `fix:`.
- both since a chosen point -- by default the previous release of the same
  series, which is what a minor's retrospective wants.

**Release-line carries are collapsed**, because the same fix carried to three
supported lines is one defect and three units of cost. Counting it as three
classes would weight carries above everything else: in one measured window they
were a third of the fix volume.

Usage:

    scripts/retro_fix_corpus.py --since v2.0.0-beta.6
    scripts/retro_fix_corpus.py --since 2026-10-13 --json

Needs `gh` authenticated. Prints a stable, readable corpus; `--json` for
machine use.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

REPO = "mattrobinsonsre/terrapod"

_ISSUE_TITLE = re.compile(r"^(fix|bug)\b|^bug\(", re.I)
_PR_TITLE = re.compile(r"^(fix|bug)[(:]", re.I)
#: A release-line suffix on an otherwise identical title. Collapsing on this is
#: what stops a three-line carry reading as three defects.
_CARRY_SUFFIX = re.compile(r"\s*\((?:v?1\.\d+|release/v\d+\.\d+)\)\s*$")


def _gh(*args: str) -> list[dict]:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        print(f"gh failed: {' '.join(args)}\n{out.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(out.stdout or "[]")


def _resolve_since(since: str) -> str:
    """A date, or a tag whose publish date we look up."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        return since
    out = subprocess.run(
        [
            "gh",
            "release",
            "view",
            since,
            "--repo",
            REPO,
            "--json",
            "publishedAt",
            "-q",
            ".publishedAt",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        print(f"could not resolve {since!r} as a date or a release tag", file=sys.stderr)
        raise SystemExit(2)
    return out.stdout.strip()[:10]


def collect(since: str) -> dict:
    issues = _gh(
        "issue",
        "list",
        "--repo",
        REPO,
        "--state",
        "all",
        "--limit",
        "600",
        "--json",
        "number,title,state,createdAt,author,labels",
    )
    prs = _gh(
        "pr",
        "list",
        "--repo",
        REPO,
        "--state",
        "all",
        "--limit",
        "600",
        "--json",
        "number,title,state,createdAt,mergedAt,author",
    )

    sel_issues = [
        i
        for i in issues
        if i["createdAt"][:10] >= since
        and (
            _ISSUE_TITLE.match(i["title"])
            or any("bug" in (lb.get("name") or "").lower() for lb in i["labels"])
        )
    ]

    groups: dict[str, list[dict]] = {}
    for p in prs:
        when = (p.get("mergedAt") or p["createdAt"])[:10]
        if when < since or not _PR_TITLE.match(p["title"]):
            continue
        key = _CARRY_SUFFIX.sub("", p["title"]).strip()
        groups.setdefault(key, []).append(p)

    return {
        "since": since,
        "issues": sorted(sel_issues, key=lambda i: i["createdAt"]),
        "pr_groups": [
            {
                "title": t,
                "numbers": [p["number"] for p in ps],
                "authors": sorted({p["author"]["login"] for p in ps}),
                "lines": len(ps),
                "when": min((p.get("mergedAt") or p["createdAt"])[:10] for p in ps),
            }
            for t, ps in sorted(
                groups.items(),
                key=lambda kv: min((p.get("mergedAt") or p["createdAt"]) for p in kv[1]),
            )
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--since", required=True, help="a date (YYYY-MM-DD) or a release tag, e.g. v2.0.0-beta.6"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data = collect(_resolve_since(args.since))

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    n_pr = sum(g["lines"] for g in data["pr_groups"])
    print(f"Fix/bug corpus since {data['since']}\n")
    print(f"  {len(data['issues'])} issue(s)")
    print(f"  {n_pr} fix PR(s), {len(data['pr_groups'])} distinct after collapsing carries\n")

    print("ISSUES")
    for i in data["issues"]:
        print(
            f"  #{i['number']:<5} {i['createdAt'][:10]}  {i['state']:<6}  "
            f"{i['author']['login']:<17}  {i['title']}"
        )

    print("\nPRs (carries collapsed)")
    for g in data["pr_groups"]:
        nums = ",".join(f"#{n}" for n in g["numbers"])
        carry = f"  [{g['lines']} lines]" if g["lines"] > 1 else ""
        print(f"  {nums}  {g['when']}  {','.join(g['authors']):<17}{carry}  {g['title']}")

    print(
        "\nThe classification is the agent's, not this script's. List each class's"
        '\nMEMBERS, not only a count: a keyword matcher put "na*vigate*" in the gate'
        "\nclass and counted a retry-shaped title that was about limits."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
