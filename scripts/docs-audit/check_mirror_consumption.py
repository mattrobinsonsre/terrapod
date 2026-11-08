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
from collections.abc import Sequence
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

#: The images the fixtures below are written against. Deliberately fixed rather
#: than read from `mirror-images.txt`: a fixture whose subject moves when the list
#: moves is not a fixture, and the point here is to pin the MATCHER.
SELF_TEST_IMAGES = ("semgrep/semgrep:1.117", "node:24-alpine")

#: Literal lines the verdict must read the stated way, as
#: `(line, occurrences examined, images judged to bypass the mirror)`.
#:
#: This is the gate on the gate, and it exists because the floors below cannot do
#: this job: `MIN_REFERENCES` proves the subject is non-empty and says nothing
#: about whether either pass-rule still discriminates. Narrow a rule and the scan
#: reports correct sites as defects; widen one and it reports nothing -- and both
#: keep the floor satisfied, because the occurrences are still being counted.
#:
#: `examined` is pinned as well as the verdict, because the comment skip is only
#: visible there: if comments stopped being skipped the counts would rise and the
#: prose in these files -- which names the upstream image precisely in order to
#: explain the mirroring -- would be read as a pull.
SELF_TEST: list[tuple[str, int, list[str]]] = [
    # The convention: read through an override whose default is upstream.
    ('    "${SEMGREP_IMAGE:-semgrep/semgrep:1.117}" \\', 1, []),
    # Already pointed at the mirror, for a single-segment image -- the mirror
    # path still contains the reference, so the `MIRROR_PREFIX` rule is what
    # passes it.
    ("      image: ghcr.io/mattrobinsonsre/mirror/node:24-alpine", 1, []),
    # Measured, and NOT what reading the code suggests: for a MULTI-segment
    # image the published mirror path is flattened (`semgrep-semgrep`), so it no
    # longer contains the literal `semgrep/semgrep:1.117` and is not examined at
    # all. Pinned because it means the `MIRROR_PREFIX` rule is load-bearing only
    # for the single-segment entries; the five multi-segment ones reach the
    # mirror through the override form above, and a hand-written un-flattened
    # mirror path -- which nothing publishes -- would pass on the prefix alone.
    ("      image: ghcr.io/mattrobinsonsre/mirror/semgrep-semgrep:1.117", 0, []),
    ("      image: ghcr.io/mattrobinsonsre/mirror/semgrep/semgrep:1.117", 1, []),
    # The defect: a bare reference with no indirection at all.
    ("      image: semgrep/semgrep:1.117", 1, ["semgrep/semgrep:1.117"]),
    # The #2361 shape -- an `env:` assignment pointing upstream, which is what
    # `release-rescan.yml` held for `SEMGREP_IMAGE`.
    ("        SEMGREP_IMAGE: semgrep/semgrep:1.117", 1, ["semgrep/semgrep:1.117"]),
    # The anchoring: `OPT_IN_DEFAULT` must be the default OF this reference, not
    # of some earlier expansion on the same line. Drop the `$` anchor and this
    # line starts passing.
    (
        "      run: docker run ${OTHER:-x} semgrep/semgrep:1.117",
        1,
        ["semgrep/semgrep:1.117"],
    ),
    # Two references on one line are two verdicts, not one.
    (
        "      run: docker run node:24-alpine && docker run semgrep/semgrep:1.117",
        2,
        ["semgrep/semgrep:1.117", "node:24-alpine"],
    ),
    # Must NOT be examined: a comment names the upstream image to explain the
    # mirroring. Nothing is pulled by prose.
    ("  # mirrored daily from semgrep/semgrep:1.117", 0, []),
    ("# node:24-alpine is on the list above", 0, []),
    # Must NOT be examined: an image that is not on the list is not our business,
    # so the matcher stays scoped to what the mirror actually publishes.
    ("      image: python:3.9-slim", 0, []),
]


def line_verdict(line: str, images: Sequence[str]) -> tuple[int, list[str]]:
    """One workflow line: how many references it carries, and which bypass.

    The scan below calls this for every line of every workflow, so the fixtures
    in `SELF_TEST` exercise the same code the real run does rather than a
    restatement of it.
    """
    if line.lstrip().startswith("#"):
        return 0, []
    examined = 0
    bypassing: list[str] = []
    for image in images:
        for match in re.finditer(re.escape(image), line):
            examined += 1
            before = line[: match.start()]
            if before.endswith(MIRROR_PREFIX):
                continue
            if OPT_IN_DEFAULT.search(before):
                continue
            bypassing.append(image)
    return examined, bypassing


def self_test() -> list[str]:
    """Assert the fixtures BEFORE the scan, so a narrowed rule says which line.

    Running this first matters: a matcher that has stopped reading a form
    otherwise just checks less, silently, and reports OK over whatever it can
    still see.
    """
    out: list[str] = []
    for line, want_examined, want_bypassing in SELF_TEST:
        got_examined, got_bypassing = line_verdict(line, SELF_TEST_IMAGES)
        if got_examined != want_examined or got_bypassing != want_bypassing:
            out.append(
                f"the verdict for {line!r} has changed: expected "
                f"{want_examined} reference(s) and {want_bypassing}, got "
                f"{got_examined} and {got_bypassing}. Fix the rule rather than "
                f"the fixture -- a form this gate cannot read is a form it does "
                f"not check."
            )
    return out


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


#: The flattening, pinned. Only the failure MESSAGE reads this, so nothing else
#: would notice it diverging from the workflow's own `${repo//\//-}` -- and a
#: message naming a path the mirror does not publish sends the reader to a 404.
FORM_SELF_TEST: list[tuple[str, str]] = [
    ("semgrep/semgrep:1.117", f"{MIRROR_PREFIX}semgrep-semgrep:1.117"),
    ("registry:2", f"{MIRROR_PREFIX}registry:2"),
    (
        "mcr.microsoft.com/playwright:v1.58.2-noble",
        f"{MIRROR_PREFIX}mcr.microsoft.com-playwright:v1.58.2-noble",
    ),
]


def main() -> int:
    blockers = self_test()
    for image, want in FORM_SELF_TEST:
        got = mirrored_form(image)
        if got != want:
            blockers.append(
                f"mirrored_form({image!r}) now gives {got!r}, not {want!r}. The "
                f"flattening has diverged from the mirror workflow's own rule, so "
                f"the failure message would name a path nothing publishes."
            )
    if blockers:
        print("FAIL — this gate cannot do its job:\n")
        for blocker in blockers:
            print(f"  {blocker}")
        return 1

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
            here, bypassing = line_verdict(line, images)
            examined += here
            for image in bypassing:
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
        f"{len(workflows)} workflow file(s), none bypassing the mirror; "
        f"{len(SELF_TEST)} verdict fixture(s) and {len(FORM_SELF_TEST)} "
        f"flattening fixture(s) hold"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
