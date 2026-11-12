#!/usr/bin/env python3
"""Decide which CI job families a change can PROVABLY not affect.

The rule this implements, in one sentence: a job runs unless every changed file
is outside the set of inputs that job reads.  Everything else about the design
follows from making that claim honest.

WHY THE INPUT SETS ARE DERIVED AND NOT WRITTEN DOWN
---------------------------------------------------
A hand-written path filter is a dependency ledger, and this repository's ledgers
go stale -- that is recorded in several places and has caused real defects.  The
specific hazard here is depth: the Python tier reads `web/src/app/workspaces`,
`web/messages`, `go-terrapod/*.go`, `provider/internal/.../workspace/*.go` and
`mcp/internal/mcpserver/crud.go`, none of which anybody would guess, and a
filter that missed one would skip the tier that gates them.

So nothing below enumerates paths.  The Python tier's inputs come from the two
test Dockerfiles' own COPY lines, and the Go edges from each `go.mod`'s own
`replace` directives.  Add a COPY and the filter widens in the same commit; the
deriver cannot lag the thing it derives from.

WHAT MAKES IT FAIL SAFE
-----------------------
Skipping wrongly is silent -- a green run that proved nothing -- so every
uncertainty resolves to RUN:

  * a changed path whose top-level component is not one this repository has
    forces every family on.  Note what this does and does not cover: the set is
    read from the CHECKED-OUT tree, which on a pull request is the merge ref and
    therefore already contains any directory the pull request ADDS.  So this
    catches a path that has gone away, not a new one.  A new top-level directory
    is instead safe by the families being derived -- `go_modules()` globs at run
    time, and a new directory matches no family and so skips nothing that reads
    it;
  * a change to `.github/workflows/ci.yml` or to a composite action it calls
    forces every family on, because that changes how the jobs themselves
    behave.  Not `.github/**`: the other seven workflows here are scheduled or
    unrelated to the pull-request jobs, and running everything for them buys
    nothing;
  * `scripts/**` forces every family on, because jobs shell out to it
    (`lib.sh` computes the test base tag, `docs-audit/` is a gate, `eval.sh`
    boots the eval stack);
  * a Dockerfile that cannot be parsed, or an empty changed-file list, forces
    every family on;
  * and the caller only consults this at all on a pull request (see below).

WHY PULL REQUESTS ONLY
----------------------
A push to `main` or a release branch must build every image, because
`create-manifests` writes the `sha-<short>` manifest that a later tag retags --
skipping one would silently cost the release its earned skip.  And an image scan
reads the live advisory database rather than the image alone, so a scan that
would have passed yesterday can fail today with no diff at all; path-gating one
would mean a CVE published since the last push goes unreported until something
unrelated happens to touch that image.  Both arguments are about the branch
build, so the gate is confined to PRs, where the question is only "does this
change need checking".

WHAT IS DELIBERATELY NOT GATED
------------------------------
`build-query` and `build-ee-agent` publish artifacts the image builds download;
`e2e-test`, `helm-eval-boot`, `cli-contract`, `airgap-gate` and
`oci-conformance` load the PR's image artifacts.  Gating any of those starves a
consumer, so they are out of scope here rather than quietly half-handled.
"""

from __future__ import annotations

import argparse
import fnmatch
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _force_all_prefixes() -> tuple[str, ...]:
    """Changed paths that mean we cannot reason about the run, so everything runs.

    `.github/` as a whole is too wide: seven of the eight workflows here have
    nothing to do with the pull-request jobs (the scheduled re-scan, the EE
    build, the image mirror, the price sheet, cleanup, checkov, this gating's
    own audit), and a change to one of them would needlessly run the lot.

    What does belong is the workflow that DEFINES these jobs, plus the composite
    actions it calls -- derived from its own `uses: ./.github/...` lines rather
    than listed, so a new composite action is covered on the day it is added.

    `scripts/` stays whole: jobs shell out to it all over (`lib.sh` computes the
    test base tag, `test.sh` runs the Python tier, `docs-audit/` is a gate,
    `eval.sh` boots the eval stack), and it is small and rarely touched.
    """
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    local = sorted(set(re.findall(r"uses:\s*\./(\.github/[A-Za-z0-9/_.-]+)", ci.read_text())))
    return (".github/workflows/ci.yml", "scripts/", *(f"{d}/" for d in local))


FORCE_ALL_PREFIXES = _force_all_prefixes()


def _copy_sources(dockerfile: str) -> list[str]:
    """The host paths a Dockerfile COPYs in -- i.e. what can change its output.

    Raises rather than returning a partial list: a Dockerfile we cannot read is
    a dependency set we cannot vouch for, and the caller turns that into "run
    everything".
    """
    path = ROOT / dockerfile
    text = re.sub(r"\\\n\s*", " ", path.read_text())  # join continuations
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r"(?i)^(COPY|ADD)\b", stripped):
            continue
        if "--from=" in stripped:
            continue  # copied from an earlier stage, not from the host
        tokens = [t for t in stripped.split()[1:] if not t.startswith("--")]
        out.extend(tokens[:-1])  # the last token is the destination
    if not out:
        raise ValueError(f"{dockerfile} declares no host COPY sources")
    return out


def _go_closure(module: str, seen: set[str] | None = None) -> set[str]:
    """A Go module plus every sibling module it `replace`s, transitively."""
    seen = seen if seen is not None else set()
    if module in seen:
        return seen
    seen.add(module)
    gomod = ROOT / module / "go.mod"
    if gomod.exists():
        for other in re.findall(r"=>\s*\.\./([A-Za-z0-9_-]+)", gomod.read_text()):
            _go_closure(other, seen)
    return seen


def go_modules() -> list[str]:
    return sorted(p.parent.name for p in ROOT.glob("*/go.mod"))


def _compose_files_for_tests() -> list[str]:
    """The compose files `scripts/test.sh` names -- the Python tier's runtime.

    `python-test` is a single step, `scripts/test.sh python -- <args>`, and that
    script brings the tier up with a compose file carrying the database URL, the
    storage backend and the postgres/redis/localstack image tags. None of that is
    baked into an image, so reading the Dockerfiles' COPY lines cannot see it --
    and a change to it with no other change would otherwise skip every Python
    slice while altering what they run against.

    Derived from the script rather than written down, for the same reason as
    everything else here.
    """
    src = (ROOT / "scripts" / "test.sh").read_text()
    # The name alone: the script writes `"$REPO_ROOT/docker-compose.test.yml"`,
    # and a pattern that also eats the prefix yields `REPO_ROOT/...`, which
    # matches no changed path -- silently, and in the direction that skips.
    found = sorted(set(re.findall(r"docker-compose[\w.-]*\.ya?ml", src)))
    if not found:
        raise ValueError("scripts/test.sh names no compose file")
    return found


def families() -> dict[str, list[str]]:
    """family -> the input paths it reads, all derived from the tree."""
    python_inputs = sorted(
        set(_copy_sources("docker/Dockerfile.test"))
        | set(_copy_sources("docker/Dockerfile.test-base"))
        # `lib.sh` computes the base image tag, so it decides which base is used
        | {"docker", "scripts/lib.sh"}
    )
    all_go = sorted(set().union(*(_go_closure(m) for m in go_modules())))
    return {
        # `pentest/` holds the accepted-risk register each audit reads. Removing
        # a suppression IS the change, and the job that would prove the finding
        # has really gone is the one that would otherwise be skipped.
        "python": sorted(set(python_inputs) | set(_compose_files_for_tests()) | {"pentest"}),
        "node": ["web", "pentest"],
        "go": sorted(set(all_go) | {"pentest"}),
        "pulumi": sorted(_go_closure("pulumi-provider")),
    }


def _matches(changed: str, pattern: str) -> bool:
    if "*" in pattern:
        return fnmatch.fnmatch(changed, pattern) or fnmatch.fnmatch(
            changed, pattern.rstrip("/") + "/*"
        )
    return changed == pattern or changed.startswith(pattern.rstrip("/") + "/")


def _known_top_level() -> set[str]:
    return {p.name for p in ROOT.iterdir() if not p.name.startswith(".")} | {".github"}


def decide(
    changed: list[str], fams: dict[str, list[str]] | None = None
) -> tuple[dict[str, bool], str]:
    """-> ({family: run?}, reason).  Any doubt resolves to running everything."""
    fams = fams if fams is not None else families()
    run_all = dict.fromkeys(fams, True)

    if not changed:
        return run_all, "no changed files could be computed"

    known = _known_top_level()
    for path in changed:
        if path.startswith(FORCE_ALL_PREFIXES):
            return run_all, f"{path} changes how CI itself runs"
        top = path.split("/", 1)[0]
        if top not in known:
            return run_all, f"{path} is under an unrecognised top-level path"

    out = {}
    for name, patterns in fams.items():
        out[name] = any(_matches(c, p) for c in changed for p in patterns)
    return out, "derived from the changed paths"


# --------------------------------------------------------------------------
# Self-test.  Literal fixtures, asserted before the real decision runs, so a
# narrowed matcher fails here naming the case rather than quietly shrinking what
# is checked -- a count over the tree cannot tell those two apart.
# --------------------------------------------------------------------------
_FIXTURES: list[tuple[list[str], dict[str, bool], str]] = [
    (
        ["README.md"],
        {"python": False, "node": False, "go": False, "pulumi": False},
        "a root doc affects nothing",
    ),
    (
        [".github/workflows/ci.yml"],
        {"python": True, "node": True, "go": True, "pulumi": True},
        "the workflow that defines these jobs forces everything",
    ),
    (
        [".github/actions/ghcr-login/action.yml"],
        {"python": True, "node": True, "go": True, "pulumi": True},
        "a composite action ci.yml calls forces everything",
    ),
    # The narrowing itself: an unrelated workflow must NOT run the lot.
    (
        [".github/workflows/release-rescan.yml"],
        {"python": False, "node": False, "go": False, "pulumi": False},
        "a scheduled workflow unrelated to the PR jobs affects nothing",
    ),
    (
        [".github/dependabot.yml"],
        {"python": False, "node": False, "go": False, "pulumi": False},
        "and nor does dependabot's config",
    ),
    (
        ["scripts/lib.sh"],
        {"python": True, "node": True, "go": True, "pulumi": True},
        "scripts/ forces everything",
    ),
    # A path whose top-level component has GONE AWAY. Deliberately not a
    # brand-new directory: `actions/checkout` gives a pull request the merge
    # ref, so a directory the pull request adds is already on disk and is
    # therefore "known" -- the check cannot fire for that case, and a fixture
    # asserting it would pass only by the name happening not to exist.
    (
        ["a-directory-that-was-deleted/x.py"],
        {"python": True, "node": True, "go": True, "pulumi": True},
        "a top-level path that is no longer in the tree forces everything",
    ),
    (
        ["services/terrapod/api/app.py"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "API source is the Python tier only",
    ),
    (
        ["web/src/app/workspaces/page.tsx"],
        {"python": True, "node": True, "go": False, "pulumi": False},
        "this web path is COPYed into the test image, so it is Python too",
    ),
    (
        ["web/src/components/nav.tsx"],
        {"python": False, "node": True, "go": False, "pulumi": False},
        "a web path outside the COPY set is node only",
    ),
    (
        ["web/messages/de.json"],
        {"python": True, "node": True, "go": False, "pulumi": False},
        "the locale catalogues are read by the Python i18n gates",
    ),
    (
        ["go-terrapod/runs.go"],
        {"python": True, "node": False, "go": True, "pulumi": True},
        "the SDK is COPYed into the test image and replaced by pulumi-provider",
    ),
    (
        ["mcp/internal/mcpserver/crud.go"],
        {"python": True, "node": False, "go": True, "pulumi": False},
        "crud.go is COPYed into the test image",
    ),
    (
        ["mcp/internal/mcpserver/act.go"],
        {"python": False, "node": False, "go": True, "pulumi": False},
        "another mcp file is not",
    ),
    (
        ["helm/terrapod/values.yaml"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "the chart is COPYed into the test image for the helm tests",
    ),
    (
        ["docs/index.md"],
        {"python": False, "node": False, "go": False, "pulumi": False},
        "an ordinary doc affects nothing",
    ),
    (
        ["docs/deprecations.md"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "this one doc IS COPYed into the test image",
    ),
    (
        ["ee-agent/main.go"],
        {"python": False, "node": False, "go": True, "pulumi": False},
        "ee-agent source is Go only -- only its go.mod is COPYed",
    ),
    (
        ["ee-agent/go.mod"],
        {"python": True, "node": False, "go": True, "pulumi": False},
        "...and its go.mod is",
    ),
    # The base image installs the Python dependency closure, so a lockfile bump
    # changes what every test runs against while touching no test and no source.
    # Found by mutation: without this case, dropping Dockerfile.test-base from
    # the derivation passed, and `deps(python): bump ...` would have skipped the
    # entire Python tier.
    (
        ["services/poetry.lock"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "the lockfile is COPYed into the test BASE image",
    ),
    (
        ["services/pyproject.toml"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "and so is pyproject.toml",
    ),
    # An empty list means the diff could not be computed, not that nothing
    # changed -- the two are indistinguishable to the caller and only one of
    # them is safe to act on.
    (
        [],
        {"python": True, "node": True, "go": True, "pulumi": True},
        "an empty changed-file list forces everything",
    ),
    # `python-test` is one step: `scripts/test.sh python -- ...`, and that script
    # brings the tier up with this file. Nothing bakes it into an image, so the
    # Dockerfile-derived set cannot see it; without this case a bump of the
    # postgres tag, or an edit to the storage backend, skips every Python slice
    # while changing what they run against.
    (
        ["docker-compose.test.yml"],
        {"python": True, "node": False, "go": False, "pulumi": False},
        "the compose file IS the Python tier's runtime",
    ),
    # Removing a suppression is the change, and the audit that would prove the
    # finding has gone is the job that would otherwise be skipped.
    (
        ["pentest/pip-audit-ignore.txt"],
        {"python": True, "node": True, "go": True, "pulumi": False},
        "the accepted-risk register is read by three audits",
    ),
]


def self_test(fams: dict[str, list[str]]) -> None:
    for changed, expected, why in _FIXTURES:
        got, _ = decide(changed, fams)
        if got != expected:
            diff = {k: (expected[k], got[k]) for k in expected if expected[k] != got[k]}
            raise SystemExit(
                f"ci-affected self-test FAILED: {why}\n"
                f"  changed={changed}\n  expected/got for the families that differ: {diff}"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--changed-file", help="file holding one changed path per line")
    ap.add_argument("--show", action="store_true", help="print the derived input sets and exit")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="assert the fixtures and exit; run by the docs-audit gate, NOT on "
        "the CI hot path -- `prepare` is the root of the job graph, so a failure "
        "there reds the whole run with the reason buried in one job's log",
    )
    args = ap.parse_args()

    try:
        fams = families()
    except Exception as exc:  # a dependency set we cannot vouch for
        print(f"could not derive the input sets ({exc}) -- running everything", file=sys.stderr)
        # The names come from the fixtures rather than a second hand-written
        # list: a list here that fell behind `families()` would emit nothing for
        # the missing family, and an absent output is the empty string, so that
        # job would be skipped on every run where the derivation failed.
        for name in sorted({k for _, expected, _ in _FIXTURES for k in expected}):
            print(f"run_{name}=true")
        return 0

    if args.self_test:
        self_test(fams)
        print(f"PASS — {len(_FIXTURES)} fixture(s) over {len(fams)} derived families.")
        return 0

    if args.show:
        self_test(fams)
        for name, patterns in fams.items():
            print(f"{name}:")
            for pattern in patterns:
                print(f"    {pattern}")
        return 0

    changed: list[str] = []
    if args.changed_file:
        path = pathlib.Path(args.changed_file)
        if path.exists():
            changed = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]

    decisions, reason = decide(changed, fams)
    print(f"# {len(changed)} changed path(s); {reason}", file=sys.stderr)
    for name in sorted(decisions):
        state = "RUN " if decisions[name] else "skip"
        print(f"#   {state} {name}", file=sys.stderr)
        print(f"run_{name}={'true' if decisions[name] else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
