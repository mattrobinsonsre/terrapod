#!/usr/bin/env python3
"""The run task payload stays a superset of HashiCorp's spec (#2366).

`docs/run-tasks.md` states that Terrapod supports the [run task integration
spec](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run-tasks/run-tasks-integration)
on a best-effort basis, so a third party can build against the spec rather than
against us. That promise needs a gate, because an outbound payload is something
Terrapod **sends** rather than serves: there is no route for it, no response
schema and no consumer in this repo, so not one of the API contract snapshots
can see it.

That is not hypothetical. #2367 found six payload fields hardcoded to `""` or
`false` while the values sat on rows the dispatcher had already loaded, and they
had survived because `build_task_callback_payload` appeared in no test at all.
The sibling gate (`check_run_task_payload_doc.py`) now keeps our own
documentation equal to what we build. This one keeps what we build equal to what
the SPEC asks for, which is a different question and the one a third party cares
about.

Two directions:

1. **Every spec field is emitted**, unless it is in `DELIBERATELY_ABSENT` — and
   each entry there must also appear in the divergence table in
   `docs/run-tasks.md`, so a gap cannot be quietly widened by editing this file
   alone. A gap is a decision we wrote down, never an omission.
2. **Nothing claims to be absent while being emitted.** The mirror direction,
   and the one that rots silently: once a field lands, a leftover entry here and
   a row in that table tell a reader we do not send something we do.

SELF_TEST IS WHAT MAKES THE LEDGER CHECKABLE (#2425 §2.7). `DELIBERATELY_ABSENT`
is EMPTY on this tree, and both directions above are reached only through it -- so
every line of the ledger machinery, the table-anchoring included, is unexercised by
any real run. The floors cannot speak for it either: they prove the spec set and
the emitted set are non-empty and say nothing about the comparison between them.
That is the shape this repository keeps getting bitten by, a gate that cannot fail,
so the comparison and the table slicer are driven by literal fixtures before the
real files are opened -- including the mirror direction (a field claimed absent
while being sent), which has never once occurred and would otherwise be dead code
telling an integrator we withhold something we send.

STDLIB ONLY, and it reads the dispatcher as TEXT via `ast` rather than importing
it. A `scripts/docs-audit/` gate runs in a job with no Terrapod dependencies
installed, and importing `run_task_dispatcher` pulls in `db.models` and so
SQLAlchemy — which is how an earlier gate of mine passed locally and died in CI.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DISPATCHER = ROOT / "services" / "terrapod" / "services" / "run_task_dispatcher.py"
DOC = ROOT / "docs" / "run-tasks.md"
BUILDER = "build_task_callback_payload"

#: The request payload's fields, transcribed from HashiCorp's run task
#: integration spec page on 2026-11-05. Transcribed from the published page,
#: never trimmed to make our payload pass -- removing a line here would silence
#: the gate rather than satisfy it.
#:
#: `plan_json_api_url` is in the spec as a CONDITIONAL field, present only for
#: `post_plan`, `pre_apply` and `post_apply`. The builder emits the KEY at every
#: stage and sets it to `None` for `pre_plan`, which is what makes it checkable
#: here: this gate reads the key set, and a key that is always present with an
#: honest value is the shape a strict client wants.
SPEC_PAYLOAD_FIELDS = frozenset(
    {
        "access_token",
        "capabilities",
        "configuration_version_download_url",
        "configuration_version_id",
        "is_speculative",
        "organization_name",
        "payload_version",
        "plan_json_api_url",
        "run_app_url",
        "run_created_at",
        "run_created_by",
        "run_id",
        "run_message",
        "stage",
        "task_result_callback_url",
        "task_result_enforcement_level",
        "task_result_id",
        "vcs_branch",
        "vcs_commit_hash",
        "vcs_commit_url",
        "vcs_pull_request_url",
        "vcs_repo_url",
        "workspace_app_url",
        "workspace_id",
        "workspace_name",
        "workspace_working_directory",
    }
)

#: Spec fields Terrapod deliberately does not send, each with the phrase that
#: must appear in the divergence table in `docs/run-tasks.md`. The reason lives
#: in the document, where an integrator reads it; this holds the two together.
#: Currently EMPTY, and the mechanism is kept rather than deleted with it.
#: Both former entries went when #2366's follow-up emitted the two URLs
#: pointing at the ordinary authenticated routes -- a URL is not an
#: authorization, so sending one discloses nothing and neither needed the
#: per-run-task opt-in that the earlier capability-URL design would have.
#: An empty ledger makes the first check above absolute: every spec field must
#: be emitted. Keep the dict so the next genuine gap has somewhere to be
#: declared WITH its reason, instead of being dropped silently.
DELIBERATELY_ABSENT: dict[str, str] = {}

#: Floors, so a matcher that stops matching FAILS rather than passing on nothing.
MIN_SPEC_FIELDS = 20
MIN_EMITTED = 20
MIN_TABLE_CHARS = 400


#: The heading the divergence table lives under, and the one after it. The
#: gate's promise is that a gap appears in THAT table -- not merely somewhere in
#: a 400-line document, which a passing mention elsewhere would satisfy.
DIVERGENCE_HEADING = "### Where Terrapod diverges, and why"


def divergence_table(doc: str) -> str:
    """The divergence table's own text, sliced from its heading to the next one.

    Checking the whole document would be the looser thing this gate's docstring
    does not claim: `plan_json_api_url` is named in the payload notes as well,
    so a whole-document search passes even with the table row deleted. Slicing
    makes the check the one that was promised.
    """
    start = doc.find(DIVERGENCE_HEADING)
    if start < 0:
        raise SystemExit(
            f"{DOC.name} no longer has a {DIVERGENCE_HEADING!r} heading. That table is "
            "where a deliberate gap is explained to an integrator; restore it, or teach "
            "this gate the new heading."
        )
    rest = doc[start + len(DIVERGENCE_HEADING) :]
    end = rest.find("\n## ")
    nxt = rest.find("\n### ")
    if 0 <= nxt < (end if end >= 0 else len(rest)):
        end = nxt
    return rest[:end] if end >= 0 else rest


#: Literal builders the AST reader must read, and ones it must REFUSE. A
#: payload whose field names are decided at run time is one this gate cannot
#: compare against the spec, so refusing names the symbol and asks for the
#: reader to be taught -- `None` is that refusal.
SELF_TEST_EMITTED: list[tuple[str, str, set[str] | None]] = [
    (
        "a literal dict",
        "def build_task_callback_payload(task, run):\n"
        '    return {"stage": stage, "run_id": run.id}\n',
        {"stage", "run_id"},
    ),
    (
        "a nested object contributes its own key and not its children",
        "def build_task_callback_payload():\n"
        '    return {"capabilities": {"outcomes": False}, "stage": s}\n',
        {"capabilities", "stage"},
    ),
    ("no builder of that name", 'def other():\n    return {"a": 1}\n', None),
    (
        "two builders -- which one ships is not a matcher's guess to make",
        'def build_task_callback_payload():\n    return {"a": 1}\n'
        'def build_task_callback_payload():\n    return {"b": 1}\n',
        None,
    ),
    (
        "two returns: the field set depends on a branch",
        "def build_task_callback_payload(x):\n"
        '    if x:\n        return {"a": 1}\n    return {"b": 1}\n',
        None,
    ),
    ("returns a name, not a literal", "def build_task_callback_payload():\n    return out\n", None),
    (
        "a splatted helper hides the fields it contributes, so coverage is unknowable",
        'def build_task_callback_payload():\n    return {**base(), "stage": s}\n',
        None,
    ),
    (
        "async def: an AsyncFunctionDef is not a FunctionDef, so it fails closed",
        'async def build_task_callback_payload():\n    return {"a": 1}\n',
        None,
    ),
]

_TABLE = (
    "\n\n| Spec | Terrapod | Why |\n|---|---|---|\n"
    "| `post_apply` stage | not implemented | the run has finished by then |\n"
)

#: (label, doc, a substring the slice must contain, one it must NOT) -- and
#: `None, None` for "the slicer must refuse". The slice is what makes this gate
#: mean what it claims: a whole-document search passes with the table row
#: deleted, because the field is named in the payload notes as well.
SELF_TEST_TABLE: list[tuple[str, str, str | None, str | None]] = [
    (
        "stops at the next level-2 heading",
        f"intro\n\n{DIVERGENCE_HEADING}{_TABLE}\n## Callback Tokens\n\nmore\n",
        "post_apply",
        "Callback Tokens",
    ),
    (
        "stops at the next level-3 sibling",
        f"{DIVERGENCE_HEADING}{_TABLE}\n### Something Else\n\nmore\n",
        "post_apply",
        "Something Else",
    ),
    ("runs to the end of the document", f"{DIVERGENCE_HEADING}{_TABLE}", "post_apply", None),
    (
        "a deeper sub-heading is part of the section, not the end of it",
        f"{DIVERGENCE_HEADING}{_TABLE}\n#### A detail\n\nkept\n",
        "kept",
        None,
    ),
    (
        "a level-3 before a level-2 ends it first",
        f"{DIVERGENCE_HEADING}{_TABLE}\n### Next\n\nx\n\n## Later\n\ny",
        "post_apply",
        "Next",
    ),
    (
        "a level-2 before a level-3 ends it first",
        f"{DIVERGENCE_HEADING}{_TABLE}\n## Later\n\ny\n\n### Deeper\n\nz",
        "post_apply",
        "Later",
    ),
    (
        "the heading renamed away: refuse rather than slice nothing",
        "## Something\n\nprose\n",
        None,
        None,
    ),
]

#: (label, spec, emitted, ledger, table, doc, expected problems). The last item
#: describes the problems ONE PER ENTRY, each as the substrings that problem must
#: contain -- so the fixture pins how many problems are reported as well as what
#: they say. A flat list of substrings cannot: two substrings may belong to one
#: problem, and "one problem naming both" and "two problems" are different
#: answers. This drives BOTH directions of the ledger, neither of which any real
#: run reaches while `DELIBERATELY_ABSENT` is empty.
SELF_TEST_PROBLEMS: list[
    tuple[str, set[str], set[str], dict[str, str], str, str, list[list[str]]]
] = [
    ("everything the spec asks for is sent", {"a", "b"}, {"a", "b", "extra"}, {}, "", "", []),
    (
        "a spec field is not sent and is not ledgered",
        {"a", "b"},
        {"a"},
        {},
        "",
        "",
        [["SPEC FIELD NOT SENT: b"]],
    ),
    (
        "a ledgered gap whose reason is in the table",
        {"a", "b"},
        {"a"},
        {"b": "not implemented"},
        _TABLE,
        _TABLE,
        [],
    ),
    (
        "a ledgered gap whose reason is elsewhere in the doc but NOT in the table",
        {"a", "b"},
        {"a"},
        {"b": "not implemented"},
        "| unrelated row |",
        "prose saying not implemented\n\n| unrelated row |",
        [["ABSENT AND UNDOCUMENTED: b", "elsewhere in"]],
    ),
    (
        "a ledgered gap with no reason anywhere",
        {"a", "b"},
        {"a"},
        {"b": "not implemented"},
        "| unrelated row |",
        "| unrelated row |",
        [["ABSENT AND UNDOCUMENTED: b", "nowhere in"]],
    ),
    (
        "the mirror direction: ledgered absent while being sent",
        {"a", "b"},
        {"a", "b"},
        {"b": "not implemented"},
        _TABLE,
        _TABLE,
        [["CLAIMED ABSENT BUT IS SENT: b"]],
    ),
    (
        "a field outside the spec is nobody's business here",
        {"a"},
        {"a", "terrapod_only"},
        {},
        "",
        "",
        [],
    ),
]


def emitted_fields_in(src: str) -> set[str]:
    """The payload's keys, read out of the builder's single return dict."""
    tree = ast.parse(src)
    builders = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == BUILDER]
    if len(builders) != 1:
        raise SystemExit(f"expected exactly one {BUILDER}, found {len(builders)}")
    returns = [n for n in ast.walk(builders[0]) if isinstance(n, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Dict):
        raise SystemExit(f"{BUILDER} no longer returns a single dict literal; teach this reader")
    out: set[str] = set()
    for key in returns[0].value.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise SystemExit("a payload key is not a string literal; teach this reader")
        out.add(key.value)
    return out


def emitted_fields() -> set[str]:
    return emitted_fields_in(DISPATCHER.read_text())


def spec_problems(
    spec: set[str], emitted: set[str], absent: dict[str, str], table: str, doc: str
) -> list[str]:
    """Both directions of the spec/ledger comparison.

    Separated from `main` so SELF_TEST_PROBLEMS can drive it: with an empty
    ledger no real run reaches the second loop at all, so the only thing that
    can establish it still works is a fixture.
    """
    problems: list[str] = []
    for field in sorted(spec - emitted):
        if field not in absent:
            problems.append(
                f"SPEC FIELD NOT SENT: {field}\n"
                f"      `docs/run-tasks.md` says we support the spec on a best-effort basis, so "
                "either emit it, or add it to DELIBERATELY_ABSENT and write the reason into the "
                "divergence table in that document."
            )
    for field, phrase in sorted(absent.items()):
        if field in emitted:
            problems.append(
                f"CLAIMED ABSENT BUT IS SENT: {field}\n"
                "      Remove it from DELIBERATELY_ABSENT and from the divergence table in "
                f"{DOC.name}, which currently tells an integrator we do not send it."
            )
            continue
        if phrase not in table:
            where = "nowhere in" if phrase not in doc else "elsewhere in, but not in that table of"
            problems.append(
                f"ABSENT AND UNDOCUMENTED: {field}\n"
                f"      Not sent, and {phrase!r} appears {where} {DOC.name} under "
                f"{DIVERGENCE_HEADING!r}. A gap has to be a decision an integrator can read "
                "where they look for it, not a line in a gate."
            )
    return problems


def self_test() -> list[str]:
    """Pin the reader, the table slicer and the comparison, before the scan."""
    out: list[str] = []
    for label, src, expected in SELF_TEST_EMITTED:
        try:
            got: set[str] | None = emitted_fields_in(src)
        except SystemExit:
            got = None
        if got != expected:
            out.append(
                f"the builder reader no longer handles {label!r} the way it must: expected "
                f"{'a refusal' if expected is None else sorted(expected)}, got "
                f"{'a refusal' if got is None else sorted(got)}."
            )
    for label, doc, must, must_not in SELF_TEST_TABLE:
        try:
            sliced: str | None = divergence_table(doc)
        except SystemExit:
            sliced = None
        if must is None:
            if sliced is not None:
                out.append(
                    f"the table slicer no longer refuses {label!r}; it returned {sliced!r}. "
                    "A document with no such heading has no table to anchor a gap to."
                )
            continue
        if sliced is None or must not in sliced or (must_not and must_not in sliced):
            out.append(
                f"the table slicer no longer slices {label!r} correctly: it must contain "
                f"{must!r} and not {must_not!r}, and returned {sliced!r}. Slicing is the "
                "whole claim -- a whole-document search passes with the row deleted."
            )
    for label, spec, emitted, absent, table, doc, wants in SELF_TEST_PROBLEMS:
        got_problems = spec_problems(spec, emitted, absent, table, doc)
        ok = len(got_problems) == len(wants) and all(
            all(sub in problem for sub in subs)
            for problem, subs in zip(got_problems, wants, strict=True)
        )
        if not ok:
            out.append(
                f"the spec comparison no longer reports {label!r} the way it must: expected "
                f"{wants}, got {got_problems}."
            )
    return out


def main() -> int:
    # The gate's own health first: a broken reader, slicer or comparison makes
    # every result below meaningless, so say so rather than reporting on nothing.
    blockers = self_test()
    if blockers:
        print("check_run_task_spec_fields cannot do its job:\n")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 1

    problems: list[str] = []
    emitted = emitted_fields()
    doc = DOC.read_text()
    table = divergence_table(doc)

    if len(SPEC_PAYLOAD_FIELDS) < MIN_SPEC_FIELDS:
        problems.append(
            f"only {len(SPEC_PAYLOAD_FIELDS)} spec fields are transcribed, below the floor "
            f"of {MIN_SPEC_FIELDS}. Lines have been removed from SPEC_PAYLOAD_FIELDS, which "
            "silences this gate rather than satisfying it."
        )
    if len(table.strip()) < MIN_TABLE_CHARS:
        problems.append(
            f"the divergence table under {DIVERGENCE_HEADING!r} is only "
            f"{len(table.strip())} characters, below the floor of {MIN_TABLE_CHARS}. "
            "An emptied table would let every gap below read as documented."
        )
    if len(emitted) < MIN_EMITTED:
        problems.append(
            f"only {len(emitted)} payload fields were read from {DISPATCHER.name}, below the "
            f"floor of {MIN_EMITTED}. The AST reader has stopped reading."
        )

    problems.extend(
        spec_problems(set(SPEC_PAYLOAD_FIELDS), emitted, DELIBERATELY_ABSENT, table, doc)
    )

    if problems:
        print("the run task payload and the integration spec disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"OK — {len(emitted)} fields emitted, covering "
        f"{len(SPEC_PAYLOAD_FIELDS & emitted)}/{len(SPEC_PAYLOAD_FIELDS)} spec fields; "
        f"{len(DELIBERATELY_ABSENT)} deliberate gap(s), each documented; "
        f"{len(SELF_TEST_EMITTED) + len(SELF_TEST_TABLE) + len(SELF_TEST_PROBLEMS)} "
        "fixtures hold."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
