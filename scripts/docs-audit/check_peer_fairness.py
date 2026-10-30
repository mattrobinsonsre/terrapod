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

FOUR BLIND SPOTS, ALL FOUND BY THE 2.0 PRE-RELEASE AUDIT (#2277) RATHER THAN BY
THIS GATE, and all four are now closed. They are recorded because each one let a
real finding through while this printed "Nothing to review":

1. `PEERS` named only the Terraform-side peers, so EVERY Ansible-area peer was
   invisible -- AWX above all. The finding was AWX named as the cautionary
   example of a scaling defect, and one of its sites was in `llms.txt`, a file
   this gate already read. A name list is the whole subject of a gate like this,
   so an omission in it is not a tuning question.
2. It read only `*.md` + `llms.txt`, so a module docstring was unreachable. Two
   of that finding's five sites were Python docstrings. Source is now in scope.
3. It split sentences PER LINE, so a sentence wrapped across two lines could
   never co-locate a peer with a phrase -- which is exactly how the docstring
   site hid, with "large AWX" ending one line and "installs are known for"
   beginning the next. Lines are now joined into paragraphs first.
4. `ABSENCE` covered only absence. The finding was not an absence at all: it was
   a peer named as the example of a PROBLEM ("the scaling pain large AWX installs
   are known for"), which is editorial rather than factual and is the thing
   AGENTS.md's "never framed to rank or belittle" clause is about. `DEFECT` is
   that second framing.

What it still cannot see, stated so nobody reads a pass as broader than it is: a
commit message (not a file), and a whole paragraph whose framing is uncharitable
without any matching phrase. Both remain the release audit's job.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Every peer this project is measured against, Terraform-side AND Ansible-side.
#: The Ansible names were missing until #2277 and that is what let the AWX
#: finding through -- so when Terrapod grows into a new area, this list is the
#: first thing that has to grow with it.
PEERS = re.compile(
    r"\b(Terrakube|Atlantis|Spacelift|env0|Scalr|Digger|Terrateam|Burrito|tf-controller"
    r"|AWX|Ansible Automation Platform|Ansible Tower|Semaphore UI)\b"
)

# Framings that describe a thing by its absence.
ABSENCE = re.compile(
    r"\b(no UI|lacks?|only does|only offers|only supports|does ?n[o']t (?:have|support|offer|do)"
    r"|not present in|listed for completeness|missing|has no|there is no|without a"
    r"|unlike|whereas .{0,40}(?:cannot|can't)|limited to|falls short|fails to)\b",
    re.I,
)

#: Framings that name a thing as the EXAMPLE OF A PROBLEM rather than describe an
#: absence. "the scaling pain X is known for" asserts unsourced reputation in an
#: editorial register, which is the half of AGENTS.md's rule that is about
#: ranking rather than about completeness.
DEFECT = re.compile(
    r"\b(known for|notorious|infamous|struggles? with|suffers from|plagued"
    r"|scaling (?:pain|problem|issues)|does ?n[o']t scale|falls over|bogs down)\b",
    re.I,
)

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")

#: A floor, so a matcher that stops matching fails here instead of printing
#: "nothing to review" over an empty subject -- the failure this gate had for as
#: long as its peer list was short.
MIN_FILES = 200


def sentences(text: str):
    yield from re.split(r"(?<=[.!?])\s+", text)


def _drain(f: str, line_no: int, para: list[str], flagged: list) -> None:
    """Flag the accumulated paragraph, then clear it.

    Module level rather than a closure inside the file loop: capturing `para`
    and `para_line` late is fragile (ruff B023) even where it happens to work,
    and the paragraph buffer is the only state this gate carries.
    """
    if not para:
        return
    text = " ".join(para)
    for s in sentences(text):
        if PEERS.search(s) and (ABSENCE.search(s) or DEFECT.search(s)):
            flagged.append((f, line_no, s.strip()[:150]))
    para.clear()


def main() -> int:
    # `services/` source is in scope because a module docstring is prose a
    # contributor reads, and two of the AWX sites were docstrings -- driven on
    # their pre-fix text, this scope catches both, which is what makes the
    # source leg load-bearing rather than decorative.
    #
    # `migrate/` and the other Go modules are deliberately OUT of scope, and not
    # as a convenience: a migration tool's whole job is mapping concepts the
    # source platform does not have, so "Atlantis has no org concept" IS the
    # engineering information. Measured -- including them produced seven flags,
    # every one legitimate, and a gate whose output is mostly correct prose is
    # one people learn to scroll past.
    files = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt", "services/*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    # This file quotes the banned phrasings in order to ban them, so it matches
    # itself on every clause. Keyed on __file__ rather than on a path literal,
    # and hoisted here so every clause shares it -- excluding it from one clause
    # and not another is its own asymmetry.
    me = str(Path(__file__).resolve().relative_to(ROOT))
    files = [f for f in files if f != me]

    if len(files) < MIN_FILES:
        print(
            f"  only {len(files)} file(s) enumerated, expected at least {MIN_FILES}: "
            "the file matcher has stopped matching and this gate is reading almost "
            "nothing. Fix the matcher rather than lowering the floor."
        )
        return 1

    flagged = []
    for f in files:
        marker = None
        # Accumulate consecutive prose lines into one paragraph before splitting
        # into sentences, because a sentence wrapped across a line break is
        # exactly what the per-line split could never see.
        para: list[str] = []
        para_line = 0

        for i, line in enumerate((ROOT / f).read_text(errors="replace").splitlines(), 1):
            m = FENCE_RE.match(line)
            if m:
                _drain(f, para_line, para, flagged)
                run, info = m.group(1), m.group(2).strip()
                if marker is None:
                    if not ("`" in info and run[0] == "`"):
                        marker = run
                elif run[0] == marker[0] and len(run) >= len(marker) and info == "":
                    marker = None
                continue
            if marker is not None:
                continue
            if not line.strip():
                _drain(f, para_line, para, flagged)
                continue
            # A table row is not prose. Joining rows produces sentences nobody
            # wrote and then flags them: a `docs/index.md` row about the provider
            # collided with an unrelated row's wording this way.
            if line.lstrip().startswith("|"):
                _drain(f, para_line, para, flagged)
                continue
            if not para:
                para_line = i
            para.append(line.strip())
        _drain(f, para_line, para, flagged)

    print(f"  {len(files)} file(s) scanned for absence- or defect-framing near a peer's name")
    if flagged:
        print(
            f"\n  {len(flagged)} sentence(s) for human review "
            f"(not a failure — check each reads as a boundary, not a put-down):\n"
        )
        for f, i, s in flagged:
            print(f"    {f}:{i}\n      {s}")
    else:
        print("  Nothing to review — no peer is described by an absence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
