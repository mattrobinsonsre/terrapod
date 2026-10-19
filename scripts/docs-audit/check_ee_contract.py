#!/usr/bin/env python3
"""The EE contract has one producer, and every reader of it still reads it.

`docker/ee-contract.sh` states what an Ansible execution environment must
provide (#2154). It is asserted twice, deliberately, because build time and
published are different questions:

* `docker/Dockerfile.ee` runs it mid-build, so a broken image cannot be pushed;
* `.github/workflows/ee-build.yml` runs it against the **pushed digest**, so
  what the registry actually serves is checked too -- a push can half-succeed,
  a tag can be moved, and an operator only ever gets the published bytes.

Dropping either halves the coverage while leaving the other green, which is the
shape this repository keeps meeting: a declaration whose only evidence of being
read is a comment saying it is read. So the gate is derived -- it finds the
readers rather than trusting a list -- and carries a floor, because a matcher
that stops matching must fail rather than pass on nothing.

It lives here rather than in the pytest tiers for the reason
`check_token_mints.py` gives: `docker/Dockerfile.test` does not ship
`.github/`, so a test there could not see the half that regressed.

There is a THIRD statement of the contract, and it is deliberately not the
same one: `ee-agent/contract.go` checks a narrower version at the EE's startup
probe, at runtime, for every image including an operator-supplied one that has
never been through our build. It cannot assert the things only we can require
of our own images -- a non-root uid, a probe playbook -- and it should not run a
playbook on every pod start. So the two differ by design, in one direction
only: whatever the agent *requires* must be something our own images are
*checked* for, or an image of ours could pass its build and then fail its own
startup probe. That containment is asserted here, derived from both files.

`docs/versioning-and-support.md` states the same contract a fourth time, in
prose, as the thing that makes an EE insensitive to a Terrapod release. A list
written in prose has no gate behind it and drifts in the direction that is worse
-- an operator builds an image against a sentence that is no longer true -- so
the same containment is asserted against that paragraph.

What it deliberately cannot check: that the contract itself still asserts
anything useful. Emptying the script passes every reader check. The build is
the backstop for that -- an empty contract would stop catching the defects
#2154's first build hit, which is a different failure from this one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path("docker/ee-contract.sh")

#: Every file that may legitimately execute the contract, with what makes its
#: reading correct. Derived checking happens below; this names the surfaces so a
#: silently-removed one is a failure rather than a smaller number.
READERS = {
    Path("docker/Dockerfile.ee"): "the build, so a broken image is never pushed",
    Path(".github/workflows/ee-build.yml"): "the published digest, post-push",
}

#: A matcher that stops matching must fail. Two readers is the whole point.
MIN_READERS = 2

#: Likewise for the agent's own list, read out of Go source by regex.
MIN_AGENT_BINARIES = 4

#: Where the contract is stated in prose, and the bullet that states it.
POLICY_PAGE = Path("docs/versioning-and-support.md")
POLICY_BULLET = "- **runner \u2194 execution environment:**"


def main() -> int:
    contract = ROOT / CONTRACT
    if not contract.is_file():
        print(f"FAIL — {CONTRACT} does not exist; nothing states the EE contract")
        return 1

    problems: list[str] = []
    found = 0

    for rel, why in READERS.items():
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"{rel} is missing — it is meant to assert the contract ({why})")
            continue
        text = path.read_text()
        if CONTRACT.name not in text:
            problems.append(
                f"{rel} no longer references {CONTRACT.name}, so it asserts nothing about {why}"
            )
            continue
        found += 1

    # The build must RUN it, not merely mention it in a comment, and must not
    # COPY it: this image ships no Terrapod artefacts, which is what keeps it
    # insensitive to a Terrapod release.
    dockerfile = ROOT / "docker/Dockerfile.ee"
    if dockerfile.is_file():
        body = dockerfile.read_text()
        directives = [
            ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
        ]
        runs_it = any(re.search(rf"^RUN\b.*{re.escape(CONTRACT.name)}", ln) for ln in directives)
        if not runs_it:
            problems.append(
                f"docker/Dockerfile.ee references {CONTRACT.name} but no RUN executes it"
            )
        copies_it = any(
            re.search(rf"^(COPY|ADD)\b.*{re.escape(CONTRACT.name)}", ln) for ln in directives
        )
        if copies_it:
            problems.append(
                f"docker/Dockerfile.ee COPYs {CONTRACT.name} into a layer; bind-mount it "
                "instead, so the published image carries no Terrapod file"
            )

    # The conformance step must address the image BY DIGEST. Against a moving
    # `X.Y` tag it would assert whatever that tag pointed at before this build,
    # which passes while saying nothing about what was just pushed.
    workflow = ROOT / ".github/workflows/ee-build.yml"
    if workflow.is_file():
        wf = workflow.read_text()
        step = next(
            (
                block
                for block in wf.split("\n      - ")
                if CONTRACT.name in block and "docker run" in block
            ),
            None,
        )
        if step is None:
            problems.append(
                f".github/workflows/ee-build.yml references {CONTRACT.name} but no step "
                "feeds it to `docker run`"
            )
        elif "outputs.digest" not in step:
            problems.append(
                "the conformance step does not address the image by digest "
                "(`steps.push.outputs.digest`); against a moving tag it would check "
                "the previous image"
            )

    # The agent's runtime requirement must be a SUBSET of what the build
    # asserts. The other direction is fine and expected -- the build requires
    # more, because it is allowed to.
    agent = ROOT / "ee-agent/contract.go"
    if not agent.is_file():
        problems.append(
            "ee-agent/contract.go is missing; nothing checks an operator-supplied EE at startup"
        )
    else:
        src = agent.read_text()
        declared = re.search(r"contractBinaries\s*=\s*\[\]string\{([^}]*)\}", src)
        if declared is None:
            problems.append(
                "cannot find `contractBinaries` in ee-agent/contract.go, so the agent's "
                "requirement cannot be compared with the build's"
            )
        else:
            required = set(re.findall(r'"([^"]+)"', declared.group(1)))
            # Plus anything it runs outright -- `ansible` is required by being
            # exec'd rather than by being looked up, which a list-only read misses.
            required |= set(re.findall(r'exec\.Command\(\s*"([^"]+)"', src))
            if len(required) < MIN_AGENT_BINARIES:
                problems.append(
                    f"only derived {len(required)} required binary/binaries from "
                    f"ee-agent/contract.go; expected at least {MIN_AGENT_BINARIES}, so "
                    "this matcher has stopped matching"
                )
            body = contract.read_text()
            unchecked = sorted(b for b in required if b not in body)
            if unchecked:
                problems.append(
                    "the agent requires "
                    + ", ".join(unchecked)
                    + f" at an EE's startup, but {CONTRACT} does not check for it, so one "
                    "of our own images could pass its build and fail its own startup probe"
                )

            # And the prose, which is what an operator building their own EE
            # reads. Scoped to the bullet that states the contract, so a
            # mention anywhere else in the page cannot satisfy it.
            page = ROOT / POLICY_PAGE
            if not page.is_file():
                problems.append(f"{POLICY_PAGE} is missing; nothing states the EE contract")
            else:
                text = page.read_text()
                start = text.find(POLICY_BULLET)
                if start < 0:
                    problems.append(
                        f"{POLICY_PAGE} no longer has the `{POLICY_BULLET}` bullet, so the "
                        "contract an operator builds an EE against is stated nowhere"
                    )
                else:
                    end = text.find("\n- **", start + len(POLICY_BULLET))
                    bullet = text[start : end if end > 0 else len(text)]
                    absent = sorted(b for b in required if f"`{b}`" not in bullet)
                    if absent:
                        problems.append(
                            f"{POLICY_PAGE}'s EE-contract bullet does not name "
                            + ", ".join(absent)
                            + ", which the agent requires at startup; an operator building "
                            "an EE from that paragraph would build a non-conforming image"
                        )

    if found < MIN_READERS:
        problems.append(
            f"only found {found} reader(s) of {CONTRACT.name}; expected at least "
            f"{MIN_READERS} (the build and the published digest)"
        )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the EE contract:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"PASS — {found} reader(s) of {CONTRACT.name}: the build and the published digest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
