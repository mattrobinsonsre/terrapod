#!/usr/bin/env python3
"""Wherever a machine credential's `key` is described, the MECHANISM is too.

#1933's machine credential is referred to from the inventory by name, with a
`${terrapod:<name>.<field>}` placeholder the runner substitutes. An earlier
design scoped it instead -- `key` was `all` or a group name, and the credential
was injected as inventory variables at that level so ansible's own group
precedence placed it. The code moved to placeholders; **seven prose surfaces did
not**, and went on telling operators and agents to scope it. This gate fails on
six of them -- `docs/index.md` (twice), `README.md`, `llms.txt`,
`docs/api-reference.md` and the runner phase's own docstring.

Two more were fixed by hand and are named here because this gate CANNOT see
them, which is worth knowing before trusting it:

* `services/.../variable_service.py` opened "`key` is the SCOPE in both" and
  three lines later said "`key` is the credential's NAME" -- one comment block,
  two answers. A block that states the mechanism passes by construction, so
  self-contradiction WITHIN a site is invisible here.
* `docs/runbooks.md` said "look instead at the credential's scope" in a
  troubleshooting row that never claims what `key` is, so it is not a site at
  all. Widening the subject to every mention would flag that row and others
  legitimately -- a gate whose failures are mostly correct prose teaches people
  to silence it.

The cost is not a confusing sentence. `all` satisfies `CREDENTIAL_NAME`, so
`key: all` is ACCEPTED at the write boundary, and the withdrawn account's whole
premise is that placement is automatic -- so a reader following it writes no
placeholder, nothing refers to the credential, and an ABSENT reference is not an
unresolved one, so the run is not refused. It reports hosts it could not reach,
naming neither the credential nor the reason. `llms.txt` carried BOTH accounts at
once, which is the worst case: an agent cannot tell which of two confident
statements is current.

**The subject is DERIVED, not enumerated.** `check_ee_contract.py`'s own comment
records what a hand-kept surface list costs -- `llms.txt` sat outside its PROSE
set and so kept asserting a withdrawn contract -- so this walks every tracked
`*.md`, `llms.txt` and `*.py` and finds the sites by what they say. A new surface
is in scope the day it is written.

**One rule, which is what lets a correction quote the error it corrects.** A
paragraph is a SITE if it mentions the machine credential AND either makes a
`key` claim or asserts scope placement; every site must state the mechanism. So
the runner phase's docstring, which quotes the withdrawn wording verbatim in
order to warn about it, passes -- because it states the placeholder in the same
breath. A gate that merely banned the phrasing would have forced that warning to
be deleted or exempted, and an exemption ledger is the thing that drifts.

What it cannot catch, and the reason is worth knowing: every surface agreeing on
the same WRONG account. Internal disagreement is the drift mechanism and the only
half checkable without a human reading the code.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The module that DECIDES the mechanism. Read so the gate's own notion of the
#: placeholder cannot drift from the implementation's.
MECHANISM_SOURCE = pathlib.Path("services/terrapod/services/ansible_auth.py")

#: Every language that states this to a human or an agent. `.go` is here
#: because it is where the blind spot was found: the MCP tool's FIELD
#: description was correct and gated (the catalogue freezes `input_schema`),
#: while its TOOL-level description -- which an agent reads first, and which
#: nothing freezes -- still said `key` was the credential's scope. `.tsx` is
#: here for the same reason in the other direction: a UI string is prose too.
SUFFIXES = (".md", ".py", ".go", ".ts", ".tsx")

SUBJECT = re.compile(r"ansible_machine_auth|machine credential", re.IGNORECASE)

#: A claim about what `key` is.
KEY_CLAIM = re.compile(r"`key`")

#: Assertions that place the credential by scope rather than by reference. These
#: are the phrasings the seven surfaces actually used; a site matching one is in
#: scope even when it never says the word `key`, which is how
#: `docs/index.md`'s feature row and `README.md`'s both read.
#: Each is a WHOLE assertion that the credential is placed by scope, never a
#: fragment -- which is what lets a correction quote the error it corrects
#: without an exemption. `key` genuinely IS a scope for the two git categories,
#: so nothing here matches a bare "scope": each alternative ties the claim to
#: `all`, to a group, or to the word "name" it was wrongly contrasted with.
#:
#: "at group level" was here and was REMOVED: it matched
#: `configure_inventory` and its test describing why group VARIABLES are not
#: re-emitted at group level in the rendered document, which is inventory
#: structure and has nothing to do with a credential. It cost nothing to drop,
#: because every surface it was added for also says "becomes inventory
#: variables" or "injected as inventory variables".
SCOPE_CLAIM = re.compile(
    r"scoped to `all`"
    r"|`key` = `all`"
    r"|`key` is the SCOPE in both"
    r"|credential's scope rather than as a name"
    r"|group precedence does the scoping"
    r"|(?:becomes|injected as) inventory variables",
    re.IGNORECASE,
)

#: Fewer than this and the matcher has stopped matching, which must fail rather
#: than pass on nothing. Eleven sites exist as this is written.
MIN_SITES = 8


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    return [f for f in out if f.endswith(SUFFIXES) or f == "llms.txt"]


def _mechanism_pattern() -> re.Pattern[str]:
    """Derive the placeholder's literal prefix from the module that defines it.

    `PLACEHOLDER` is a compiled regex, so its source is the authority on what a
    reference looks like; taking the prefix from there means a change to the
    syntax moves this gate with it instead of leaving it asserting the old one.
    """
    text = (ROOT / MECHANISM_SOURCE).read_text()
    if r"\$\{terrapod:" not in text:
        print(
            f"FAIL — {MECHANISM_SOURCE} no longer defines a ${{terrapod:...}} placeholder, "
            f"so this gate is asserting a mechanism that has been replaced; rewrite it "
            f"against whatever replaced it rather than deleting it"
        )
        raise SystemExit(1)
    # The mechanism may be stated as the placeholder itself, or in words.
    return re.compile(
        r"\$\{terrapod:|credential's \*{0,2}name|credential's NAME", re.IGNORECASE
    )


def main() -> int:
    mechanism = _mechanism_pattern()
    problems: list[str] = []
    sites = 0

    for rel in _tracked():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if not SUBJECT.search(text):
            continue
        for para in re.split(r"\n\s*\n", text):
            if not SUBJECT.search(para):
                continue
            if not (KEY_CLAIM.search(para) or SCOPE_CLAIM.search(para)):
                continue
            sites += 1
            if mechanism.search(para):
                continue
            line = next(
                (ln for ln in para.splitlines() if SUBJECT.search(ln)), para[:120]
            )
            problems.append(
                f"{rel}: describes a machine credential's `key` without the mechanism "
                f"(the ${{terrapod:<name>.<field>}} placeholder, or that `key` is the "
                f"credential's name)\n      {line.strip()[:160]}"
            )

    # Clause two, independent of the paragraph walk above and the reason this
    # gate catches the MCP tool description at all. That sentence sits in one
    # contiguous block with the FIELD description directly above it, which
    # states the mechanism correctly -- so the paragraph passes while the
    # sentence inside it is wrong. Granularity cannot fix that without either
    # missing sites or flagging correct prose, so the withdrawn ASSERTIONS are
    # banned outright instead.
    #
    # There is no exemption list, and none is needed: the runner phase's
    # docstring quotes the withdrawn wording in order to warn about it and
    # survives, because it quotes a FRAGMENT ('the scope -- `all`, or a group')
    # rather than any of the whole assertions below. An exemption ledger is the
    # thing that drifts, so the absence of one is deliberate.
    for rel in _tracked():
        path = ROOT / rel
        if not path.is_file() or path.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for match in SCOPE_CLAIM.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(
                f"{rel}:{line}: asserts a machine credential is placed by SCOPE "
                f"({match.group(0)!r}), which was the design before placeholders"
            )

    if sites < MIN_SITES:
        print(
            f"FAIL — only {sites} site(s) examined, expected at least {MIN_SITES}; the "
            f"matcher has stopped finding the prose it exists to check"
        )
        return 1

    if problems:
        print(
            f"FAIL — {len(problems)} problem(s) across {len({p.split(':')[0] for p in problems})} "
            f"file(s); {sites} sites describing a machine credential's `key` were examined:"
        )
        for p in problems:
            print(f"  - {p}")
        print(
            "\n  A machine credential's `key` is its NAME. Where it applies is said in "
            "the\n  INVENTORY, by a ${terrapod:<name>.<field>} placeholder the runner "
            "substitutes\n  while rendering the run's own copy. It is not scoped to "
            "`all` or to a group,\n  and nothing is injected at group level — that was "
            "the design before\n  placeholders. See docs/ansible-credentials.md."
        )
        return 1

    print(
        f"OK — {sites} sites describe a machine credential's `key`, all with the mechanism"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
