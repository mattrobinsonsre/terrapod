#!/usr/bin/env python3
"""The health-condition codes the server emits are the ones every reader lists.

`_compute_health_conditions` in `services/terrapod/api/routers/tfe_v2.py` is the
only producer of the codes in a workspace's `health-conditions`. Four places
restate that list by hand, and an eighth condition added to the server fails
none of their tests:

* `FLAG_CODES` in `web/src/lib/workspace-filter.ts`, from which the `flag:`
  values, the Flags dropdown and the Health Issues chip derive;
* the `workspaces.flagName` keys in `web/messages/en.json` (kebab form; the
  other locales are held to `en` by `i18n:check`);
* the codes table under "Health Conditions" in `docs/api-reference.md`;
* the `flag` row of the reserved-keys table in `docs/rbac.md` (kebab form).

**A docs-audit gate and not a test beside `FLAG_CODES`:** the unit test pins
`FLAG_CODES` to a literal in the same test file, and `docker/Dockerfile.test`
copies only `services/`, so a pytest cannot see `web/` or `docs/`.

**Read as TEXT, never imported.** Importing the router drags in the API's whole
dependency closure, which the job running this does not install.

**It refuses rather than guesses.** A non-literal code, or a reader it cannot
find, fails naming the file, and the producer must yield at least `MIN_CODES`
codes so a matcher that stops matching fails instead of passing on nothing.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PRODUCER = Path("services/terrapod/api/routers/tfe_v2.py")
FUNCTION = "_compute_health_conditions"
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

    codes: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "code"):
                continue
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                raise _fail(
                    f"{PRODUCER}:{value.lineno}: a condition's code is not a string "
                    "literal. Teach this reader the new shape; do not copy the list here."
                )
            codes.add(value.value)
    if len(codes) < MIN_CODES:
        raise _fail(
            f"{PRODUCER}: only {len(codes)} code(s) found in {FUNCTION} (floor {MIN_CODES}); "
            "the matcher has stopped matching or the function changed shape."
        )
    return codes


def _flag_codes_ts() -> set[str]:
    text = FLAG_CODES_SOURCE.read_text()
    match = re.search(r"export const FLAG_CODES[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    if not match:
        raise _fail(f"{FLAG_CODES_SOURCE}: no `FLAG_CODES = [...]` array literal found")
    return set(re.findall(r"""['"]([^'"]+)['"]""", match.group(1)))


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
