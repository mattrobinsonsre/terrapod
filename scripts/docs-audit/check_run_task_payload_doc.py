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

SELF_TEST IS THE GATE ON THE GATE (#2425 §2.7). Both halves are matchers over
text -- an AST walk for the builder, a regex for the heading and its fence --
and `MIN_KEYS` can speak for neither. A floor proves the code side read SOMETHING;
it is blind to a doc matcher that stopped finding the example (which raises, so
that one at least fails loudly) and, worse, blind to a reader that finds a
DIFFERENT block or quietly reads fewer keys while still clearing the floor. The
two readers also compare against each other, so a change that narrows BOTH in the
same way agrees with itself and reports OK. The fixtures below are therefore
asserted before either file is opened, and they include inputs each reader must
REFUSE: a payload whose field names are decided at run time cannot be documented
either, so refusing is the correct answer and the one worth pinning.

STDLIB ONLY, and the dispatcher is read as TEXT via `ast` rather than imported:
importing `run_task_dispatcher` pulls in `db.models` and so SQLAlchemy, which a
`scripts/docs-audit/` job does not install.
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
#: It bounds total breakage only -- SELF_TEST_* is what pins the forms, because
#: a key count cannot tell a payload that shrank from a reader that did.
MIN_KEYS = 10

EXAMPLE = re.compile(r"^##+ Webhook Payload\b.*?```json\n(\{.*?\n\})\n```", re.S | re.M)

#: `None` means the reader must REFUSE: these are payloads whose field names
#: cannot be established statically, and a payload this gate cannot read is one
#: an integrator cannot be given a documented shape for either. Refusing names
#: the symbol and asks for the reader to be taught; passing would vouch for a
#: shape nobody has seen.
SELF_TEST_SOURCE: list[tuple[str, str, list[str] | None]] = [
    (
        "a literal dict, read in source order",
        "def build_task_callback_payload(task, run):\n"
        '    return {"payload_version": 1, "stage": stage, "run_id": run.id}\n',
        ["payload_version", "stage", "run_id"],
    ),
    (
        "order is read, never sorted -- the doc's order is part of the promise",
        'def build_task_callback_payload():\n    return {"zebra": 1, "alpha": 2}\n',
        ["zebra", "alpha"],
    ),
    (
        "a nested object contributes its own key and not its children",
        "def build_task_callback_payload():\n"
        '    return {"capabilities": {"outcomes": False}, "stage": s}\n',
        ["capabilities", "stage"],
    ),
    (
        "a decorated builder is still the builder",
        '@cache\ndef build_task_callback_payload():\n    return {"a": 1}\n',
        ["a"],
    ),
    # Must REFUSE from here down.
    (
        "no builder of that name: fails naming the symbol rather than reading nothing",
        'def something_else():\n    return {"a": 1}\n',
        None,
    ),
    (
        "two builders -- which one ships is not a question a matcher may guess at",
        'def build_task_callback_payload():\n    return {"a": 1}\n'
        'def build_task_callback_payload():\n    return {"b": 1}\n',
        None,
    ),
    (
        "two returns: the payload's shape depends on a branch",
        "def build_task_callback_payload(x):\n"
        '    if x:\n        return {"a": 1}\n    return {"b": 1}\n',
        None,
    ),
    (
        "returns a name rather than a literal -- built, not declared",
        "def build_task_callback_payload():\n    out = {}\n    return out\n",
        None,
    ),
    (
        "a computed key cannot be documented either",
        'def build_task_callback_payload():\n    return {f"run_{x}": 1}\n',
        None,
    ),
    (
        "a splatted helper hides the field names it contributes",
        'def build_task_callback_payload():\n    return {**base(), "stage": s}\n',
        None,
    ),
    (
        "async def: an AsyncFunctionDef is not a FunctionDef, so this fails closed "
        "naming the symbol -- teach the reader rather than letting it read nothing",
        'async def build_task_callback_payload():\n    return {"a": 1}\n',
        None,
    ),
]

_EX = (
    '{\n  "payload_version": 1,\n  "capabilities": {\n    "outcomes": false\n  },'
    '\n  "stage": "pre_apply"\n}'
)
_KEYS = ["payload_version", "capabilities", "stage"]

#: The heading spellings the doc reader must follow, and the ones it must refuse.
#: A refusal here is correct: if the example cannot be found, the body an
#: integration is built against is described nowhere, which is the defect rather
#: than an absence of one.
SELF_TEST_DOC: list[tuple[str, str, list[str] | None]] = [
    (
        "the heading as the doc spells it",
        f"## Webhook Payload\n\nprose\n\n```json\n{_EX}\n```\n",
        _KEYS,
    ),
    ("a deeper heading", f"### Webhook Payload\n\n```json\n{_EX}\n```\n", _KEYS),
    (
        "trailing words on the heading line",
        f"## Webhook Payload (what you receive)\n\n```json\n{_EX}\n```\n",
        _KEYS,
    ),
    (
        "a yaml block in between does not satisfy it -- the json one is the example",
        f"## Webhook Payload\n\n```yaml\nurl: https://example.invalid\n```\n\n```json\n{_EX}\n```\n",
        _KEYS,
    ),
    (
        "an earlier json block under another heading is not the one",
        '## Response\n\n```json\n{\n  "not": "it"\n}\n```\n\n'
        f"## Webhook Payload\n\n```json\n{_EX}\n```\n",
        _KEYS,
    ),
    (
        "order is the example's order",
        '## Webhook Payload\n\n```json\n{\n  "b": 1,\n  "a": 2\n}\n```\n',
        ["b", "a"],
    ),
    # Must REFUSE from here down.
    ("no such heading", f"## Something Else\n\n```json\n{_EX}\n```\n", None),
    (
        "the heading pluralised -- a renamed heading means the reader cannot find "
        "the example, and silently checking a different block would be worse",
        f"## Webhook Payloads\n\n```json\n{_EX}\n```\n",
        None,
    ),
    (
        "the heading with no example under it",
        "## Webhook Payload\n\nprose only, no example\n",
        None,
    ),
    (
        "an example an integrator cannot paste into anything",
        '## Webhook Payload\n\n```json\n{\n  "a": 1,\n}\n```\n',
        None,
    ),
    (
        "an array rather than an object (refused at the matcher, before the type check)",
        "## Webhook Payload\n\n```json\n[\n  1\n]\n```\n",
        None,
    ),
]


def payload_keys_in_source(src: str, where: str = "<fixture>") -> list[str]:
    """The keys `build_task_callback_payload` returns, in source order."""
    tree = ast.parse(src)
    builders = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == BUILDER]
    if len(builders) != 1:
        raise SystemExit(
            f"FAIL — found {len(builders)} definitions of {BUILDER} in {where}, expected exactly 1"
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


def payload_keys_in_doc(text: str, where: str = "<fixture>") -> list[str]:
    """The keys of the JSON example under the doc's Webhook Payload heading."""
    match = EXAMPLE.search(text)
    if match is None:
        raise SystemExit(
            f"FAIL — no JSON example found under a 'Webhook Payload' heading in "
            f"{where}. The payload an integration receives is described nowhere "
            "else, so it must be described there."
        )
    try:
        example = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"FAIL — the Webhook Payload example in {where} is not valid JSON "
            f"({exc}); an integrator cannot paste it into anything."
        ) from exc
    if not isinstance(example, dict):
        raise SystemExit("FAIL — the Webhook Payload example is not a JSON object")
    return list(example.keys())


def payload_keys_from_code() -> list[str]:
    return payload_keys_in_source(DISPATCHER.read_text(), str(DISPATCHER.relative_to(ROOT)))


def payload_keys_from_doc() -> list[str]:
    return payload_keys_in_doc(DOC.read_text(), str(DOC.relative_to(ROOT)))


def _run(reader, text: str) -> list[str] | None:
    """A reader's answer, or None when it refused."""
    try:
        return reader(text)
    except SystemExit:
        return None


def compare(code: list[str], doc: list[str]) -> list[str]:
    """How the example disagrees with the builder. Empty means it agrees.

    Separated from `main` so SELF_TEST_COMPARE can drive it. The ORDER half of
    this gate's promise is otherwise unreachable by any fixture over the real
    tree: the two key lists agree today, so relaxing this to compare sorted
    lists would pass every run and silently withdraw the claim the docstring
    makes about reading the example top to bottom.
    """
    if code == doc:
        return []
    missing = [k for k in code if k not in doc]
    extra = [k for k in doc if k not in code]
    out: list[str] = []
    if missing:
        out.append(f"  sent but NOT documented: {', '.join(missing)}")
    if extra:
        out.append(
            f"  documented but NOT sent: {', '.join(extra)}"
            "  <- an integration written from the docs would KeyError on these"
        )
    if not missing and not extra:
        out.append(f"  same fields, different order:\n    code: {code}\n    doc : {doc}")
    return out


#: (code keys, doc keys, substrings every reported line must cover).
SELF_TEST_COMPARE: list[tuple[list[str], list[str], list[str]]] = [
    (["a", "b"], ["a", "b"], []),
    # The order half. Same membership, so a sorted comparison reports nothing.
    (["a", "b"], ["b", "a"], ["different order"]),
    (["a", "b"], ["a"], ["sent but NOT documented"]),
    (["a"], ["a", "b"], ["documented but NOT sent"]),
    (["a", "b"], ["b", "c"], ["sent but NOT documented", "documented but NOT sent"]),
]


def self_test() -> list[str]:
    """Pin both readers against literals, before either real file is opened."""
    out: list[str] = []
    for code, doc, wants in SELF_TEST_COMPARE:
        got = compare(code, doc)
        if len(got) != len(wants) or not all(any(w in g for g in got) for w in wants):
            out.append(
                f"the comparison no longer reports {code} against {doc} the way it "
                f"must: expected {wants}, got {got}. Membership is not the whole "
                "promise -- the example is read top to bottom, so order is part of it."
            )
    for label, src, expected in SELF_TEST_SOURCE:
        got = _run(payload_keys_in_source, src)
        if got != expected:
            out.append(
                f"the builder reader no longer handles {label!r} the way it must: "
                f"expected {'a refusal' if expected is None else expected}, got "
                f"{'a refusal' if got is None else got}. Teach the reader the new "
                "shape -- a payload it cannot read is one it must not vouch for."
            )
    for label, text, expected in SELF_TEST_DOC:
        got = _run(payload_keys_in_doc, text)
        if got != expected:
            out.append(
                f"the doc reader no longer handles {label!r} the way it must: "
                f"expected {'a refusal' if expected is None else expected}, got "
                f"{'a refusal' if got is None else got}. Widen the matcher rather "
                "than the fixture -- a spelling it cannot see is an example it does "
                "not check."
            )
    return out


def main() -> int:
    blockers = self_test()
    if blockers:
        print("check_run_task_payload_doc cannot do its job:\n")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 1

    code = payload_keys_from_code()
    doc = payload_keys_from_doc()

    if len(code) < MIN_KEYS:
        print(
            f"FAIL — read only {len(code)} payload keys out of {BUILDER}, below the "
            f"floor of {MIN_KEYS}. The reader has probably stopped matching; fix it "
            "rather than lowering the floor."
        )
        return 1

    disagreements = compare(code, doc)
    if not disagreements:
        print(
            f"OK — the documented run task payload matches {BUILDER}: "
            f"{len(code)} field(s), same order; "
            f"{len(SELF_TEST_SOURCE) + len(SELF_TEST_DOC) + len(SELF_TEST_COMPARE)} "
            "fixtures hold"
        )
        return 0

    print(f"FAIL — {DOC.relative_to(ROOT)}'s Webhook Payload example disagrees with {BUILDER}")
    for line in disagreements:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
