#!/usr/bin/env python3
"""The health-condition codes the server emits are the ones every reader lists.

`_compute_health_conditions` (`services/terrapod/api/routers/tfe_v2.py`) is the
only producer. Four hand-kept lists restate it, and a new server condition fails
none of their tests: `FLAG_CODES` (`web/src/lib/workspace-filter.ts`), the
`workspaces.flagName` keys (`web/messages/en.json`, kebab), the codes table in
`docs/api-reference.md`, and the `flag` row in `docs/rbac.md` (kebab). A gate
here because the test image copies only `services/`. Producer read as text; any
shape it cannot read fails loudly, naming the line, rather than passing.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PRODUCER = Path("services/terrapod/api/routers/tfe_v2.py")
FUNCTION = "_compute_health_conditions"
LIST_NAME = "conditions"
FLAG_CODES_SOURCE = Path("web/src/lib/workspace-filter.ts")
MESSAGES = Path("web/messages/en.json")
API_DOC = Path("docs/api-reference.md")
API_DOC_HEADING = "### Health Conditions"
RBAC_DOC = Path("docs/rbac.md")

MIN_CODES = 6


def _kebab(code: str) -> str:
    return code.replace("_", "-")


def _fail(message: str) -> SystemExit:
    return SystemExit(f"FAIL — {message}")


def _producer_codes() -> set[str]:
    tree = ast.parse(PRODUCER.read_text())
    func = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == FUNCTION
        ),
        None,
    )
    if func is None:
        raise _fail(f"{PRODUCER}: {FUNCTION} not found. If it moved, move this gate with it.")

    parents = {child: parent for parent in ast.walk(func) for child in ast.iter_child_nodes(parent)}
    codes: set[str] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Name) and node.id == LIST_NAME):
            continue
        parent = parents[node]
        grand = parents.get(parent)
        if isinstance(parent, ast.Return):
            continue
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            if isinstance(parent.value, ast.List) and not parent.value.elts:
                continue
        elif (
            isinstance(parent, ast.Attribute)
            and parent.attr == "append"
            and isinstance(grand, ast.Call)
            and grand.func is parent
            and len(grand.args) == 1
            and not grand.keywords
        ):
            code = _literal_code(grand.args[0])
            if code is not None:
                codes.add(code)
                continue
        raise _fail(
            f"{PRODUCER}:{node.lineno}: `{LIST_NAME}` is used in a way this reader cannot "
            'vouch for. Only `.append({"code": "<literal>", ...})` is understood; teach this '
            "reader the new shape rather than copying the list here."
        )
    if len(codes) < MIN_CODES:
        raise _fail(
            f"{PRODUCER}: only {len(codes)} code(s) found in {FUNCTION} (floor {MIN_CODES}); "
            "the matcher has stopped matching or the function changed shape."
        )
    return codes


def _literal_code(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "code":
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _flag_codes_ts() -> set[str]:
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", FLAG_CODES_SOURCE.read_text(), flags=re.DOTALL)
    match = re.search(r"export const FLAG_CODES[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    if not match:
        raise _fail(f"{FLAG_CODES_SOURCE}: no `FLAG_CODES = [...]` array literal found")
    body = match.group(1)
    items = re.findall(r"""'([^'\n]*)'|"([^"\n]*)\"""", body)
    leftover = re.sub(r"""'[^'\n]*'|"[^"\n]*\"""", "", body)
    leftover = re.sub(r"[\s,]+", " ", leftover).strip()
    if leftover:
        raise _fail(
            f"{FLAG_CODES_SOURCE}: FLAG_CODES holds more than plain string items "
            f"({leftover!r}); teach this reader the new shape."
        )
    return {a or b for a, b in items}


def _flag_name_keys() -> set[str]:
    flag_name = json.loads(MESSAGES.read_text()).get("workspaces", {}).get("flagName")
    if not isinstance(flag_name, dict):
        raise _fail(f"{MESSAGES}: no `workspaces.flagName` object found")
    return set(flag_name)


def _api_doc_codes() -> set[str]:
    lines = API_DOC.read_text().splitlines()
    if API_DOC_HEADING not in lines:
        raise _fail(f"{API_DOC}: heading {API_DOC_HEADING!r} not found")
    codes: set[str] = set()
    for line in lines[lines.index(API_DOC_HEADING) + 1 :]:
        if line.startswith("#"):
            break
        row = re.match(r"\| `([a-z_]+)` \|", line)
        if row:
            codes.add(row.group(1))
    if not codes:
        raise _fail(f"{API_DOC}: no `| `code` |` rows under {API_DOC_HEADING!r}")
    return codes


def _rbac_flag_values() -> set[str]:
    for line in RBAC_DOC.read_text().splitlines():
        if line.startswith("| `flag` |"):
            cells = line.split("|")
            return set(re.findall(r"`([a-z-]+)`", cells[2]))
    raise _fail(f"{RBAC_DOC}: no `| `flag` |` row found in the reserved-keys table")


def main() -> int:
    produced = _producer_codes()
    kebab = {_kebab(c) for c in produced}
    readers = (
        (f"{FLAG_CODES_SOURCE} FLAG_CODES", _flag_codes_ts(), produced),
        (f"{MESSAGES} workspaces.flagName", _flag_name_keys(), kebab),
        (f"{API_DOC} codes table", _api_doc_codes(), produced),
        (f"{RBAC_DOC} `flag` row", _rbac_flag_values(), kebab),
    )

    problems: list[str] = []
    for name, found, expected in readers:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        if missing or extra:
            problems.append(f"{name}: missing {missing or 'none'}, unknown {extra or 'none'}")

    if problems:
        print(f"FAIL — {len(problems)} reader(s) disagree with {FUNCTION} in {PRODUCER}:")
        for problem in problems:
            print(f"  - {problem}")
        print("Update every reader together with the producer.")
        return 1

    print(f"PASS — {len(produced)} health-condition codes; all {len(readers)} readers agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
