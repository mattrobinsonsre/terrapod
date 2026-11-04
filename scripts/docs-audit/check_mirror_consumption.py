#!/usr/bin/env python3
"""An image we mirror is not then pulled from the registry we mirrored it away from.

`.github/mirror-images.txt` lists the third-party images CI pulls heavily, and
`mirror-images.yml` copies each one into GHCR daily by digest. That is not a
speed optimisation: it is why a moved or withdrawn upstream tag, or a
third-party registry outage, cannot turn a run red for a reason the change had
nothing to do with -- and the daily sync is the path a rebuilt base image
reaches us by. `mirror-images.txt` says so itself, and says the half that goes
wrong: "Adding an image here is not enough on its own -- wire the override at
the call site too, or the mirror copy sits unused."

That is exactly how #2361 happened. The Playwright entry had been in the list
for releases and the mirrored copy existed all along, while `release/v1.11` and
`release/v1.10` held two hardcoded `mcr.microsoft.com/playwright` references
apiece -- so each of eight E2E shards pulled 0.91GB from mcr.microsoft.com on
every run, on the two lines we cut patches from. Nothing reported it, because a
successful upstream pull looks exactly like a successful mirror pull. The same
sweep found `release-rescan.yml` setting `SEMGREP_IMAGE` to the bare Docker Hub
reference, which is the identical defect in a workflow nobody was looking at.

A reference PASSES in one of two ways, and the second is the whole reason this
gate is usable:

  * it is prefixed by the mirror path; or
  * it is the default of a `${VAR:-upstream}` expansion.

The second form IS the convention -- 40 sites in `ci.yml` across four variables
-- and `mirror-images.txt` states the rule behind it: consumption is opt-in, so
every call site keeps its upstream reference as the default and a contributor
invoking the same command outside CI needs no access to our GHCR packages. A
gate that failed on that form would report 40 correct sites as defects, which is
the kind of gate people learn to wave through. What it catches is the reference
with no indirection at all, and an `env:` assignment pointing upstream, because
in both cases nothing anywhere can make the pull go to the mirror.

Comment lines are skipped deliberately: the prose in these files names the
upstream image in order to explain the mirroring, and `ci.yml` does it twice.

WHAT THIS DOES NOT CHECK, stated rather than gated. The converse -- that every
entry in the list is consumed somewhere -- would be the sharper reading of
`mirror-images.txt`'s own warning, and it is not checkable here without a ledger:
four of the fifteen entries (`python:3.10-slim` through `3.13-slim`) are consumed
by the Ansible execution-environment matrix through a reference built at run time
from the line's python version, so a literal search reports them missing. Four
false positives in fifteen is the noisy-gate failure, so that half is left to
the eye. The direction gated here is the one that costs bandwidth and a
third-party dependency on every run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

IMAGE_LIST = Path(".github/mirror-images.txt")
WORKFLOW_DIR = Path(".github/workflows")

#: The mirror namespace `mirror-images.yml` publishes into.
MIRROR_PREFIX = "ghcr.io/mattrobinsonsre/mirror/"

#: A matcher that stops matching must fail rather than pass on nothing. Two
#: floors, because they fail independently: an empty image list would take the
#: subject to zero, and a broken workflow glob would examine no references while
#: the list stayed full.
MIN_IMAGES = 10
MIN_REFERENCES = 20

#: `${VAR:-` immediately before the reference. Anchored at the end of the text
#: preceding the match, so it is the default OF this expansion rather than some
#: earlier one on the same line.
OPT_IN_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-$")


def mirrored_form(image: str) -> str:
    """The destination `mirror-images.yml` publishes `image` to.

    Its own rule, verbatim: `flat="${repo//\\//-}"` -- every slash in the
    repository path becomes a hyphen, so `alpine/helm` and a hypothetical other
    `helm` cannot collide in one flat namespace. Reproduced rather than imported
    because the workflow is shell; a divergence here would make the gate accept
    a path the mirror does not publish.
    """
    repo, tag = image.rsplit(":", 1)
    return f"{MIRROR_PREFIX}{repo.replace('/', '-')}:{tag}"


def main() -> int:
    listing = ROOT / IMAGE_LIST
    if not listing.is_file():
        print(f"FAIL — {IMAGE_LIST} does not exist")
        return 1

    images: list[str] = []
    for raw in listing.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[a-z0-9._/-]+:[a-zA-Z0-9._-]+", line):
            # The mirror workflow refuses this too. A reference the gate cannot
            # split is one it cannot vouch for, so it fails rather than skips.
            print(f"FAIL — {IMAGE_LIST} entry is not a repo:tag reference: {line!r}")
            return 1
        images.append(line)

    if len(images) < MIN_IMAGES:
        print(
            f"FAIL — only {len(images)} image(s) read from {IMAGE_LIST}, "
            f"expected at least {MIN_IMAGES}. Either the list has been gutted "
            f"or this gate's parse has stopped matching."
        )
        return 1

    workflows = sorted((ROOT / WORKFLOW_DIR).glob("*.y*ml"))
    if not workflows:
        print(f"FAIL — no workflow files under {WORKFLOW_DIR}")
        return 1

    examined = 0
    problems: list[str] = []

    for path in workflows:
        rel = path.relative_to(ROOT)
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for image in images:
                for match in re.finditer(re.escape(image), line):
                    examined += 1
                    before = line[: match.start()]
                    if before.endswith(MIRROR_PREFIX):
                        continue
                    if OPT_IN_DEFAULT.search(before):
                        continue
                    problems.append(
                        f"{rel}:{number} pulls {image} from upstream, though it "
                        f"is mirrored at {mirrored_form(image)}\n"
                        f"    {line.strip()}"
                    )

    if examined < MIN_REFERENCES:
        print(
            f"FAIL — only {examined} reference(s) examined across "
            f"{len(workflows)} workflow file(s), expected at least "
            f"{MIN_REFERENCES}. The matcher has stopped matching."
        )
        return 1

    if problems:
        print(f"FAIL — {len(problems)} mirrored image(s) referenced upstream in {WORKFLOW_DIR}:\n")
        for problem in problems:
            print(f"  {problem}")
        print(
            "\nPoint the reference at the mirror, or read it through an "
            "override variable whose default is the upstream reference — the "
            "form the other call sites use, which keeps a local invocation "
            "working without access to our GHCR packages."
        )
        return 1

    print(
        f"OK — {len(images)} mirrored image(s), {examined} reference(s) across "
        f"{len(workflows)} workflow file(s), none bypassing the mirror"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
