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
#: `post_plan`, `pre_apply` and `post_apply`. It is listed here all the same
#: because we send it at no stage, so the distinction makes no difference to
#: what this gate has to check.
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
DELIBERATELY_ABSENT = {
    "plan_json_api_url": "plan_json_api_url",
    "configuration_version_download_url": "configuration_version_download_url",
}

#: Floors, so a matcher that stops matching FAILS rather than passing on nothing.
MIN_SPEC_FIELDS = 20
MIN_EMITTED = 20


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

    if len(SPEC_PAYLOAD_FIELDS) < MIN_SPEC_FIELDS:
        problems.append(
            f"only {len(SPEC_PAYLOAD_FIELDS)} spec fields are transcribed, below the floor "
            f"of {MIN_SPEC_FIELDS}. Lines have been removed from SPEC_PAYLOAD_FIELDS, which "
            "silences this gate rather than satisfying it."
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
        if phrase not in doc:
            problems.append(
                f"ABSENT AND UNDOCUMENTED: {field}\n"
                f"      Not sent, and {phrase!r} appears nowhere in {DOC.name}. A gap has to be "
                "a decision someone can read, not a line in a gate."
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
