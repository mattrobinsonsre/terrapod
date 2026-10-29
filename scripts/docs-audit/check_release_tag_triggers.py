#!/usr/bin/env python3
"""The release trigger admits a pre-release, and a pre-release can hold the badge.

**Every half of this fails SILENTLY, which is why it is gated.**

`on.push.tags` patterns are FULL-match, so until #2277 the single pattern
`v[0-9]+.[0-9]+.[0-9]+` matched `v2.0.0-alpha.1` not at all: pushing a
pre-release tag started no pipeline, published nothing, cleaned nothing up, and
produced no failure anywhere to say so. A release that does nothing looks
identical to one nobody has pushed yet.

The second half is the same shape one step along, and it reverses what this file
asserted when it was written. A pre-release is published under the "Latest"
badge DELIBERATELY (#2277): while `v2.0.0-alpha.1` is the newest thing we have
published, the repo front page should point at it rather than at the previous
stable line. GitHub refuses the badge to a release flagged `--prerelease`, so the
flag and the badge cannot both be had -- and `gh release edit` exits 0 either
way, so reintroducing the flag would decline the badge without erroring.

The third half is the ordering, and it is the one no amount of reading catches.
`sort -V` is Debian version ordering, in which a pre-release suffix sorts AFTER
the release it precedes -- measured identically under GNU coreutils 9.12 and BSD
sort, so it is not a platform quirk to be waited out. Without the `-` to `~`
transform the election picks `v2.0.0-alpha.1` over `v2.0.0`, and the alpha keeps
the badge for ever once 2.0.0 ships.

So the election is not described here, it is EXTRACTED FROM THE WORKFLOW AND
RUN. A reimplementation of a pipeline with semantics this unobvious is the thing
that would be wrong in a direction nobody could see; the only honest check is to
feed the production pipeline a fixture and read what it elects.

A construction this cannot read RAISES rather than being skipped -- an
unreadable pattern or an unlocatable pipeline is one it cannot vouch for.

Run from the repository root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import yaml

WORKFLOW = pathlib.Path(".github/workflows/ci.yml")

#: Tags that MUST trigger the release pipeline. One stable, one pre-release in
#: the form settled on in #2277, and one with a two-digit minor so a pattern
#: narrowed to single digits is caught.
MUST_MATCH = ("v1.11.1", "v2.0.0-alpha.1", "v3.0.0-rc.2")

#: Tags that must NOT, so a pattern widened into a catch-all fails here rather
#: than by publishing something from a stray tag.
#: `v2.1.0-alpha.1` and `v2.0.1-rc.1` are here because a pre-release is for a
#: MAJOR only. Admitting one would let it publish and take the "Latest" badge
#: off a stable release, since this workflow deliberately does not flag a
#: pre-release and the badge therefore follows publish order.
MUST_NOT_MATCH = (
    "latest",
    "v1.2",
    "checkov-3.2.481",
    "nightly-2026-10-28",
    "v2.1.0-alpha.1",
    "v2.0.1-rc.1",
    "v2.10.0-rc.2",
)

#: The jobs that create and then finalise the GitHub Release.
RELEASE_JOBS = ("release-create", "release-finalize")

#: `(the tags that exist, the one that must be elected, why)`. The Checkov
#: builder publishes non-`v` releases into this repository, so every case
#: carries one: an election that starts admitting them is the #1374 failure from
#: a new direction.
_CHECKOV = "checkov-3.2.481"
ELECTIONS = (
    (
        [_CHECKOV, "v1.10.6", "v1.11.2"],
        "v1.11.2",
        "no pre-release in play: the newest stable wins, as it always did",
    ),
    (
        [_CHECKOV, "v1.11.2", "v2.0.0-alpha.1"],
        "v2.0.0-alpha.1",
        "a pre-release holds the badge while it is the newest thing published",
    ),
    (
        [_CHECKOV, "v1.11.2", "v2.0.0-alpha.1", "v2.0.0"],
        "v2.0.0",
        "the final takes the badge back -- what `sort -V` gets WRONG untransformed",
    ),
    (
        [_CHECKOV, "v2.0.0-alpha.1", "v2.0.0-rc.1"],
        "v2.0.0-rc.1",
        "rc beats alpha",
    ),
    (
        [_CHECKOV, "v2.0.0-alpha.2", "v2.0.0-alpha.10"],
        "v2.0.0-alpha.10",
        "numeric, not lexical, within a pre-release series",
    ),
    (
        [_CHECKOV, "v1.11.2"],
        "v1.11.2",
        "a lone stable release is still elected over the Checkov tags",
    ),
    (
        [_CHECKOV, "v1.11.2", "v2.0.0", "v2.1.0-alpha.1"],
        "v2.0.0",
        "a MINOR pre-release cannot take the badge from a stable major -- the tag "
        "filter refuses it so no such release can exist, and this is the second "
        "layer: were one somehow published, the election still would not elect it",
    ),
)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub Actions filter pattern to an anchored regex.

    Actions patterns are not globs and not regexes: `*` matches any run of
    characters except `/`, `+` means one-or-more of what precedes it, `[]` is a
    character class, and `.` is LITERAL. Getting that last one wrong is how a
    check like this would pass on a pattern that cannot match a real tag.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[":
            j = pattern.index("]", i)
            out.append(pattern[i : j + 1])
            i = j + 1
        elif c in "+*?":
            out.append({"+": "+", "*": "[^/]*", "?": "[^/]"}[c])
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def job_shell(doc: dict, name: str) -> str:
    """Every `run:` block of one job, joined."""
    spec = (doc.get("jobs") or {}).get(name)
    if spec is None:
        raise LookupError(f"ci.yml declares no job named {name!r} -- rename it here too")
    return "\n".join(str(s.get("run", "")) for s in (spec.get("steps") or []))


def without_comments(shell: str) -> str:
    """Drop whole-line `#` comments.

    The flag scan below asks what the job RUNS, and the comments beside those
    lines necessarily quote the flag in order to explain why it is not used --
    so scanning the raw text makes the gate fail on its own rationale. That is
    the recorded shape where a gate reports a defect that does not exist, and
    the fix is to narrow the subject rather than to stop quoting the flag.
    """
    return "\n".join(line for line in shell.split("\n") if not line.lstrip().startswith("#"))


def extract_election(shell: str) -> str:
    """Pull the badge-election pipeline out of the workflow, verbatim.

    It runs as `highest=$( { ...enumerate... } | <pipeline> )`, so the pipeline
    is everything from the `}` that closes the enumeration to the `)` that
    closes the substitution. Taken as TEXT and run as-is: translating it would
    reintroduce exactly the ordering subtlety it exists to pin.
    """
    start = shell.find("} | ")
    if start < 0:
        raise LookupError(
            "cannot locate the badge election: no `} | ` in release-finalize. "
            "If it was restructured, teach this extractor the new shape -- do "
            "NOT reimplement the pipeline here."
        )
    lines = shell[start:].split("\n")
    out = [lines[0][len("} | ") :]]
    for line in lines[1:]:
        if line.strip() == ")":
            break
        out.append(line)
    else:
        raise LookupError("the badge election is not closed by `)` -- cannot read it")
    pipeline = "\n".join(out)
    # Drop the line continuations so this is one shell pipeline.
    return pipeline.replace("\\\n", " ")


def concrete(pattern: str) -> str:
    """A concrete ref name that the given Actions branch pattern matches.

    Substituted rather than listed, so a renamed branch convention is still
    covered. The result is checked back against the pattern and RAISES if it
    does not match -- a pattern this cannot instantiate is one it cannot vouch
    for, and silently skipping it is how a check comes to pass on nothing.
    """
    out = pattern.replace("**", "v2.0").replace("*", "x").replace("?", "y")
    if not glob_to_regex(pattern).match(out):
        raise LookupError(f"cannot build a concrete branch name for the pattern {pattern!r}")
    return out


def extract_cosign_identity(text: str) -> str:
    """The certificate-identity regex the manifest verification trusts."""
    m = re.search(r"^\s*COSIGN_IDENTITY_RE:\s*(\S+)\s*$", text, re.M)
    if not m:
        raise LookupError(
            "cannot locate COSIGN_IDENTITY_RE. If the trust anchor moved, teach "
            "this extractor where -- do NOT reimplement it here."
        )
    return m.group(1)


def extract_is_tag_regex(shell: str) -> list[str]:
    """Pull the `is_tag` bash regex(es) out of the `prepare` job, verbatim.

    `on.push.tags` decides whether a pipeline STARTS; this decides whether that
    pipeline is a release. They are written in different languages and nothing
    makes them agree, so a tag admitted by one and rejected by the other starts
    a build that publishes nothing and fails nowhere -- the worst shape, because
    `Cleanup Tag` never fires and the tag survives looking released.

    Returns a LIST because the test is a disjunction: a pre-release suffix is
    admitted on a MAJOR only (`vN.0.0-...`), which one pattern with an optional
    suffix cannot express. Every alternative inside the one `[[ ]]` is returned
    and `is_tag` re-joins them with `||`, so the shape being checked stays the
    shape the workflow runs rather than a reconstruction of it.
    """
    block = re.search(r"\[\[(.+?)\]\]", shell, re.S)
    if not block:
        raise LookupError(
            "cannot locate the `is_tag` test in the `prepare` job. If it was "
            "restructured, teach this extractor the new shape -- do NOT "
            "reimplement the regex here."
        )
    found = re.findall(r'"\$GITHUB_REF"\s*=~\s*(\S+)', block.group(1))
    if not found:
        raise LookupError(
            'the `is_tag` test matches no `"$GITHUB_REF" =~ <regex>` comparison. '
            "Teach this extractor the new shape -- do NOT reimplement the regex here."
        )
    return found


def is_tag(regexes: list[str], tag: str) -> bool:
    """Run the extracted test in bash, exactly as the workflow does."""
    test = " || ".join(f'[[ "$1" =~ {r} ]]' for r in regexes)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"if {test}; then exit 0; else exit 1; fi",
            "_",
            f"refs/tags/{tag}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # exit 1 means "the regex did not match", not an error
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"the extracted `is_tag` test errored: {proc.stderr.strip()}")
    return proc.returncode == 0


def elect(pipeline: str, tags: list[str]) -> str:
    proc = subprocess.run(
        ["bash", "-c", pipeline],
        input="\n".join(tags) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # the returncode is inspected below, to report stderr
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"the extracted election exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def main() -> int:
    if not WORKFLOW.is_file():
        print(f"FAIL: {WORKFLOW} is missing", file=sys.stderr)
        return 1

    doc = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 types a bare `on` as the boolean True, which is a trap worth
    # naming: `doc["on"]` is a KeyError on a workflow that parses perfectly.
    triggers = doc.get(True) or doc.get("on") or {}
    patterns = ((triggers.get("push") or {}).get("tags")) or []
    if not patterns:
        print("FAIL: ci.yml declares no `on.push.tags`, so no tag releases at all", file=sys.stderr)
        return 1

    regexes = [(p, glob_to_regex(p)) for p in patterns]
    problems: list[str] = []

    for tag in MUST_MATCH:
        if not any(r.match(tag) for _, r in regexes):
            problems.append(
                f"{tag!r} matches NO pattern -- pushing it would start no pipeline, silently"
            )
    for tag in MUST_NOT_MATCH:
        hit = [p for p, r in regexes if r.match(tag)]
        if hit:
            problems.append(f"{tag!r} matches {hit} -- the filter has widened into a catch-all")

    # The `is_tag` derivation must admit exactly what the filter admits.
    try:
        regex = extract_is_tag_regex(job_shell(doc, "prepare"))
        for tag in MUST_MATCH:
            if not is_tag(regex, tag):
                problems.append(
                    f"{tag!r} passes `on.push.tags` but NOT the `is_tag` test -- the "
                    "pipeline would start, skip every release job, publish nothing and "
                    "fail nowhere, leaving the tag looking released"
                )
        for tag in MUST_NOT_MATCH:
            if is_tag(regex, tag):
                problems.append(
                    f"{tag!r} is rejected by `on.push.tags` but accepted by the `is_tag` test"
                )
    except (LookupError, RuntimeError) as exc:
        problems.append(str(exc))

    # Every ref that can build a manifest must be trusted to have built one.
    #
    # `create-manifests` is gated only on `is_pr != 'true'`, so ANY non-PR push
    # reaches it -- every branch in `on.push.branches` and every tag in
    # `on.push.tags`. The verification downstream pins the certificate identity
    # to a fixed list of refs, and the two drifted apart when #2185 put
    # `release/**` on `on.push`: a release branch began building and signing
    # manifests that the verifier would refuse. Nothing caught it because neither
    # supported release line carries the verification step, so it would first
    # have fired on the first tag of the first release branch cut from this
    # workflow -- failing the release and letting `Cleanup Tag` delete the tag.
    #
    # Derived from `on.push` rather than listed, so the next change to it is
    # covered without anyone remembering this exists.
    try:
        identity = re.compile(extract_cosign_identity(WORKFLOW.read_text()))
        prefix = "https://github.com/mattrobinsonsre/terrapod/.github/workflows/ci.yml@"
        builders = [
            f"refs/heads/{concrete(b)}"
            for b in ((triggers.get("push") or {}).get("branches") or [])
        ]
        builders += [f"refs/tags/{t}" for t in MUST_MATCH]
        if not builders:
            problems.append(
                "`on.push` declares no branches or tags -- nothing could build a manifest"
            )
        for ref in builders:
            if not identity.match(prefix + ref):
                problems.append(
                    f"{ref} can build a manifest but COSIGN_IDENTITY_RE refuses it -- the "
                    "release that first uses it fails verification and Cleanup Tag deletes "
                    "the tag"
                )
        # Both directions, and the second is DERIVED rather than listed. The
        # anchor must admit every ref that can build -- too narrow and the
        # release that first uses it fails verification -- and no more. A
        # mutation widening it back to `vN.N.N(-suffix)?` was NOT caught while
        # this loop held three hand-written refs, because widening still admits
        # every builder: the gate only ever asked the first question. Anything
        # the tag filter refuses must be refused here too, or the anchor is
        # already permitting a ref a later widening of the filter would let
        # sign, which is one layer of defence gone before anyone notices.
        refused = ["refs/heads/feature/x", "refs/pull/123/merge"]
        refused += [f"refs/tags/{t}" for t in MUST_NOT_MATCH]
        for ref in refused:
            if identity.match(prefix + ref):
                problems.append(
                    f"COSIGN_IDENTITY_RE accepts {ref}, which cannot build a manifest -- "
                    "the trust anchor is wider than the refs that can sign"
                )
    except (LookupError, re.error) as exc:
        problems.append(str(exc))

    # No release job may flag a release as a pre-release. `--prerelease` on its
    # own is a truthy flag and `--prerelease=true` is the same thing spelled out;
    # only an explicit `=false` is permitted, and it is required rather than
    # merely allowed so a re-run converges instead of inheriting what is there.
    for name in RELEASE_JOBS:
        try:
            shell = job_shell(doc, name)
        except LookupError as exc:
            problems.append(str(exc))
            continue
        for m in re.finditer(r"--prerelease(=[\w.$\"'-]*)?", without_comments(shell)):
            if m.group(1) != "=false":
                problems.append(
                    f"job {name!r} passes {m.group(0)!r} -- GitHub refuses the Latest "
                    "badge to a flagged pre-release, and `gh release edit` exits 0 "
                    "either way, so this declines the badge in silence (#2277)"
                )

    # The election itself: extracted and run.
    try:
        pipeline = extract_election(job_shell(doc, "release-finalize"))
    except (LookupError, RuntimeError) as exc:
        problems.append(str(exc))
        pipeline = ""

    if pipeline:
        for tags, expected, why in ELECTIONS:
            try:
                got = elect(pipeline, tags)
            except RuntimeError as exc:
                problems.append(str(exc))
                break
            if got != expected:
                problems.append(
                    f"election over {tags} picked {got!r}, expected {expected!r} -- {why}"
                )

    if problems:
        print(
            "FAIL: the release tag trigger or the Latest-badge election has regressed.\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(
        f"PASS -- {len(patterns)} tag pattern(s) admit every release form "
        f"({', '.join(MUST_MATCH)}) and no catch-all; no release job flags a "
        f"pre-release; every ref that can build a manifest is a trusted signer; "
        f"the extracted `is_tag` test agrees with the filter; the "
        f"extracted election is correct over {len(ELECTIONS)} cases."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
