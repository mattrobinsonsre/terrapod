#!/usr/bin/env python3
"""Every Helm-values snippet in the docs must survive `helm lint`.

WHY THIS EXISTS. `docs/pulumi.md` opened with a copy-pasteable block telling an
operator to set `api.config.engines.pulumi.enabled: true`. That switch was
withdrawn before 2.0 (#1986) and `values.schema.json` sets
`additionalProperties: false`, so the snippet did not quietly no-op --- it FAILED
`helm lint` and `helm upgrade`:

    at '/api/config': additional properties 'engines' not allowed

Following the page broke the install. Worse, `docs/upgrading-to-2.0.md` told
operators to REMOVE that exact block, so two pages gave opposite instructions and
the copy-pasteable one was wrong. A cold read of the repository found it; nothing
in CI could, because every existing gate reads prose, counts or links. None of
them had an opinion about whether a fenced YAML block is a thing an operator can
actually apply.

HELM IS THE AUTHORITY, NOT A REIMPLEMENTATION. The schema carries 122
`additionalProperties: false` clauses across ten `$defs`, and a hand-rolled key
walker would be a second implementation of the thing that rejects --- wrong in
ways nobody could see. So this writes each snippet to a file and runs the real
`helm lint`, which is exactly how the defect above was proven.

WHICH BLOCKS COUNT, derived rather than listed: a fenced `yaml` block is a Helm
values snippet when its top-level keys are all declared at the root of
`values.schema.json`. That is read from the schema itself, so a new top-level
section is covered the day it is added. A block mixing a known root key with an
unknown one is still checked, and failing is correct: that is the defect shape.
A block with NO known root key is some other kind of YAML (a Kubernetes object,
an `ansible.cfg` fragment, an inventory) and is skipped.

SELF_TEST, AND WHY THIS GATE PARTICULARLY NEEDED ONE (#2425 §2.7). `MIN_BLOCKS`
proves some blocks were classified as Helm values; it has nothing to say about
the four separate judgements between the fence and the verdict --- which blocks
are ours, which are marked for removal, what helm's answer meant, and what that
answer means for a marked block versus an unmarked one. Three of those are
asymmetric in a way a count cannot see:

* the REMOVAL path is INVERTED, so a bug there fails in the quiet direction. A
  marked block must be REFUSED by the schema; if it lints clean the key still
  exists and the upgrade note is wrong. Only two blocks in the tree are marked,
  and both are currently refused --- so "the inversion still works" rests on two
  data points that would keep passing if the inversion were dropped entirely;
* `classify` reads helm's own message text. A narrowed regex, or helm rewording
  its output, turns every undeclared key into "clean" --- 161 snippets reporting
  OK while the gate checks nothing. No literal fixture of mine can catch that,
  because I would be asserting my own transcription of the message; so the
  fixtures below are paired with a LIVE run of the real binary over three
  literal snippets, one per outcome;
* `MISSING` is a note and `UNDECLARED` is a failure, deliberately, and that
  distinction is a one-line `elif` that nothing else pins.

One hole the fixtures NAME rather than fix, because fixing it would change what
this gate rejects: an `unexplained` outcome --- helm failing for a reason that is
neither an undeclared key nor a missing required one, a template error say --- is
currently ignored, exactly as it was before the outcomes were named. No snippet
in the tree produces one. It is fixtured as `None` to pin today's behaviour and
to make the hole visible rather than implicit.

The removal marker is also deliberately narrow: it reads the imperative verb
(`remove`/`delete`/`drop`) that both real marked blocks use, so `# Removed in
2.0` is NOT read as a marker and such a block would be held to the ordinary
rule. Fixtured as a must-not-match so widening it is a deliberate act.

STDLIB ONLY --- no PyYAML, because classification needs the top-level keys and
nothing more, which a line matcher gets right for the block shapes docs use.
Needs `helm` on PATH, which is why this runs in the chart job rather than beside
the prose gates.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "terrapod"
SCHEMA = CHART / "values.schema.json"

#: A floor, so a matcher that stops matching fails here instead of passing on
#: nothing. There are comfortably more than this in the tree today. It bounds
#: TOTAL breakage only; the fixtures below are what pin each judgement.
MIN_BLOCKS = 5

#: An UNDECLARED key is always a defect: the key does not exist, so the snippet
#: cannot be applied by anybody. A MISSING REQUIRED key is different -- a doc
#: legitimately shows a fragment illustrating one setting -- so that is a note
#: rather than a failure. Both come from helm's own message, so neither is a
#: judgement of ours.
UNDECLARED = re.compile(r"additional propert(?:y|ies).*not allowed")
MISSING = re.compile(r"missing propert(?:y|ies)")

#: A block an upgrade guide prints in order to say "delete this" is EXPECTED not
#: to lint, and the expectation is inverted: if it passes, the key still exists
#: and the upgrade note is wrong. Derived from a marker the docs already use on
#: the block's own first line, so there is no ledger to drift.
REMOVAL = re.compile(r"#.*\b(remove|delete|drop)\b", re.I)

FENCE = re.compile(r"^```ya?ml\s*$")
FENCE_END = re.compile(r"^```\s*$")
#: A top-level mapping key: no leading whitespace, no list dash.
ROOT_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):")

#: (label, markdown, [(lineno, body), ...]) -- which fenced blocks are ours.
SELF_TEST_BLOCKS: list[tuple[str, str, list[tuple[int, str]]]] = [
    ("a yaml fence", "intro\n\n```yaml\napi:\n  config: {}\n```\n", [(4, "api:\n  config: {}")]),
    ("the yml spelling", "```yml\nweb: {}\n```\n", [(2, "web: {}")]),
    (
        "two blocks, each with its own line number",
        "a\n```yaml\nfirst: 1\n```\nb\n```yaml\nsecond: 2\n```\n",
        [(3, "first: 1"), (7, "second: 2")],
    ),
    # Must NOT be read as a values snippet.
    ("a json fence", "```json\n{}\n```\n", []),
    ("a bare fence", "```\napi: x\n```\n", []),
    # Two documented narrownesses, pinned so widening either is deliberate.
    ("an attributed fence is not read today", "```yaml title=values.yaml\napi: x\n```\n", []),
    ("an indented fence is not read today", "  ```yaml\n  api: x\n  ```\n", []),
]

#: (label, body, root keys) -- what decides a block is a Helm values snippet.
SELF_TEST_ROOT_KEYS: list[tuple[str, str, set[str]]] = [
    ("a top-level key", "api:\n  config:\n    log_level: debug", {"api"}),
    ("two top-level keys", "api: {}\nweb: {}", {"api", "web"}),
    ("nested keys are not root keys", "api:\n  config:\n    nested: 1", {"api"}),
    ("a list item is not a root key", "- api: 1", set()),
    ("a comment is not a root key", "# api: 1", set()),
    ("underscores and dashes are part of a key", "run_task-x: 1", {"run_task-x"}),
]

#: (label, body, is it marked for removal). The imperative verbs the two real
#: marked blocks use, and the near-misses that must NOT be read as markers --
#: a block wrongly read as marked is one this gate stops holding to the rule.
SELF_TEST_REMOVAL: list[tuple[str, str, bool]] = [
    ("the marker the upgrade guide uses", "# remove this block entirely\napi:\n  config: {}", True),
    ("its line variant", "# remove this line entirely\napi: x", True),
    ("delete", "# delete this key\napi: x", True),
    ("drop", "# drop this section\napi: x", True),
    ("read past a leading blank line", "\n# remove this block\napi: x", True),
    ("an ordinary comment is not a marker", "# set this to enable X\napi: x", False),
    ("no comment at all", "api:\n  config: {}", False),
    ("the past participle is NOT read as a marker today", "# Removed in 2.0\napi: x", False),
    ("the noun is NOT read as a marker today", "api: x  # removal note", False),
    ("only the FIRST non-blank line is a marker", "api: x\n# remove the line above", False),
]

#: (label, helm output, returncode, outcome). Pinning the two message readers
#: and the note/failure split. Paired with LIVE_SELF_TEST below, which is what
#: establishes these transcriptions are what helm really says.
SELF_TEST_CLASSIFY: list[tuple[str, str, int, str]] = [
    (
        "an undeclared key -- the #1986 defect's own message",
        "[ERROR] values.yaml: - at '/api/config': additional properties 'engines' not allowed",
        1,
        "undeclared",
    ),
    (
        "the singular wording",
        "[ERROR] values.yaml: - at '/api': additional property 'nope' not allowed",
        1,
        "undeclared",
    ),
    (
        "a missing required key is a fragment, not a failure",
        "[ERROR] values.yaml: - at '/api/config/auth/sso/oidc/0': missing properties "
        "'issuer_url', 'client_id'",
        1,
        "fragment",
    ),
    ("a clean lint", "1 chart(s) linted, 0 chart(s) failed", 0, "clean"),
    (
        "an undeclared key wins over a missing one in the same output",
        "- at '/api': additional properties 'engines' not allowed\n"
        "- at '/api': missing properties 'x'",
        1,
        "undeclared",
    ),
    (
        "a failure that is neither: IGNORED today, and that is the hole this names",
        "Error: template: terrapod/templates/x.yaml:3:9: executing ... nil pointer",
        1,
        "unexplained",
    ),
]

#: (label, marked, outcome, verdict). The inverted half is the point: a marked
#: block must be REFUSED, so `clean` is the problem there and `undeclared` is
#: the correct answer. Dropping the inversion keeps every real block passing,
#: which is why only a fixture can catch it.
SELF_TEST_VERDICT: list[tuple[str, bool, str, str | None]] = [
    ("an ordinary snippet that applies", False, "clean", None),
    ("an ordinary snippet with a key that does not exist", False, "undeclared", "undeclared"),
    ("an ordinary fragment", False, "fragment", "fragment"),
    (
        "an ordinary snippet failing for some third reason: ignored today",
        False,
        "unexplained",
        None,
    ),
    ("a marked block the schema refuses -- the upgrade note is right", True, "undeclared", None),
    (
        "a marked block the schema ACCEPTS -- the key still exists",
        True,
        "clean",
        "removal-accepted",
    ),
    (
        "a marked block that only misses a required key: the key still exists",
        True,
        "fragment",
        "removal-accepted",
    ),
    (
        "a marked block failing for some third reason: nothing proves it is gone",
        True,
        "unexplained",
        "removal-accepted",
    ),
]

#: (label, snippet, outcome) -- run through the REAL `helm lint`. This is what
#: no literal fixture can do: it establishes that `classify` reads the messages
#: helm actually emits, so a narrowed regex or a reworded helm fails here
#: instead of turning every snippet into "clean".
LIVE_SELF_TEST: list[tuple[str, str, str]] = [
    ("a snippet an operator can paste whole", "api:\n  config:\n    log_level: debug\n", "clean"),
    (
        "the withdrawn engine gate (#1986), the defect this gate was written for",
        "api:\n  config:\n    engines:\n      pulumi:\n        enabled: true\n",
        "undeclared",
    ),
    (
        "a fragment illustrating one setting",
        "api:\n  config:\n    auth:\n      sso:\n        oidc:\n"
        '          - name: "your-idp"\n            existingSecret: "terrapod-oidc"\n',
        "fragment",
    ),
]


def schema_roots() -> set[str]:
    try:
        s = json.loads(SCHEMA.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"FAIL: cannot read {SCHEMA}: {exc}")
    roots = set((s.get("properties") or {}).keys())
    if not roots:
        sys.exit(
            "FAIL: values.schema.json declares no root properties, so this "
            "check cannot tell a Helm snippet from any other YAML. Fix the "
            "schema read rather than lowering the floor."
        )
    return roots


def blocks_in(text: str) -> list[tuple[int, str]]:
    """Every fenced yaml block, as (line number of its first line, body)."""
    out: list[tuple[int, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if FENCE.match(lines[i]):
            start = i + 1
            j = start
            while j < len(lines) and not FENCE_END.match(lines[j]):
                j += 1
            out.append((start + 1, "\n".join(lines[start:j])))
            i = j + 1
        else:
            i += 1
    return out


def root_keys(body: str) -> set[str]:
    """The block's top-level mapping keys."""
    return {m.group(1) for m in (ROOT_KEY.match(ln) for ln in body.split("\n")) if m}


def is_removal_marked(body: str) -> bool:
    """Is this a block an upgrade guide prints in order to say 'delete this'?"""
    first = next((ln for ln in body.split("\n") if ln.strip()), "")
    return bool(REMOVAL.search(first))


def classify(output: str, returncode: int) -> str:
    """What helm's answer meant: undeclared / fragment / clean / unexplained."""
    if UNDECLARED.search(output):
        return "undeclared"
    if returncode == 0:
        return "clean"
    if MISSING.search(output):
        return "fragment"
    return "unexplained"


def verdict(marked: bool, outcome: str) -> str | None:
    """What that outcome means for this block. None is 'nothing to say'."""
    if marked:
        # INVERTED: a block printed to be deleted must be refused by the schema.
        return None if outcome == "undeclared" else "removal-accepted"
    if outcome in ("undeclared", "fragment"):
        return outcome
    return None


def lint(body: str, workdir: pathlib.Path) -> tuple[str, int]:
    """Run the real `helm lint` over one snippet."""
    f = workdir / "snippet.yaml"
    f.write_text(body + "\n")
    p = subprocess.run(
        ["helm", "lint", str(CHART), "-f", str(f)],
        capture_output=True,
        text=True,
        check=False,
    )
    return p.stdout + p.stderr, p.returncode


def self_test() -> list[str]:
    """Pin every judgement between the fence and the verdict, before the scan."""
    out: list[str] = []
    for label, text, expected in SELF_TEST_BLOCKS:
        got = blocks_in(text)
        if got != expected:
            out.append(
                f"the fence matcher no longer reads {label!r} the way it must: expected "
                f"{expected}, read {got}. A block it cannot see is a snippet it does not "
                "check."
            )
    for label, body, expected_keys in SELF_TEST_ROOT_KEYS:
        got_keys = root_keys(body)
        if got_keys != expected_keys:
            out.append(
                f"the root-key matcher no longer reads {label!r} the way it must: expected "
                f"{sorted(expected_keys)}, read {sorted(got_keys)}. This is what decides a "
                "block is a Helm values snippet at all."
            )
    for label, body, expected_marked in SELF_TEST_REMOVAL:
        got_marked = is_removal_marked(body)
        if got_marked != expected_marked:
            out.append(
                f"the removal marker no longer reads {label!r} the way it must: expected "
                f"{expected_marked}, read {got_marked}. A block wrongly read as marked is "
                "one this gate stops holding to the ordinary rule."
            )
    for label, output, rc, expected_outcome in SELF_TEST_CLASSIFY:
        got_outcome = classify(output, rc)
        if got_outcome != expected_outcome:
            out.append(
                f"the helm-output reader no longer classifies {label!r} the way it must: "
                f"expected {expected_outcome!r}, read {got_outcome!r}. If undeclared keys "
                "start reading as clean, every snippet passes and nothing is checked."
            )
    for label, marked, outcome, expected_verdict in SELF_TEST_VERDICT:
        got_verdict = verdict(marked, outcome)
        if got_verdict != expected_verdict:
            out.append(
                f"the verdict no longer judges {label!r} the way it must: expected "
                f"{expected_verdict!r}, got {got_verdict!r}. The marked case is INVERTED "
                "on purpose -- a block printed to be deleted must be refused."
            )
    return out


def live_self_test() -> list[str]:
    """Drive the real `helm lint` over one literal snippet per outcome.

    The only thing that can establish `classify` reads what helm really says.
    Three invocations, ~0.1s each, against a gate that already runs 160.
    """
    out: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for label, body, expected in LIVE_SELF_TEST:
            output, rc = lint(body, pathlib.Path(td))
            got = classify(output, rc)
            if got != expected:
                detail = "\n".join(
                    ln.strip()
                    for ln in output.split("\n")
                    if UNDECLARED.search(ln) or MISSING.search(ln) or "ERROR" in ln
                )
                out.append(
                    f"the real `helm lint` no longer answers {label!r} as {expected!r} -- "
                    f"this gate read it as {got!r} (rc={rc}).\n        {detail}\n"
                    "      Either helm has reworded its output and the two message readers "
                    "need teaching, or the chart's schema has moved. Do NOT relax the "
                    "fixture: if an undeclared key reads as clean, every snippet in the "
                    "docs passes and this gate checks nothing."
                )
    return out


def main() -> int:
    if not CHART.is_dir():
        sys.exit(f"FAIL: no chart at {CHART}")

    blockers = self_test() + live_self_test()
    if blockers:
        print("check_doc_helm_values cannot do its job:\n")
        for blocker in blockers:
            print(f"  - {blocker}\n")
        return 1

    roots = schema_roots()

    docs = sorted(
        [*(ROOT / "docs").rglob("*.md"), ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "llms.txt"]
    )
    problems: list[str] = []
    notes: list[str] = []
    checked = 0

    with tempfile.TemporaryDirectory() as td:
        for doc in docs:
            if not doc.is_file():
                continue
            for lineno, body in blocks_in(doc.read_text()):
                keys = root_keys(body)
                if not keys & roots:
                    continue  # not a Helm values snippet
                checked += 1
                out, rc = lint(body, pathlib.Path(td))
                marked = is_removal_marked(body)
                said = verdict(marked, classify(out, rc))
                if said is None:
                    continue

                first = next((ln for ln in body.split("\n") if ln.strip()), "")
                rel = doc.relative_to(ROOT)
                detail = "\n".join(
                    ln.strip()
                    for ln in out.split("\n")
                    if UNDECLARED.search(ln) or MISSING.search(ln)
                )

                if said == "removal-accepted":
                    problems.append(
                        f"{rel}:{lineno} — this block is marked for removal and "
                        f"the schema ACCEPTS it, so the key still exists and the "
                        f"upgrade note is wrong:\n        {first.strip()}"
                    )
                elif said == "undeclared":
                    problems.append(
                        f"{rel}:{lineno} — an operator cannot apply this snippet:\n"
                        + "\n".join(f"        {d}" for d in detail.split("\n") if d)
                        + f"\n        keys: {sorted(keys & roots)}"
                    )
                else:
                    notes.append(
                        f"{rel}:{lineno} — a fragment: lints only as part of a "
                        f"fuller block ({detail.splitlines()[0] if detail else 'missing required keys'})"
                    )

    if checked < MIN_BLOCKS:
        problems.append(
            f"only {checked} Helm-values snippet(s) were found, below the floor "
            f"of {MIN_BLOCKS}. The fence or root-key matcher has stopped "
            f"matching; fix it rather than lowering the floor."
        )

    if problems:
        print(f"check_doc_helm_values: {len(problems)} problem(s) over {checked} snippet(s)\n")
        for pr in problems:
            print(f"  {pr}\n")
        print(
            "  A values snippet in the docs is something an operator PASTES. "
            "A key the\n  schema refuses does not no-op -- it fails `helm lint` "
            "and `helm upgrade`, so\n  the page breaks the install it is trying "
            "to describe."
        )
        return 1

    for n in notes:
        print(f"  note: {n}")
    fixtures = (
        len(SELF_TEST_BLOCKS)
        + len(SELF_TEST_ROOT_KEYS)
        + len(SELF_TEST_REMOVAL)
        + len(SELF_TEST_CLASSIFY)
        + len(SELF_TEST_VERDICT)
        + len(LIVE_SELF_TEST)
    )
    print(
        f"check_doc_helm_values: OK -- {checked} Helm-values snippet(s) across "
        f"the docs; every one an operator would paste whole applies, and "
        f"{len(notes)} {'is a fragment' if len(notes) == 1 else 'are fragments'}; "
        f"{fixtures} fixtures hold"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
