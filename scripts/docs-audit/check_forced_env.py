#!/usr/bin/env python3
"""Where prose enumerates the environment a configure forces, it names the real set.

`services/terrapod/runner/phases/configure.py` DECIDES it: `_FORCED_ENV` plus
whatever `_forced_env()` assigns on top. Environment beats `ansible.cfg`, so that
set is the one place Terrapod overrides an operator's own file, and an operator
reading about it is deciding what they still control.

**It had gone false in both directions at once, which is why this exists.**
`docs/runners.md` listed `ANSIBLE_HOST_KEY_CHECKING` as forced, with the
connection-time reason "an unattended run has no one to answer a host-key
prompt" -- and the configure phase has never set it. The only place it is set is
`services/terrapod/services/inventory_resolve.py`, for the API's own
`ansible-inventory` run, whose own comment gives the real reason: "Nothing here
should ever reach a remote host: this is a parse." So the table gave a
parse-time setting a connection-time justification and implied a control that
does not exist, in the one area where an operator most needs the truth. The same
table OMITTED `ANSIBLE_FORCE_COLOR`, which is forced. `llms.txt` repeated both
halves. The tell was in the table itself: that row carried no value where every
other row did.

**Set equality, not presence.** A gate asking "is each forced variable
mentioned" would have passed on that table, because the four names it carried
included three real ones. Only equality sees a name that should not be there,
and only equality sees one missing.

**Derived by AST, never by a regex over prose.** A regex over the doc would be a
second copy of the claim, which is the thing being checked. `_FORCED_ENV`'s dict
literal and `_forced_env`'s own subscript assignments are read out of the
module's source, so adding a forced variable moves this gate with it. It is read
as TEXT rather than imported: importing `terrapod.runner.phases.configure` drags
in `RunnerConfig` and so `settings`, and so SQLAlchemy and the rest of the API's
dependency closure, which the job running this installs none of -- the recorded
way a docs-audit gate passes locally and fails in CI with `ModuleNotFoundError`.

**The passthroughs are a separate set, deliberately.** `TMPDIR`,
`ANSIBLE_LOCAL_TEMP` and `ANSIBLE_REMOTE_TEMP` are forwarded from the Job's own
environment when it supplies them -- the value is the Job's, not a Terrapod
decision -- so they do not belong in the forced table and naming one there would
be a different claim. They are extracted anyway, so a prose site may mention one
without being flagged.

**A mention outside both sets is allowed only where the prose negates it, and
the negator must FOLLOW the name closely.** That is what lets `llms.txt` say
Terrapod "sets `ANSIBLE_CONFIG` nowhere" -- a statement that something does NOT
reach the process is the opposite of a false claim that it is forced.

The window is narrow and one-sided because a generous one is useless here, and
measurably so. A first version allowed a negator within 90 characters on either
side and found `docs/runners.md`'s wrong row while MISSING the identical claim in
`llms.txt`: that file holds its whole entry on one line, so "EVERY OTHER
INHERITED `ANSIBLE_*` IS STRIPPED" sits about sixty characters past
`ANSIBLE_HOST_KEY_CHECKING` and read as negating it. Sentence scope is no better
on that file -- the enumeration and the `ANSIBLE_CONFIG` disclaimer are one
sentence joined by semicolons. So the rule is a convention a writer can satisfy
deliberately rather than a guess at intent: say it is not set immediately after
naming it.

**It does not scan itself, and does not need a clause for that.** Its subject is
tracked `*.md` and `llms.txt`; this is a `.py` file, so the docstring above can
quote the withdrawn row verbatim and remain out of scope. That is worth stating
rather than adding a self-exclusion that could never fire -- a dead clause reads
as protection and is none.

What it cannot catch: a surface that states the set correctly and gives the
wrong REASON for an entry. The reason is the half that survives review, because
a wrong name reads as a typo while a wrong justification reads as considered.
Only a human reading the module's comments beside the doc's can see that.

Run from the repository root.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The module that DECIDES the set.
PHASE = pathlib.Path("services/terrapod/runner/phases/configure.py")

#: The dict literal holding the unconditional half, and the function that adds
#: to it. Named rather than discovered, because these two are the construction
#: this gate knows how to read; anything else raises.
FORCED_DICT = "_FORCED_ENV"
FORCED_FUNC = "_forced_env"

#: The phrase that marks a prose enumeration of the set. One phrase, shared by
#: both surfaces, so a new surface is in scope the day it is written.
LOCATOR = re.compile(r"forced through the", re.IGNORECASE)

#: An environment variable name as prose writes it. A letter is REQUIRED after
#: the underscore, so the glob prose uses for "every inherited one" --
#: `ANSIBLE_*` -- is not read as a variable called `ANSIBLE_`.
ENV_NAME = re.compile(r"\b(?:ANSIBLE_[A-Z][A-Z_]*|TMPDIR)\b")

#: Saying a variable is NOT set is the opposite of claiming it is forced, so a
#: mention followed closely by one of these is not a finding. Short, literal and
#: attached to the name: inferring negation from prose is how a gate acquires
#: findings that are mostly correct writing, which teaches people to silence it.
NEGATION = re.compile(r"nowhere|(?:is|are) not set|never set|(?:is|are) stripped", re.IGNORECASE)

#: How far PAST a mention a negation counts. One-sided and tight, for the reason
#: in the module docstring: a generous two-sided window read `llms.txt`'s "every
#: other inherited `ANSIBLE_*` is stripped" as negating the name sixty
#: characters before it, and so missed the very claim this gate exists for.
NEGATION_WINDOW = 40

#: Spelled counts, so "Four settings are forced" is checked against the real
#: number instead of being a number in prose with nothing enforcing it -- which
#: is exactly what it was.
COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
COUNT_CLAIM = re.compile(
    r"\b(" + "|".join(COUNT_WORDS) + r")\b[^.]{0,40}?\bsettings? (?:are|is) forced",
    re.IGNORECASE,
)

#: Floors. A matcher that stops matching must fail rather than pass on nothing,
#: and one floor per thing matched: a single combined floor would let the AST
#: extraction go to zero while the prose sites carried the count.
MIN_FORCED = 4
MIN_SITES = 2

#: The one surface whose enumeration is a markdown TABLE rather than a sentence.
#: Held to set equality, which is stricter than the prose rule below, and it is
#: where the defect lived.
TABLE_DOC = "docs/runners.md"


def _tracked_prose() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    return [f for f in out if f.endswith(".md") or f == "llms.txt"]


def _string_keys(node: ast.AST, where: str) -> list[str]:
    """A dict literal's keys, refusing anything that is not a plain string."""
    if not isinstance(node, ast.Dict):
        raise ValueError(f"{where} is not a dict literal, so it cannot be read")
    keys = []
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ValueError(
                f"{where} has a key that is not a string literal; if it is now built "
                "dynamically this gate needs rewriting rather than relaxing"
            )
        keys.append(key.value)
    return keys


def _tuple_strings(node: ast.AST, where: str) -> list[str]:
    if not isinstance(node, ast.Tuple):
        raise ValueError(f"{where} is not a tuple literal, so it cannot be read")
    values = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise ValueError(f"{where} holds a non-string element, so it cannot be read")
        values.append(element.value)
    return values


def from_phase() -> tuple[set[str], set[str]]:
    """The forced set and the passthrough set, by AST.

    Forced is `_FORCED_ENV`'s keys plus every `env["NAME"] = ...` inside
    `_forced_env`. Passthrough is the names the function forwards from its own
    environment -- recognised by the subscript key being the target of the
    enclosing `for`, whose iterable supplies the names.

    Anything else -- a key that is a variable this cannot resolve, a dict built
    by comprehension -- RAISES. A construction the gate cannot read is one it
    cannot vouch for, and skipping it would turn a missing name into a pass.
    """
    tree = ast.parse((ROOT / PHASE).read_text())

    forced: set[str] = set()
    passthrough: set[str] = set()
    func: ast.FunctionDef | None = None

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == FORCED_DICT:
                forced.update(_string_keys(node.value, f"{PHASE}::{FORCED_DICT}"))
        elif isinstance(node, ast.FunctionDef) and node.name == FORCED_FUNC:
            func = node

    if not forced:
        raise ValueError(f"no {FORCED_DICT} dict literal found in {PHASE}")
    if func is None:
        raise ValueError(f"no `def {FORCED_FUNC}` found in {PHASE}")

    # Which `for` target each subscript assignment sits under, so a key that is
    # a loop variable resolves to the loop's own names rather than raising.
    loop_names: dict[str, list[str]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            loop_names[node.target.id] = _tuple_strings(
                node.iter, f"{PHASE}::{FORCED_FUNC}'s `for {node.target.id} in ...`"
            )

    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        if not isinstance(target.value, ast.Name) or target.value.id != "env":
            continue
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            forced.add(key.value)
        elif isinstance(key, ast.Name) and key.id in loop_names:
            passthrough.update(loop_names[key.id])
        else:
            raise ValueError(
                f"{PHASE}::{FORCED_FUNC} assigns `env[...]` with a key this gate cannot "
                "resolve to a name; teach it the new shape rather than relaxing it"
            )

    return forced, passthrough


def _sites(path: str, text: str) -> list[str]:
    """Every span in one file that enumerates the forced set.

    For a markdown page a site runs from the locator line to the next heading,
    so the table and the prose explaining it are ONE site -- the set is stated
    across both, and splitting them would make a name stated only in the prose
    read as missing. `llms.txt` holds each entry on one line, so a site there is
    that line.
    """
    lines = text.splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        if not LOCATOR.search(line):
            continue
        if path == "llms.txt":
            found.append(line)
            continue
        end = len(lines)
        for ahead in range(index + 1, len(lines)):
            if lines[ahead].startswith("#"):
                end = ahead
                break
        found.append("\n".join(lines[index:end]))
    return found


def _table_names(site: str) -> list[str] | None:
    """The first column of the FIRST markdown table in a site.

    The first, because a site opens on the locator line, so the table that
    follows it is the enumeration; a later one (a troubleshooting table, say) is
    a different claim and merging the two would read its rows as forced
    variables. Returns None when there is no table at all, which is a finding
    rather than a pass: the shape has changed and the equality clause -- the one
    the original defect would have failed -- would otherwise be skipped.
    """
    names: list[str] = []
    seen_header = False
    for line in site.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if seen_header:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not seen_header:
            seen_header = True
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        first = cells[0].strip("`").split("=")[0].strip()
        if first:
            names.append(first)
    return names or None


def _unnegated(site: str, name: str) -> bool:
    """True when at least one mention of `name` is not disclaimed right after it.

    Every mention has to be disclaimed, not just one: a surface that says a
    variable is not set and then lists it among the forced ones is making the
    false claim regardless of the order it makes them in.
    """
    return any(
        not NEGATION.search(site[match.end() : match.end() + NEGATION_WINDOW])
        for match in re.finditer(rf"\b{re.escape(name)}\b", site)
    )


def main() -> int:
    if not (ROOT / PHASE).is_file():
        print(f"FAIL — {PHASE} is missing", file=sys.stderr)
        return 1
    try:
        forced, passthrough = from_phase()
    except ValueError as exc:
        print(f"FAIL — cannot read the forced set from {PHASE}: {exc}", file=sys.stderr)
        return 1

    if len(forced) < MIN_FORCED:
        print(
            f"FAIL — only {len(forced)} forced variable(s) extracted from {PHASE} "
            f"({sorted(forced)}); expected at least {MIN_FORCED}, so the AST extraction "
            "has stopped matching rather than the set having shrunk.",
            file=sys.stderr,
        )
        return 1

    allowed = forced | passthrough
    problems: list[str] = []
    sites = 0
    table_checked = False

    for path in _tracked_prose():
        for site in _sites(path, (ROOT / path).read_text()):
            sites += 1
            mentioned = set(ENV_NAME.findall(site))

            if missing := sorted(forced - mentioned):
                problems.append(
                    f"{path}: enumerates the forced environment and does not name "
                    f"{', '.join(missing)}, which the configure phase forces"
                )

            for name in sorted(mentioned - allowed):
                if _unnegated(site, name):
                    problems.append(
                        f"{path}: names {name} where the forced environment is "
                        "enumerated, and the configure phase does not set it. If the "
                        "point is that it is NOT set, say so in the same breath; "
                        "otherwise the doc implies a control that does not exist"
                    )

            claim = COUNT_CLAIM.search(site)
            if claim and COUNT_WORDS[claim.group(1).lower()] != len(forced):
                problems.append(
                    f"{path}: says {claim.group(1).lower()!r} settings are forced; the "
                    f"configure phase forces {len(forced)}"
                )

            if path != TABLE_DOC:
                continue
            names = _table_names(site)
            if names is None:
                problems.append(
                    f"{path}: the forced-environment enumeration carries no table, so "
                    "this gate cannot hold it to set equality -- teach it the new shape"
                )
                continue
            table_checked = True
            if extra := sorted(set(names) - forced):
                problems.append(
                    f"{path}: its table lists {', '.join(extra)} as forced, and the "
                    "configure phase does not set them"
                )
            if absent := sorted(forced - set(names)):
                problems.append(
                    f"{path}: its table omits {', '.join(absent)}, which the configure phase forces"
                )

    if sites < MIN_SITES:
        problems.append(
            f"only {sites} prose site(s) matched {LOCATOR.pattern!r}; expected at least "
            f"{MIN_SITES}, so this gate has stopped finding the enumerations rather than "
            "them having been removed"
        )
    elif not table_checked:
        problems.append(
            f"{TABLE_DOC}'s forced-environment table was never checked for set equality, "
            "which is the clause the defect this gate exists for would have failed"
        )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the forced-environment prose:")
        for problem in problems:
            print(f"  - {problem}")
        print(
            f"\nForced by {PHASE}: {', '.join(sorted(forced))}"
            f"\nForwarded from the Job's environment: {', '.join(sorted(passthrough))}"
        )
        return 1

    print(
        f"PASS — {sites} site(s) enumerate the {len(forced)} variable(s) the configure "
        f"phase forces ({', '.join(sorted(forced))}), and {TABLE_DOC}'s table names "
        "exactly that set."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
