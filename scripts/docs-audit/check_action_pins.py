#!/usr/bin/env python3
"""A SHA-pinned action's `# vN` comment agrees with every other pin of that SHA.

Every `uses:` in `.github/workflows/` is pinned to a 40-character commit SHA, which
is the supply-chain control: a tag can be moved, a SHA cannot. The trailing
`# vN` comment is therefore the *only* human-readable statement of what is
pinned, and an auditor reading a workflow has nothing else to go on.

Those comments drift, because dependabot rewrites the SHA and the comment per
OCCURRENCE: a file whose comment was already stale keeps its stale comment
through the bump. Measured when dependabot raised the actions group in #2161 --
four of twenty distinct (action, sha) pairs disagreed with themselves, and
`actions/checkout@3d3c42e5` was simultaneously labelled `# v4` in
`checkov-build.yml`, `# v5.0.0` in `pricesheet.yml` and `# v7` across `ci.yml`.
It is genuinely v7, so two of those three files told a reader they were three
majors behind where they were.

**What this cannot catch, stated so nobody trusts it further than it goes:** if
every occurrence of a SHA carries the SAME wrong version, this passes. Internal
disagreement is the drift mechanism and is what is checkable without the
network; resolving a SHA to its real tag needs the API
(`repos/<a>/git/refs/tags`, matching `.object.sha`) and is the manual check when
a pin is bumped by hand.

Two failure modes, both fatal:
  * one (action, sha) labelled with more than one version
  * a SHA-pinned action with no version comment at all -- an unlabelled pin is
    unauditable, which is the same defect with the comment missing instead of wrong
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

#: `uses: owner/repo@<40 hex>` with an optional trailing `# vN` comment. Captures
#: the action, the SHA and the comment separately so a missing comment is a
#: distinguishable outcome rather than a non-match.
USES = re.compile(
    r"uses:\s*(?P<action>[^@\s]+)@(?P<sha>[0-9a-f]{40})\s*(?:#\s*(?P<ver>v[0-9][0-9A-Za-z.\-]*))?"
)

#: A floor, so a matcher that has stopped matching fails here instead of passing
#: with nothing inspected. Twenty distinct pairs were present when this was
#: written; the floor is deliberately well below that.
MIN_PINS = 10


def main() -> int:
    if not WORKFLOWS.is_dir():
        print(f"FAIL — {WORKFLOWS.relative_to(ROOT)} does not exist; this check has gone blind.")
        return 1

    versions: dict[tuple[str, str], set[str]] = defaultdict(set)
    where: dict[tuple[str, str], list[str]] = defaultdict(list)
    unlabelled: list[str] = []

    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        rel = path.relative_to(ROOT)
        for lineno, line in enumerate(path.read_text().split("\n"), 1):
            m = USES.search(line)
            if not m:
                continue
            key = (m.group("action"), m.group("sha"))
            ver = m.group("ver")
            if ver is None:
                unlabelled.append(f"{rel}:{lineno}  {m.group('action')}@{m.group('sha')[:12]}")
                continue
            versions[key].add(ver)
            where[key].append(f"{rel}:{lineno} ({ver})")

    if len(versions) < MIN_PINS:
        print(
            f"FAIL — only {len(versions)} distinct SHA-pinned action(s) found, expected at "
            f"least {MIN_PINS}. The matcher has probably stopped matching rather than the "
            "pins having disappeared."
        )
        return 1

    failed = False

    conflicting = {k: v for k, v in versions.items() if len(v) > 1}
    if conflicting:
        failed = True
        print(
            f"FAIL — {len(conflicting)} pinned action(s) carry disagreeing version comments.\n"
            "       One SHA is one version. Resolve the truth from the API\n"
            "       (gh api repos/<owner>/<repo>/git/refs/tags, match .object.sha)\n"
            "       and make every occurrence say it."
        )
        for (action, sha), vers in sorted(conflicting.items()):
            print(f"\n  {action}@{sha[:12]} is labelled {sorted(vers)}:")
            for site in where[(action, sha)]:
                print(f"      {site}")

    if unlabelled:
        failed = True
        print(f"\nFAIL — {len(unlabelled)} SHA-pinned action(s) carry no version comment:")
        for site in unlabelled:
            print(f"      {site}")

    if failed:
        return 1

    print(
        f"PASS — {len(versions)} pinned action(s); every occurrence of a SHA agrees on its version."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
