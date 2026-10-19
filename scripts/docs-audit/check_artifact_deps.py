#!/usr/bin/env python3
"""Every downloaded artifact must come from a job this job depends on.

A workflow job that runs `actions/download-artifact` is reading a file another
job uploaded. If it does not declare that job in `needs`, the two are merely
*usually* ordered favourably by the scheduler -- and a race that usually wins is
worse than one that always loses, because it goes green until it does not.

That is not hypothetical. On 2026-10-19 the runner image build downloaded
`terrapod-ee-agent-linux-<arch>` while declaring only
`needs: [prepare, build-query]`. The arm64 image job started fifteen seconds
BEFORE the arm64 agent job and failed with "Artifact not found"; the amd64 pair
passed in the very same run because its agent happened to finish twenty-seven
seconds earlier. Nothing in the workflow had changed -- only the ordering.

The producer and the consumer need not spell the artifact the same way: the
agent uploads `…-${{ matrix.goarch }}` while each image job hardcodes its own
arch. So the matrix is expanded on both sides before they are compared, which
is the whole reason this cannot be a grep.
"""

from __future__ import annotations

import itertools
import pathlib
import re
import sys

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: A floor, so a parser that stops matching fails instead of passing on nothing
#: -- the failure mode every derived gate here is written against.
MIN_DOWNLOADS = 4

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _matrix_values(job: dict) -> dict[str, list[str]]:
    """The matrix dimensions a name template could interpolate.

    `include:` entries are folded in as additional candidate values rather than
    as a cross product: GitHub EXTENDS a matrix with `include` instead of
    crossing it, and over-generating candidates here is harmless because this
    only widens what a producer is credited with.
    """
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in matrix.items():
        if key in ("include", "exclude"):
            continue
        if isinstance(value, list):
            out[key] = [str(v) for v in value]
    for entry in matrix.get("include") or []:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if isinstance(value, (str, int, float)):
                out.setdefault(key, []).append(str(value))
    return {k: sorted(set(v)) for k, v in out.items()}


def _expand(name: str, job: dict) -> list[str]:
    """Every concrete artifact name this template can produce in this job."""
    refs = _MATRIX_REF.findall(name)
    if not refs:
        return [name]
    values = _matrix_values(job)
    axes = [values.get(ref) or [] for ref in refs]
    if any(not axis for axis in axes):
        # A dimension we cannot resolve. Return nothing rather than a half
        # expansion: crediting a producer with a name it may not emit would
        # silently excuse a missing dependency, which is the wrong direction.
        return []
    out = []
    for combo in itertools.product(*axes):
        expanded = name
        for ref, value in zip(refs, combo, strict=True):
            expanded = re.sub(r"\$\{\{\s*matrix\." + re.escape(ref) + r"\s*\}\}", value, expanded)
        out.append(expanded)
    return out


def _artifact_steps(job: dict, action: str) -> list[tuple[str, str]]:
    """(step name, artifact name) for each use of an upload/download action."""
    found = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses") or "")
        if f"actions/{action}-artifact" not in uses:
            continue
        with_ = step.get("with") or {}
        name = with_.get("name")
        if not isinstance(name, str):
            # `pattern:`/`merge-multiple:` downloads, or an upload with no
            # explicit name. Not compared, and said out loud rather than
            # silently skipped.
            found.append((str(step.get("name") or uses), ""))
            continue
        found.append((str(step.get("name") or uses), name))
    return found


def _closure(job_id: str, jobs: dict[str, dict]) -> set[str]:
    seen: set[str] = set()
    stack = list(_as_list(jobs.get(job_id, {}).get("needs")))
    while stack:
        current = stack.pop()
        if current in seen or current not in jobs:
            continue
        seen.add(current)
        stack.extend(_as_list(jobs[current].get("needs")))
    return seen


def main() -> int:
    offenders: list[str] = []
    unresolved: list[str] = []
    downloads = 0

    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            continue
        jobs = {k: v for k, v in (doc.get("jobs") or {}).items() if isinstance(v, dict)}

        producers: dict[str, set[str]] = {}
        for job_id, job in jobs.items():
            for _, name in _artifact_steps(job, "upload"):
                if not name:
                    continue
                for expanded in _expand(name, job):
                    producers.setdefault(expanded, set()).add(job_id)

        for job_id, job in jobs.items():
            deps = _closure(job_id, jobs)
            for step_name, name in _artifact_steps(job, "download"):
                if not name:
                    unresolved.append(f"{path.name}:{job_id}: {step_name} (no literal name)")
                    continue
                expansions = _expand(name, job)
                if not expansions:
                    unresolved.append(f"{path.name}:{job_id}: {step_name} ({name})")
                    continue
                for expanded in expansions:
                    downloads += 1
                    owners = producers.get(expanded, set())
                    if not owners:
                        # Produced by nothing in this workflow -- another
                        # workflow's run, or a typo. Reported, not failed.
                        unresolved.append(f"{path.name}:{job_id}: {expanded} has no producer here")
                        continue
                    if not (owners & deps):
                        offenders.append(
                            f"{path.name}: job '{job_id}' downloads '{expanded}' "
                            f"produced by {sorted(owners)}, which is not in its needs "
                            f"closure {sorted(deps)}"
                        )

    for line in unresolved:
        print(f"  note: {line}")

    if downloads < MIN_DOWNLOADS:
        print(
            f"FAIL — only {downloads} artifact download(s) inspected; the parser has "
            f"stopped matching (floor is {MIN_DOWNLOADS})"
        )
        return 1

    if offenders:
        print(f"FAIL — {len(offenders)} artifact download(s) without a declared dependency:\n")
        for line in offenders:
            print(f"  {line}")
        print(
            "\nAdd the producing job to `needs`. Without it the ordering is the "
            "scheduler's choice, so the job passes until the day it does not."
        )
        return 1

    print(f"PASS — {downloads} artifact download(s), every one from a job in `needs`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
