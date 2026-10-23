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

#: Every place the contract is stated in PROSE, and the heading or bullet that
#: states it, so a mention elsewhere on the page cannot satisfy the check.
#:
#: FIVE, because each is read by a different person at a different moment and
#: every one of them has drifted at least once:
#:
#: * `docs/versioning-and-support.md` states it as the thing that makes an EE
#:   insensitive to a Terrapod release;
#: * `docs/runners.md` is what somebody BUILDING an image reads. It was ungated
#:   and had drifted -- it still named the 1.x requirement ("a uid that can read
#:   the shared volume") after #1928 narrowed it to uid 1000, so an operator
#:   following it would have built an image the startup probe refuses;
#: * `helm/terrapod/values.yaml` is what an operator reads at the moment they
#:   configure this;
#: * `services/terrapod/config.py` is the same statement on the config schema,
#:   which is where it reaches an operator who never opens the chart;
#: * `llms.txt` is what an AGENT reads, and it is the one that proves the gate
#:   needed widening rather than the pages needing fixing: it was outside PROSE
#:   when #1928 pinned the uid, so it went on telling an agent the contract was
#:   "a uid that can read the shared volume" while the probe demanded 1000 --
#:   and the sibling credential bullet still gave the WITHDRAWN REASONING for
#:   group-readable files ("the EE sidecar deliberately pins no `runAsUser`"),
#:   which by then was the opposite of the truth.
#:
#: Fixing the instances and leaving the gate at three is how the next one
#: happens, so the gate grew with them.
#: path -> (where the statement starts, where it ends). An explicit end rather
#: than one inferred from the marker's shape, because two of these have no
#: heading structure at all -- a YAML comment block and a Python string.
PROSE: dict[Path, tuple[str, str]] = {
    Path("docs/versioning-and-support.md"): (
        "- **runner \u2194 execution environment:**",
        "\n- **",
    ),
    Path("docs/runners.md"): ("#### What your image must provide", "\n#### "),
    Path("helm/terrapod/values.yaml"): (
        "# Each image must carry",
        "execution_environments: {}",
    ),
    Path("services/terrapod/config.py"): (
        "Operator-supplied execution environments as label -> image URI",
        "availability_ttl_seconds",
    ),
    Path("llms.txt"): (
        "- [Ansible Execution Environments]",
        "\n- [",
    ),
}


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

            # The uid is a requirement of a different KIND, and a derivation that
            # only walks binaries is blind to it -- which is the shape of gate
            # failure this repository keeps meeting. Derived from the agent's own
            # constant so it cannot drift from what the probe enforces.
            uid_decl = re.search(r"requiredUID\s*=\s*(\d+)", src)
            required_uid = uid_decl.group(1) if uid_decl else None
            if required_uid is None:
                problems.append(
                    "cannot find `requiredUID` in ee-agent/contract.go, so the uid the "
                    "probe enforces cannot be compared with what the build and the docs "
                    "promise"
                )
            # Matched as a TEST, not as a mention. A bare `1000 in text` is
            # satisfied by any incidental occurrence -- a comment, an example,
            # the failure message of a different check -- so deleting the real
            # assertion survived it. Measured: it did.
            elif not re.search(
                rf'"\$uid"\s*=\s*"{required_uid}"', contract.read_text()
            ):
                problems.append(
                    f"the agent requires uid {required_uid} at an EE's startup, but "
                    f"{CONTRACT} does not TEST for it (expected a `[ \"$uid\" = "
                    f"\"{required_uid}\" ]`), so one of our own images could pass its "
                    "build and fail its own startup probe"
                )
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

            # And the prose -- every statement of it, because an operator
            # building an EE reads one of them and has no way to know which is
            # current. Scoped to the block that states the contract, so a
            # mention elsewhere on the page cannot satisfy it.
            for rel, (marker, terminator) in PROSE.items():
                page = ROOT / rel
                if not page.is_file():
                    problems.append(f"{rel} is missing; it states the EE contract")
                    continue
                text = page.read_text()
                start = text.find(marker)
                if start < 0:
                    problems.append(
                        f"{rel} no longer has `{marker}`, so the contract an operator "
                        "builds an EE against is not stated there any more"
                    )
                    continue
                stop = text.find(terminator, start + len(marker))
                block = text[start : stop if stop > 0 else len(text)]
                absent = sorted(b for b in required if f"`{b}`" not in block)
                if absent:
                    problems.append(
                        f"{rel} does not name "
                        + ", ".join(absent)
                        + ", which the agent requires at startup; an operator building an "
                        "EE from that section would build a non-conforming image"
                    )
                # As a REQUIREMENT, not a mention. These sections legitimately
                # contain the number several times -- a Dockerfile example, a
                # `docker run --user`, the prose about a missing passwd entry --
                # so a substring check passes with the requirement sentence
                # gutted. Measured: changing "run as uid 1000" to "run as a uid"
                # survived the substring form.
                # The subject has to be the IMAGE. Two different requirements
                # phrase alike -- "the image must run as uid 1000" and "`ansible`
                # must be runnable as uid 1000" -- so a plain search is satisfied
                # by the second while the first is gutted. Measured: it was, and
                # the mutation that removes the image requirement from `llms.txt`
                # SURVIVED until this excluded an `ansible` subject.
                #
                # `re.IGNORECASE` because `llms.txt` writes the requirement in
                # CAPS for emphasis throughout. It costs nothing -- what is
                # required is the whole phrase, not the number.
                uid_claims = [
                    m
                    for m in re.finditer(
                        rf"\b(?:run|runs|running|runnable|ability to run)\s+as\s+"
                        rf"(?:the\s+)?uid\s+{required_uid}\b",
                        block,
                        re.IGNORECASE,
                    )
                    if not re.search(r"`?ansible`?\s*$", block[max(0, m.start() - 20) : m.start()])
                ]
                if required_uid and not uid_claims:
                    problems.append(
                        f"{rel} does not state that an EE must RUN AS uid {required_uid} "
                        "(the number appearing somewhere in the section is not the same "
                        "thing); an operator building an EE from it would build one that "
                        "is refused at pod start"
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
