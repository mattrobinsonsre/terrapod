#!/usr/bin/env python3
"""Hold `ci.yml` and `scripts/ci-affected.py` to each other.

`ci-affected.py` decides which job families a change cannot affect; this asserts
the workflow consumes that decision coherently.  Three properties, each of which
fails SILENTLY if it drifts -- a skipped job and a passing job look identical in
the checks list, which is the whole reason this file exists.

1.  EVERY JOB IS ACCOUNTED FOR.  A job is either gated on a family or listed in
    `ALWAYS_RUN` with a reason.  Without this a new job simply sits outside the
    system, and nobody finds out, because running too much is invisible.

2.  THE FAMILY NAMES AGREE.  Every `run_<family>` the workflow reads is one the
    script emits, and every family the script emits is read by at least one job.
    A typo in either direction yields an empty expression, which GitHub treats
    as false -- so the job would be skipped on every single run.

3.  A GATED JOB'S CONSUMERS SURVIVE IT BEING SKIPPED.  This is the one that
    would break the build rather than weaken it, and it has two halves that are
    easy to conflate:

      * the CASCADE.  GitHub skips any job whose dependency was skipped, unless
        that job's `if` contains a status function (`always()`, `!cancelled()`,
        `failure()`).  So a consumer that merely never mentions the dependency
        is NOT safe -- it silently skips too, and so does everything behind it.
        That is the shape that nearly shipped here: gating `build-query` looks
        harmless because no `if` references it, and it would have cascade-skipped
        the image builds and with them E2E, eval-boot, the CLI contract, the
        air-gap gate and OCI conformance.
      * the WEDGE.  A consumer that does run but requires
        `result == 'success'` fails instead.

    Both are checked, because a consumer needs to clear both to be safe.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

# Jobs that deliberately run whatever changed. The reason is required: an entry
# with no reason is how a job that SHOULD be gated hides in here.
ALWAYS_RUN: dict[str, str] = {
    "prepare": "computes the decision; cannot depend on it",
    # Named "python-checks", and it is 34 docs-audit gates plus two ruff steps.
    # Gating it on run_python disables every documentation gate on exactly the
    # changes that need them most -- a docs-only pull request, where
    # check_links, check_counts and check_references are the whole point. It was
    # gated in the first draft of this change and this entry is the correction.
    "python-checks": "hosts the docs-audit gates, which read the whole tree",
    "build-test-base": "publishes the base image the Python tier pulls",
    "ci-pass": "the required check; must always report",
    "quality-gate": "aggregates every result",
    "create-manifests": "writes the sha- manifest a later tag retags",
    "secret-scan": "scans the whole tree",
    "dependency-review": "reads the dependency graph, not our paths",
    "host-checks": "the docs-audit gates read the whole tree",
    "sast": "semgrep scans every language at once",
    "codeql": "one matrix over three languages; gating needs a dynamic matrix",
    "build-query": "publishes a binary the image builds download",
    "build-ee-agent": "publishes a binary the image builds download",
    "build-images-amd64": "five downstream jobs load its artifacts",
    "build-images-arm64": "its manifests are the release's retag source",
    # Two reasons, and the first is the one that binds: the scan matrix is
    # per-image and consumes the build matrix, so gating it means shrinking
    # that, which starves the five jobs loading the image artifacts. Second, a
    # scan reads the live advisory database rather than the image alone.
    "scan-images": "scans the built images; its matrix consumes the build matrix",
    "e2e-test": "boots the whole stack from the PR's image artifacts",
    "e2e-report": "merges the e2e shards' reports",
    "helm-eval-boot": "boots the whole stack",
    "cli-contract": "drives the real CLI against a booted stack",
    "airgap-gate": "boots the stack with the network withdrawn",
    "oci-conformance": "drives the registry spec suite against a booted stack",
    "ssh-target-gate": "drives a real ssh target",
    "git-source-gate": "drives a real git source",
    "cleanup-tag": "must fire precisely when a release failed",
}

MIN_GATED = 6  # below this the wiring has plainly come undone


def _families_from_script() -> set[str]:
    """What `ci-affected.py` emits, read from the script rather than imported.

    Importing it would be fine today, but this job installs no Terrapod
    dependencies and the import would become a liability the moment that script
    grew one.
    """
    src = (ROOT / "scripts" / "ci-affected.py").read_text()
    m = re.search(r"^def families\(\).*?^    return \{(.*?)^    \}", src, re.S | re.M)
    if not m:
        raise SystemExit(
            "could not find families() in scripts/ci-affected.py -- teach this "
            "reader the new shape; do NOT hardcode the family list here"
        )
    names = set(re.findall(r'^\s*"([a-z_]+)":', m.group(1), re.M))
    if not names:
        raise SystemExit("families() returned no recognisable keys")
    return names


def _raw_job_blocks() -> dict[str, str]:
    """Each job's own slice of the workflow SOURCE, header to next header.

    Deliberately not `yaml.dump` of the parsed job: a `run:` block round-trips
    as a double-quoted scalar with escaped quotes, so `== "failure"` does not
    appear literally in the dump and a matcher over it silently finds nothing.
    """
    lines = CI.read_text().splitlines()
    heads = [(i, ln[2:-1]) for i, ln in enumerate(lines) if re.match(r"^  [a-z0-9-]+:$", ln)]
    out: dict[str, str] = {}
    for n, (start, name) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        out[name] = "\n".join(lines[start:end])
    return out


def _tolerates_skipped(consumer: dict, dep: str, raw: str) -> bool:
    """Does `consumer` accept `dep` being skipped?

    Three shapes are in use and each is matched explicitly rather than by
    anything looser, so this can still fail:

      (a) `(needs.X.result == 'success' || needs.X.result == 'skipped')` in the
          `if` -- what `create-manifests` uses;
      (b) a shell loop over a RESULTS list with a `success|skipped)` case --
          what `quality-gate` uses;
      (c) a loop that rejects ONLY `"failure"` -- what `ci-pass` uses, which is
          deliberately more permissive still (it tolerates `cancelled` too).

    A consumer that mentions the dependency but matches none of the three is
    the finding: it is requiring something the gated job will not supply.
    """
    cond = str(consumer.get("if", ""))

    # Half one: does the consumer run at all when a dependency is skipped?
    # Without a status function GitHub skips it, whatever its condition says.
    if not re.search(r"always\s*\(\s*\)|!\s*cancelled\s*\(\s*\)|failure\s*\(\s*\)", cond):
        return False

    # Half two: having run, does it accept this dependency's `skipped`?
    if re.search(rf"needs\.{re.escape(dep)}\.result\s*==\s*'skipped'", cond):
        return True
    if f"needs.{dep}.result" not in raw:
        return False
    if re.search(r"success\s*\|\s*skipped", raw):
        return True
    return re.search(r'==\s*"failure"', raw) is not None


def main() -> int:
    wf = yaml.safe_load(CI.read_text())
    jobs = wf["jobs"]
    script_families = _families_from_script()
    raw_blocks = _raw_job_blocks()
    problems: list[str] = []

    gated: dict[str, str] = {}
    for name, job in jobs.items():
        found = re.findall(r"needs\.prepare\.outputs\.run_([a-z_]+)", str(job.get("if", "")))
        if found:
            gated[name] = found[0]

    # 1. every job accounted for
    for name in jobs:
        if name in gated or name in ALWAYS_RUN or name.startswith("release-"):
            continue
        problems.append(
            f"{name}: neither gated on a run_<family> nor listed in ALWAYS_RUN. "
            f"Gate it, or add it to ALWAYS_RUN with the reason it must always run."
        )
    for name, reason in ALWAYS_RUN.items():
        if name not in jobs:
            problems.append(f"ALWAYS_RUN names '{name}', which is not a job in ci.yml")
        elif not reason.strip():
            problems.append(f"ALWAYS_RUN['{name}'] has no reason")
        elif name in gated:
            problems.append(f"{name} is both gated and in ALWAYS_RUN")

    # 2. the family names agree, in both directions
    used = set(gated.values())
    for fam in sorted(used - script_families):
        problems.append(
            f"ci.yml reads run_{fam}, which scripts/ci-affected.py does not emit "
            f"-- the expression would be empty, i.e. the job would never run"
        )
    for fam in sorted(script_families - used):
        problems.append(
            f"scripts/ci-affected.py emits run_{fam}, which no job reads -- "
            f"either gate a job on it or stop computing it"
        )

    # 3. a gated job's consumers tolerate it being skipped
    for name, job in jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else (needs or [])
        for dep in needs:
            if dep in gated and not _tolerates_skipped(job, dep, raw_blocks.get(name, "")):
                problems.append(
                    f"{name} needs the gated job {dep} but does not survive it being "
                    f"skipped. Either {name} has no status function in its `if` (so "
                    f"GitHub cascade-skips it, and everything behind it), or it "
                    f"requires {dep} to have succeeded. Gating {dep} would silently "
                    f"disable {name} on every run where the family is unaffected."
                )

    if len(gated) < MIN_GATED:
        problems.append(
            f"only {len(gated)} gated job(s), below the floor of {MIN_GATED} -- "
            f"the wiring has come undone rather than the gates being unnecessary"
        )

    if problems:
        print("check_ci_affected: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"PASS — {len(gated)} gated job(s) across {len(script_families)} families; "
        f"{len(ALWAYS_RUN)} always-run job(s), each with a reason."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
