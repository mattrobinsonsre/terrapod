#!/usr/bin/env python3
"""The documented run task payload is the payload we actually send.

`docs/run-tasks.md` carries a JSON example of the webhook body an external
run task service receives. It is the only description of that body anyone
builds an integration against -- there is no schema, no OpenAPI entry and no
response contract, because the payload is something Terrapod *sends* rather
than serves, so none of the snapshot gates can see it.

It had drifted in both directions at once (#2367). The example advertised a
`run_status` field the code has never emitted -- so an integration written
from our own documentation would `KeyError` on it -- and omitted five fields
the code does emit. The fields were wrong too, which is the other half of
#2367; this gate is about the description of them.

So: the example's key set must equal the key set
`run_task_dispatcher.build_task_callback_payload` returns, in the same order.
Order as well as membership, because the example is read top to bottom by a
person deciding what they can rely on, and a key that has quietly moved to
the end reads as a key that was added later.

It compares KEYS and says nothing about values. The example's values are
illustrative placeholders (`run-uuid`, `cost-check`) and must stay readable;
asserting them would mean generating the block, which would make the
documentation a build artifact to keep a property that nobody has ever got
wrong. What goes wrong is a field appearing or disappearing.

A dict it cannot read statically is a failure rather than a skip, following
`check_pulumi_plugin_naming.py`: a payload assembled at run time is one this
gate cannot vouch for, and silently vouching for it is the defect.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = ROOT / "services/terrapod/services/run_task_dispatcher.py"
DOC = ROOT / "docs/run-tasks.md"
BUILDER = "build_task_callback_payload"

#: A matcher that stops matching must fail rather than pass on nothing. The
#: payload has sixteen keys; the floor is what turns "the regex found an empty
#: dict" into a failure instead of a silent agreement between two empty sets.
MIN_KEYS = 10


def payload_keys_from_code() -> list[str]:
    """The keys `build_task_callback_payload` returns, in source order."""
    tree = ast.parse(DISPATCHER.read_text())
    builders = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == BUILDER]
    if len(builders) != 1:
        raise SystemExit(
            f"FAIL — found {len(builders)} definitions of {BUILDER} in "
            f"{DISPATCHER.relative_to(ROOT)}, expected exactly 1"
        )
    returns = [n for n in ast.walk(builders[0]) if isinstance(n, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Dict):
        raise SystemExit(
            f"FAIL — {BUILDER} no longer returns a single literal dict. This gate "
            "reads the payload's shape statically and cannot vouch for one built at "
            "run time; teach it the new shape rather than deleting it."
        )
    keys = []
    for key in returns[0].value.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise SystemExit(
                f"FAIL — {BUILDER} returns a dict with a computed key; this gate "
                "cannot read it, and a payload whose field names are decided at run "
                "time cannot be documented either."
            )
        keys.append(key.value)
    return keys


def payload_keys_from_doc() -> list[str]:
    """The keys of the JSON example under the doc's Webhook Payload heading."""
    text = DOC.read_text()
    match = re.search(r"^##+ Webhook Payload\b.*?```json\n(\{.*?\n\})\n```", text, re.S | re.M)
    if match is None:
        raise SystemExit(
            f"FAIL — no JSON example found under a 'Webhook Payload' heading in "
            f"{DOC.relative_to(ROOT)}. The payload an integration receives is "
            "described nowhere else, so it must be described there."
        )
    try:
        example = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"FAIL — the Webhook Payload example in {DOC.relative_to(ROOT)} is not "
            f"valid JSON ({exc}); an integrator cannot paste it into anything."
        ) from exc
    if not isinstance(example, dict):
        raise SystemExit("FAIL — the Webhook Payload example is not a JSON object")
    return list(example.keys())


def main() -> int:
    code = payload_keys_from_code()
    doc = payload_keys_from_doc()

    if len(code) < MIN_KEYS:
        print(
            f"FAIL — read only {len(code)} payload keys out of {BUILDER}, below the "
            f"floor of {MIN_KEYS}. The reader has probably stopped matching; fix it "
            "rather than lowering the floor."
        )
        return 1

    if code == doc:
        print(
            f"OK — the documented run task payload matches {BUILDER}: "
            f"{len(code)} field(s), same order"
        )
        return 0

    missing = [k for k in code if k not in doc]
    extra = [k for k in doc if k not in code]
    print(f"FAIL — {DOC.relative_to(ROOT)}'s Webhook Payload example disagrees with {BUILDER}")
    if missing:
        print(f"  sent but NOT documented: {', '.join(missing)}")
    if extra:
        print(
            f"  documented but NOT sent: {', '.join(extra)}"
            "  <- an integration written from the docs would KeyError on these"
        )
    if not missing and not extra:
        print(f"  same fields, different order:\n    code: {code}\n    doc : {doc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
