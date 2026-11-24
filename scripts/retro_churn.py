#!/usr/bin/env python3
"""Do the lines we add survive? The leading indicator of the instability tax.

**Why this metric and not another.** Published research on agent-authored code
converges on churn rather than on defect counts: a study of roughly 110,000
open-source pull requests across five coding agents found human additions
persisting at about double the rate of agent-authored lines at every interval
measured, and a separate study found AI-generated code more likely to be
revisited within 30 and 90 days. DORA's position is adjacent -- higher AI
adoption raises throughput and instability together, which it prices as an
"instability tax".

This project's own review-remediation rate is the same phenomenon seen from
inside: a third of one window's fix PRs were remediation OF a review. Churn is
that, measured directly and early, rather than inferred from how much rework we
happen to notice.

**What it measures.** For a window ending `--before` and starting `--window`
days earlier, it takes the lines ADDED in that window and asks what fraction
still exist at HEAD. `git log -L` would be exact and is far too slow over a tree
this size, so this uses the cheap approximation that is still directionally
right: for each file touched in the window, compare the lines added then against
the file's content now.

**It is deliberately not a gate.** There is no threshold worth failing a build
on, the number is noisy on small windows, and a metric that blocks a merge
becomes a metric people game. It is a trend to read monthly: if survival
degrades, the retrospective and the simplification pass are not working, and we
know before the defect count tells us.

Usage:

    scripts/retro_churn.py                       # last 30 days, ending 30 days ago
    scripts/retro_churn.py --window 30 --before 60
"""

from __future__ import annotations

import argparse
import collections
import subprocess
import sys


def _git(*args: str, tolerant: bool = False) -> str:
    """`tolerant` returns "" rather than exiting -- for a path that no longer
    exists at HEAD, which is not an error but the strongest churn signal there
    is: every line added to a file since deleted is a line that did not
    survive."""
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        if tolerant:
            return ""
        print(f"git failed: {' '.join(args)}\n{out.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return out.stdout


#: Files whose churn says nothing about engineering quality: generated artefacts,
#: lockfiles, snapshots and locale catalogues all move wholesale by design.
_IGNORE = (
    "poetry.lock",
    "package-lock.json",
    "go.sum",
    ".golden",
    ".snap",
    "/messages/",
    "schema.json",
    "_contract.json",
    "baseline.json",
    # Exported requirement sets are regenerated wholesale by
    # `scripts/lock-python-deps.sh`, so they churn at ~44% and say nothing about
    # engineering quality. Added after the first run surfaced them alongside a
    # stray `.sarif` at 0% -- both real churn, neither a signal. The rule: if a
    # tool writes the file, its survival rate measures the tool.
    "requirements-",
    ".sarif",
    ".lock",
    "/tool-results/",
    "tool_catalogue.json",
)


def survival(window: int, before: int) -> dict:
    since = f"{window + before} days ago"
    until = f"{before} days ago"
    revs = _git("log", "--format=%H", f"--since={since}", f"--until={until}").split()
    if not revs:
        return {"commits": 0}

    added: dict[str, set[str]] = collections.defaultdict(set)
    for rev in revs:
        # Numstat first so we skip merges and huge generated diffs cheaply.
        diff = _git("show", "--format=", "--unified=0", "--no-renames", rev)
        path = ""
        for line in diff.split("\n"):
            if line.startswith("+++ b/"):
                path = line[6:]
            elif line.startswith("+") and not line.startswith("+++") and path:
                body = line[1:].strip()
                if len(body) > 12:  # skip braces, blanks, single tokens
                    added[path].add(body)

    kept = lost = 0
    per_area: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for path, lines in added.items():
        if any(x in path for x in _IGNORE):
            continue
        now = _git("show", f"HEAD:{path}", tolerant=True)
        present = {ln.strip() for ln in now.split("\n")}
        area = path.split("/")[0] + ("/" + path.split("/")[1] if "/" in path else "")
        for body in lines:
            if body in present:
                kept += 1
                per_area[area][0] += 1
            else:
                lost += 1
                per_area[area][1] += 1

    total = kept + lost
    return {
        "commits": len(revs),
        "window": f"{window}d ending {before}d ago",
        "added": total,
        "kept": kept,
        "survival": (kept / total) if total else 0.0,
        "per_area": {
            a: {"kept": k, "lost": miss, "survival": k / (k + miss)}
            for a, (k, miss) in sorted(per_area.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
            if (k + miss) >= 40
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--before", type=int, default=30)
    a = ap.parse_args()

    r = survival(a.window, a.before)
    if not r.get("commits"):
        print("no commits in that window")
        return 0

    print(f"Line survival — {r['window']}, {r['commits']} commit(s)\n")
    print(f"  added (substantive lines): {r['added']:,}")
    print(f"  still present at HEAD:     {r['kept']:,}")
    print(f"  SURVIVAL:                  {r['survival']:.1%}\n")
    print("  by area (>=40 lines):")
    for area, v in r["per_area"].items():
        print(f"    {area:<34} {v['survival']:>6.1%}   ({v['kept']}/{v['kept'] + v['lost']})")
    print(
        "\n  Read as a TREND, not a threshold. The published comparison point is that"
        "\n  agent-authored lines have been found to persist at roughly half the rate of"
        "\n  human ones; what matters here is whether our own number is moving."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
