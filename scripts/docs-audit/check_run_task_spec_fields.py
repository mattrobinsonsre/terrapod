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


def emitted_fields() -> set[str]:
    """The payload's keys, read out of the builder's single return dict."""
    tree = ast.parse(DISPATCHER.read_text())
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


def main() -> int:
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

    for field in sorted(SPEC_PAYLOAD_FIELDS - emitted):
        if field not in DELIBERATELY_ABSENT:
            problems.append(
                f"SPEC FIELD NOT SENT: {field}\n"
                f"      `docs/run-tasks.md` says we support the spec on a best-effort basis, so "
                "either emit it, or add it to DELIBERATELY_ABSENT and write the reason into the "
                "divergence table in that document."
            )

    for field, phrase in sorted(DELIBERATELY_ABSENT.items()):
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

    if problems:
        print("the run task payload and the integration spec disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"OK — {len(emitted)} fields emitted, covering "
        f"{len(SPEC_PAYLOAD_FIELDS & emitted)}/{len(SPEC_PAYLOAD_FIELDS)} spec fields; "
        f"{len(DELIBERATELY_ABSENT)} deliberate gap(s), each documented."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
