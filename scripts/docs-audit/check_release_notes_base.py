#!/usr/bin/env python3
"""The release notes' compare base is selected on the Terrapod release pattern.

The generated notes used to compute their base with `git describe --tags
--abbrev=0 "${version}^"`, which answers "the nearest tag of ANY kind". This
repository also publishes Checkov builds, tagged `3.4.4` and the like -- 38 of
its 311 tags when this was written -- so that command for `v2.0.0-beta.6`
returned `3.4.4`, and the notes linked `compare/3.4.4...v2.0.0-beta.6` with
their commit list drawn from the same wrong range (#2702).

Three things make it worth a gate rather than a fix:

It is INTERMITTENT. The same command for `v2.0.0-beta.7` returned
`v2.0.0-beta.6`, correctly, because no Checkov tag happened to sit in between.
So a release can be right, and the next one wrong, with nothing changed.

It was invisible. The notes are hand-replaced on every release, because the
generated ones are commit subjects and tell an operator nothing -- so the wrong
base is overwritten before anyone sees it. A PRACTICE was the only thing
between the defect and a published "Full Changelog" link spanning a Checkov
build's history, which is precisely the kind of thing that fails the first time
a release is cut in a hurry.

And it was carried forward unfiled three times (#2570, #2656, #2700 gate J).

So the selection lives in `scripts/release-versions.sh`, the one producer of
"which published versions are ours, and which of two is newer", and this script
DRIVES THAT PRODUCER over literal fixtures rather than describing it. The
ordering is Debian version ordering with a `-` to `~` transform, which is
unobvious enough that a reimplementation here would be the thing that is wrong
in a direction nobody could see.

The fixtures are asserted BEFORE the tree is scanned, and every one of them
carries a non-Terrapod tag: a fixture holding only the shapes the filter was
written for cannot see the defect it exists to catch.

A construction this cannot read RAISES rather than being skipped -- an
unreadable assignment or an unlocatable job is one it cannot vouch for.

Run from the repository root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import yaml

WORKFLOW = pathlib.Path(".github/workflows/ci.yml")
PRODUCER = pathlib.Path("scripts/release-versions.sh")

#: The job that generates the notes and elects the badge, and the two things it
#: must select with the producer.
RELEASE_JOB = "release-finalize"

#: Both call sites: the notes' compare base and the "Latest" badge election. It
#: catches one of them being rewired to select versions some other way. A third
#: site routing THROUGH the producer is the desired state rather than a defect;
#: a third site selecting versions without it would be the defect, and the
#: inline-pattern scan below is what catches that -- so a lower bound is what is
#: meant here, and the other direction is covered elsewhere.
MIN_CALL_SITES = 2

#: A floor against a case being DELETED. A lower bound is what is meant here:
#: each fixture asserts one specific selection rather than contributing to a
#: count, so a case added later can only add coverage and the other direction
#: needs no guard. What stops the set being narrowed by REPLACING a case with a
#: weaker one is derived from the fixtures themselves in `self_test` -- every
#: case must carry a non-Terrapod tag, and both producer modes must be covered.
MIN_FIXTURES = 8

#: Tags this repository really carries that are NOT Terrapod releases. The
#: Checkov builder publishes them, and they are what `git describe` picked.
_NOISE = ("3.4.4", "3.2.481", "checkov-3.2.481")

#: `(candidate tags, --before or None, expected selection, why)`. With a
#: `--before` the expectation is the newest release strictly older than it --
#: the notes' compare base. Without one it is the newest of all -- the badge
#: election, which `check_release_tag_triggers.py` additionally drives through
#: the workflow's own pipeline end to end.
FIXTURES: tuple[tuple[list[str], str | None, str, str], ...] = (
    (
        ["3.4.4", "v2.0.0-beta.5", "v2.0.0-beta.6"],
        "v2.0.0-beta.6",
        "v2.0.0-beta.5",
        "#2702 itself: a Checkov build newer than the previous release must not "
        "be the base, which is what `git describe --tags` answered",
    ),
    (
        ["v1.11.2", "3.4.4", "3.2.481", "v2.0.0-beta.6", "v2.0.0-beta.7"],
        "v2.0.0-beta.7",
        "v2.0.0-beta.6",
        "the real shape: Checkov tags interleaved through the history",
    ),
    (
        ["3.4.4", "v2.0.0-beta.6", "v2.0.0"],
        "v2.0.0",
        "v2.0.0-beta.6",
        "a final release's base is its last pre-release -- what `sort -V` gets "
        "WRONG untransformed, since it sorts a pre-release suffix AFTER the "
        "release it precedes",
    ),
    (
        ["3.4.4", "v2.0.0-alpha.2", "v2.0.0-alpha.10"],
        "v2.0.0-alpha.10",
        "v2.0.0-alpha.2",
        "numeric, not lexical, within a pre-release series",
    ),
    (
        ["3.4.4", "v1.10.8", "v1.10.9", "v1.11.0"],
        "v1.10.9",
        "v1.10.8",
        "a patch on an OLDER line takes its own line's predecessor, not the "
        "newest release in the repository",
    ),
    (
        ["3.4.4", "v2.0.0", "v2.1.0", "v2.1.0-alpha.1"],
        "v2.1.0",
        "v2.0.0",
        "a MINOR pre-release is not a Terrapod release (#2277: pre-releases are "
        "for majors only), so it cannot become a base -- the tag filter refuses "
        "it so no such release can exist, and this is the second layer",
    ),
    (
        [*_NOISE],
        "v1.0.0",
        "",
        "the first release of all has no predecessor: empty, and exit 0, so the "
        "notes step's own emptiness check is what handles it rather than a "
        "failing release step",
    ),
    (
        ["3.4.4", "v1.11.2", "v2.0.0-alpha.1"],
        None,
        "v2.0.0-alpha.1",
        "no `--before`: a pre-release is the newest thing published and holds "
        "the badge while it is (#2277)",
    ),
)


def run_producer(candidates: list[str], before: str | None) -> str:
    """Run the real producer and return its newest selection, or ``''``."""
    argv: list[str] = ["bash", str(PRODUCER)]
    if before is not None:
        argv += ["--before", before]
    proc = subprocess.run(
        argv,
        input="".join(f"{c}\n" for c in candidates),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # the returncode is inspected below, to report stderr
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{PRODUCER} exited {proc.returncode} for --before={before!r}: {proc.stderr.strip()}"
        )
    lines = [line for line in proc.stdout.split("\n") if line.strip()]
    return lines[-1] if lines else ""


def self_test() -> list[str]:
    """Drive the producer over the literal fixtures, before scanning anything.

    A gate that only scans the tree reports PASS the moment its matcher stops
    matching. These run first so that a narrowed producer fails here, naming
    the case, rather than silently checking less.
    """
    problems: list[str] = []

    if len(FIXTURES) < MIN_FIXTURES:
        problems.append(
            f"only {len(FIXTURES)} fixture(s), below the floor of {MIN_FIXTURES} -- "
            "a case was deleted, which narrows this gate's own subject"
        )

    # Derived from the fixtures rather than stated: a case made only of
    # Terrapod-shaped tags passes whether or not the filter is applied at all,
    # so it cannot see the defect. The one exception is deliberate and named.
    for candidates, before, _expected, why in FIXTURES:
        if not any(c for c in candidates if not c.startswith("v")):
            problems.append(
                f"fixture --before={before!r} carries no non-Terrapod tag, so it "
                f"would pass with the filter removed entirely ({why})"
            )

    modes = {before is None for _c, before, _e, _w in FIXTURES}
    if modes != {True, False}:
        problems.append(
            "the fixtures exercise only one of the producer's two modes; both "
            "call sites must be covered (`--before` for the notes base, none "
            "for the badge election)"
        )

    for candidates, before, expected, why in FIXTURES:
        try:
            got = run_producer(candidates, before)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        if got != expected:
            problems.append(
                f"over {candidates} with --before={before!r} the producer selected "
                f"{got!r}, expected {expected!r} -- {why}"
            )

    # The fail-closed half. Were `--before` handed something the filter drops,
    # the truncation would stop at nothing and the caller's `tail -1` would
    # return the newest release of ALL -- possibly newer than the version being
    # released, which is the one direction nothing downstream could detect.
    try:
        run_producer(["v1.11.2", "3.4.4"], "3.4.4")
    except RuntimeError:
        pass
    else:
        problems.append(
            "the producer accepted `--before 3.4.4`, which is not a Terrapod "
            "release version -- it must refuse rather than return the newest "
            "release of all"
        )

    return problems


def without_comments(shell: str) -> str:
    """Drop whole-line `#` comments.

    The scans below ask what the workflow RUNS, and the comments beside those
    lines necessarily quote `git describe` in order to explain why it is no
    longer used -- so scanning the raw text makes this gate fail on its own
    rationale.
    """
    return "\n".join(line for line in shell.split("\n") if not line.lstrip().startswith("#"))


def job_steps(doc: dict, name: str) -> list[dict]:
    spec = (doc.get("jobs") or {}).get(name)
    if spec is None:
        raise LookupError(f"ci.yml declares no job named {name!r} -- rename it here too")
    return list(spec.get("steps") or [])


def notes_step(doc: dict) -> str:
    """The `run:` block that computes the compare base.

    Located by what it DOES rather than by its step name, so renaming the step
    does not silently take it out of scope.
    """
    hits = [
        str(s.get("run", ""))
        for s in job_steps(doc, RELEASE_JOB)
        if "prev_tag=" in str(s.get("run", ""))
    ]
    if len(hits) != 1:
        raise LookupError(
            f"expected exactly one step of {RELEASE_JOB!r} to assign `prev_tag`, "
            f"found {len(hits)}. If the notes moved, teach this gate where -- do "
            "NOT reimplement the selection here."
        )
    return hits[0]


def assignment(shell: str, var: str) -> str:
    """The text of `var=$( ... )`, paren-balanced so a multi-line form reads."""
    anchor = f"{var}=$("
    start = shell.find(anchor)
    if start < 0:
        raise LookupError(f"cannot locate a `{anchor}` assignment")
    depth = 0
    for i in range(start + len(anchor) - 1, len(shell)):
        if shell[i] == "(":
            depth += 1
        elif shell[i] == ")":
            depth -= 1
            if depth == 0:
                return shell[start : i + 1]
    raise LookupError(f"the `{anchor}` assignment is not closed -- cannot read it")


def producer_pattern() -> list[str]:
    """The alternatives of the producer's own filter, read out of the script."""
    m = re.search(r"^PATTERN='([^']+)'$", PRODUCER.read_text(), re.M)
    if not m:
        raise LookupError(
            f"cannot locate `PATTERN='...'` in {PRODUCER}. If the filter moved, "
            "teach this gate where -- do NOT restate the pattern here."
        )
    parts = m.group(1).split("|")
    for p in parts:
        if not (p.startswith("^") and p.endswith("$")):
            raise LookupError(
                f"cannot split {PRODUCER}'s pattern into anchored alternatives: {p!r}"
            )
    return parts


def is_tag_alternatives(doc: dict) -> list[str]:
    """The `is_tag` test's regexes, with the ref prefix stripped.

    `is_tag` decides whether a pushed ref is a release at all; the producer
    decides which published versions are ours. They are the same rule about the
    same version strings, written in two languages, so they are compared rather
    than each being trusted. `check_release_tag_triggers.py` separately holds
    `is_tag` to `on.push.tags`, which chains all three together.
    """
    shell = "\n".join(str(s.get("run", "")) for s in job_steps(doc, "prepare"))
    found = re.findall(r'"\$GITHUB_REF"\s*=~\s*(\S+)', shell)
    if not found:
        raise LookupError(
            "cannot locate the `is_tag` test in the `prepare` job. Teach this "
            "gate the new shape -- do NOT restate the regex here."
        )
    prefix = "^refs/tags/"
    out = []
    for r in found:
        if not r.startswith(prefix):
            raise LookupError(f"the `is_tag` regex {r!r} does not start with {prefix!r}")
        out.append("^" + r[len(prefix) :])
    return out


def main() -> int:
    problems = self_test()

    for path in (WORKFLOW, PRODUCER):
        if not path.is_file():
            print(f"FAIL: {path} is missing", file=sys.stderr)
            return 1

    text = WORKFLOW.read_text()
    doc = yaml.safe_load(text)

    # The producer is invoked as a command by both call sites, so it must be
    # executable. A lost mode bit would fail the release itself.
    if not PRODUCER.stat().st_mode & 0o111:
        problems.append(f"{PRODUCER} is not executable, but both call sites invoke it directly")

    # 1. The compare base is selected by the producer, and not by `git describe`.
    try:
        step = notes_step(doc)
        assign = without_comments(assignment(step, "prev_tag"))
        if "git describe" in assign:
            problems.append(
                "the compare base is selected with `git describe`, which answers "
                '"the nearest tag of ANY kind" -- it picks a Checkov build (#2702). '
                f"Select it with {PRODUCER} instead."
            )
        if PRODUCER.name not in assign:
            problems.append(
                f"the compare base is not selected by {PRODUCER} -- whatever "
                "selects it is a second copy of a rule that has one producer"
            )
        if "--before" not in assign:
            problems.append(
                f"the compare base does not pass `--before` to {PRODUCER}, so it "
                "selects the newest release of all rather than this one's predecessor"
            )
        # Reachability is the enumeration's job, not the producer's, and it is
        # the one good property `git describe` had: without it a release cut
        # from an older line, or an old tag re-run later, can take a NEWER
        # release as its base. v1.10.11 shipped a month after v1.11.0.
        if "--merged" not in assign:
            problems.append(
                "the compare base enumerates every tag in the repository rather "
                'than `git tag --list --merged "$version"`, so a release whose '
                "line has since been patched can take a NEWER release as its base"
            )

        # Selected correctly is not the same as USED: the value that reaches the
        # compare link and the `git log` range must be the one checked above.
        body = without_comments(step)
        for what, needle in (
            ("the compare link", "/compare/"),
            ("the `git log` range", "range="),
        ):
            line = next((ln for ln in body.split("\n") if needle in ln), None)
            if line is None:
                problems.append(f"cannot locate {what} in the notes step")
            elif "prev_tag" not in line:
                problems.append(
                    f"{what} does not use `prev_tag`, so the selection checked "
                    "above is not the one the notes publish"
                )
    except LookupError as exc:
        problems.append(str(exc))

    # 2. One producer: every selection routes through it, and the filter is not
    #    restated anywhere. The pattern is searched for whole, which is what a
    #    copy-paste of the filter would look like.
    try:
        pattern = producer_pattern()
    except LookupError as exc:
        problems.append(str(exc))
        pattern = []

    if pattern:
        whole = "|".join(pattern)
        for wf in sorted(pathlib.Path(".github/workflows").glob("*.yml")):
            if whole in without_comments(wf.read_text()):
                problems.append(
                    f"{wf} restates the producer's filter inline -- one rule, two "
                    f"copies, and the copies diverge. Call {PRODUCER} instead."
                )

        try:
            if sorted(pattern) != sorted(is_tag_alternatives(doc)):
                problems.append(
                    f"{PRODUCER}'s filter and the `prepare` job's `is_tag` test "
                    "disagree about which version strings are Terrapod releases "
                    f"({pattern} vs {is_tag_alternatives(doc)}) -- a tag that starts "
                    "a release pipeline would not be recognised as a release by the "
                    "selection, or the reverse. Change both."
                )
        except LookupError as exc:
            problems.append(str(exc))

    call_sites = [
        line for line in without_comments(text).split("\n") if f"scripts/{PRODUCER.name}" in line
    ]
    if len(call_sites) < MIN_CALL_SITES:
        problems.append(
            f"only {len(call_sites)} call site(s) route through {PRODUCER}, below "
            f"the floor of {MIN_CALL_SITES} (the notes' compare base and the "
            "'Latest' badge election) -- one of them now selects versions some "
            "other way"
        )

    if problems:
        print(
            "FAIL: the release notes' compare base is not selected on the "
            "Terrapod release pattern.\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(
        f"PASS -- {PRODUCER} selects correctly over {len(FIXTURES)} fixture(s) and "
        f"refuses a non-release `--before`; the compare base and the `git log` "
        f"range both come from it rather than from `git describe`; "
        f"{len(call_sites)} call site(s) route through it and no workflow restates "
        f"its filter, which agrees with the `is_tag` test."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
