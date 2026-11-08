#!/usr/bin/env python3
"""The CLI-surface body catalogues say what we consume. They must be true (#2389).

`docs/tfe-cli-surface.md` catalogues the **request body** of every route on the
compatibility surface that takes one, and each attribute carries a claim: we
consume it, or we accept it and do not. Those claims are the whole value of the
catalogues -- #1903 happened because `POST /api/tfe/v2/runs` was served
correctly while `save-plan` was read off its body by nobody, and no route table
could show it.

A claim with nothing behind it is the same defect one level up, so this checks
the half that needs no `go-tfe` checkout:

1. every attribute a catalogue calls **consumed** is really read by the handler
   that serves that route; and
2. every attribute it calls **accepted and not consumed** (or **not
   supported**) really is not.

The second direction is the one that rots. If someone wires up the run-action
`comment`, the catalogue goes on telling an integrator it is dropped -- a wrong
REASON, which reads as considered and survives review.

**What it deliberately cannot do**, and both have cost something:

Tell you HashiCorp has added a field to an `*Options` struct. That needs the
go-tfe source, which would make a documentation gate depend on a Go module
cache, so it stays a step in the document's own maintenance procedure.

Tell a PER-VERB claim from a per-heading one. `HANDLERS` maps a heading to the
set of functions serving it, so "Workspace create/PATCH attributes" is satisfied
when EITHER `_create_workspace_impl` or `update_workspace` reads a key -- which
is how the catalogue came to say `terraform-version` was *"Dropped on `POST`,
consumed on `PATCH`"* while both verbs honoured it (#2425 §5.2). The row passed
here via the update path.

Splitting the mapping per verb would not fix it either, and that is the harder
half: both handlers read that key through the shared `engine_version_attr`, so
the literal appears in NEITHER function and an AST search of either one finds
nothing. A gate that followed every helper call would be a call-graph walk, and
the recorded reason this file reads literals is that a narrower matcher which
stops matching fails loudly rather than passing on nothing. So a per-verb claim
is reviewed, not gated -- and the safer thing to write is no per-verb claim at
all, because a key read through a shared helper is consumed wherever that helper
is called.

STDLIB ONLY: it reads the document as text and the routers with `ast`.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "tfe-cli-surface.md"
ROUTERS = ROOT / "services" / "terrapod" / "api" / "routers"

#: Catalogue heading -> the handler functions that serve those routes. Small,
#: structural, and CHECKED rather than trusted: every function named here must
#: exist, so a rename fails this gate instead of silently narrowing it.
HANDLERS: dict[str, dict[str, list[str]]] = {
    "Workspace create/PATCH attributes": {
        "tfe_v2.py": ["_create_workspace_impl", "update_workspace", "_resolve_pool_set_attrs"],
    },
    "Workspace lock attributes": {
        "tfe_v2.py": ["lock_workspace", "unlock_workspace", "_lock_reason_from_body"],
    },
    "State-version-create attributes": {"tfe_v2.py": ["create_state_version"]},
    "Configuration-version-create attributes": {
        "config_versions.py": ["create_configuration_version"],
    },
    "Run-action attributes (`apply` / `discard` / `cancel`)": {
        "runs.py": ["confirm_run", "discard_run", "cancel_run"],
    },
    "Variable and variable-set attributes": {
        "variables.py": [
            "create_workspace_var",
            "update_workspace_var",
            "create_varset",
            "update_varset",
            "create_varset_var",
            "update_varset_var",
            # Shared helpers that read the body on these routes' behalf. Named
            # for the same reason `_lock_reason_from_body` is: the question is
            # whether the route looks at the key, not which frame does.
            "_structured_from",
        ],
    },
}

#: Floors, so a matcher that stops matching FAILS rather than passing on nothing.
MIN_CATALOGUES = 5
MIN_CONSUMED = 20
MIN_NOT_CONSUMED = 8


def read_keys(path: pathlib.Path, fn_names: list[str]) -> tuple[set[str], set[str]]:
    """Every string key the named functions read off a request body.

    Returns (keys, missing_function_names). A `.get("x")` on anything, plus
    `"x" in thing` -- deliberately broad, because the question is whether the
    handler looks at the key at all, and narrowing by receiver name is how a
    gate comes to miss a read through a differently-named local.
    """
    tree = ast.parse(path.read_text())
    byname: dict[str, ast.AST] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            byname[n.name] = n
    keys: set[str] = set()
    missing = {f for f in fn_names if f not in byname}
    for f in fn_names:
        node = byname.get(f)
        if node is None:
            continue
        for x in ast.walk(node):
            if (
                isinstance(x, ast.Call)
                and isinstance(x.func, ast.Attribute)
                and x.func.attr == "get"
                and x.args
                and isinstance(x.args[0], ast.Constant)
                and isinstance(x.args[0].value, str)
            ):
                keys.add(x.args[0].value)
            elif isinstance(x, ast.Subscript) and isinstance(x.slice, ast.Constant):
                if isinstance(x.slice.value, str):
                    keys.add(x.slice.value)
            elif isinstance(x, ast.Compare) and isinstance(x.left, ast.Constant):
                if isinstance(x.left.value, str) and any(
                    isinstance(o, (ast.In, ast.NotIn)) for o in x.ops
                ):
                    keys.add(x.left.value)
            elif isinstance(x, (ast.For, ast.AsyncFor)) and isinstance(
                x.iter, (ast.Tuple, ast.List, ast.Set)
            ):
                # `for key in ("reason", "Info", "Operation"): body.get(key)`.
                # The keys are in the iterable, never in the `.get` call, so a
                # constant-argument walk alone reports a true claim as unread.
                for el in x.iter.elts:
                    if isinstance(el, ast.Constant) and isinstance(el.value, str):
                        keys.add(el.value)
    return keys, missing


#: An attribute claimed BOTH consumed and not-consumed is only credible if its
#: not-consumed row says which verb is which. Without that the reader cannot use
#: it either, and -- the reason this is enforced -- leaving the consumed row in
#: place would otherwise silence a false not-consumed claim. Found by mutating
#: `serial` into the not-consumed table and watching the gate pass.
ASYMMETRY = re.compile(r"\b(?:POST|PATCH|PUT|create|update|on `POST`|on `PATCH`)\b")


def catalogues(doc: str) -> dict[str, dict[str, set[str]]]:
    """Heading -> {"consumed": {...}, "not_consumed": {...}}.

    A table's class comes from the nearest preceding prose marker, because that
    is what a reader goes by too.
    """
    out: dict[str, dict[str, set[str]]] = {}
    blocks = re.split(r"^### ", doc, flags=re.M)[1:]
    for b in blocks:
        heading = b.split("\n", 1)[0].strip()
        if heading not in HANDLERS:
            continue
        cls = "consumed"
        found = {"consumed": set(), "not_consumed": set()}
        rowtext: dict[str, str] = {}
        for line in b.split("\n"):
            # A TABLE ROW IS NEVER A MARKER (#2425 §5.2). The marker checks used
            # to run on every line, so a row whose cell contained a marker's
            # words reclassified every row BELOW it -- measured: rewriting the
            # `terraform-version` row to read "Consumed, on both verbs" moved
            # three rows out of the not-consumed table and failed this gate on
            # `tags`, which nobody had touched. The classification is prose's to
            # make, so only prose is read for it.
            if line.startswith("|"):
                if not line.startswith("| `"):
                    continue
            else:
                low = line.lower()
                if "accepted and not consumed" in low or "not supported" in low:
                    cls = "not_consumed"
                elif re.match(r"^consumed\b", low) or "consumed, on both" in low:
                    cls = "consumed"
                continue
            cell = line.split("|")[1]
            for name in re.findall(r"`([a-z0-9][a-z0-9.\-]*)`", cell):
                found[cls].add(name)
                if cls == "not_consumed":
                    rowtext[name] = line
        found["_rowtext"] = rowtext  # type: ignore[assignment]
        out[heading] = found
    return out


def main() -> int:
    doc = DOC.read_text()
    cats = catalogues(doc)
    problems: list[str] = []

    if len(cats) < MIN_CATALOGUES:
        problems.append(
            f"only {len(cats)} body catalogue(s) matched, below the floor of "
            f"{MIN_CATALOGUES}. The `###` heading matcher has stopped matching; fix it "
            "rather than lowering the floor."
        )

    n_consumed = n_not = 0
    for heading, claims in sorted(cats.items()):
        keys: set[str] = set()
        for fname, fns in HANDLERS[heading].items():
            path = ROUTERS / fname
            if not path.exists():
                problems.append(f"[{heading}] {fname} is missing; this gate reads it")
                continue
            got, missing = read_keys(path, fns)
            keys |= got
            for m in sorted(missing):
                problems.append(
                    f"[{heading}] {fname} has no function `{m}`. Renamed? Teach this gate, "
                    "or it silently stops checking that route."
                )

        # An attribute in BOTH tables is the asymmetric case -- consumed on one
        # verb and dropped on another (`terraform-version` on workspaces). Both
        # claims are true, so neither read-check applies; it is derived from the
        # document rather than exempted by hand.
        rowtext: dict[str, str] = claims.get("_rowtext", {})  # type: ignore[assignment]
        both = set()
        for a in claims["consumed"] & claims["not_consumed"]:
            if ASYMMETRY.search(rowtext.get(a, "")):
                both.add(a)
            else:
                problems.append(
                    f"[{heading}] CLAIMED BOTH WAYS WITHOUT SAYING WHICH VERB: `{a}`\n"
                    "      It is in the consumed table and the not-consumed table, and the "
                    "not-consumed row names no verb. Either say which route drops it (as "
                    "`terraform-version` does) or remove the wrong row -- as written, a reader "
                    "cannot tell, and the duplicate silences this gate's read check."
                )

        for a in sorted(claims["consumed"] - both):
            n_consumed += 1
            if a not in keys:
                problems.append(
                    f"[{heading}] CLAIMED CONSUMED BUT NOT READ: `{a}`\n"
                    f"      No handler in {','.join(HANDLERS[heading])} reads it. Either the "
                    "catalogue is wrong, or the code stopped reading it and the document now "
                    "promises behaviour that is gone."
                )
        for a in sorted(claims["not_consumed"] - both):
            n_not += 1
            if a in keys:
                problems.append(
                    f"[{heading}] CLAIMED NOT CONSUMED BUT IS READ: `{a}`\n"
                    "      The catalogue tells an integrator we drop it and we do not. Move the "
                    "row to the consumed table and say what we do with it."
                )

    if n_consumed < MIN_CONSUMED:
        problems.append(
            f"only {n_consumed} consumed claim(s) checked, below the floor of {MIN_CONSUMED}"
        )
    if n_not < MIN_NOT_CONSUMED:
        problems.append(
            f"only {n_not} not-consumed claim(s) checked, below the floor of {MIN_NOT_CONSUMED}"
        )

    if problems:
        print("the CLI-surface body catalogues disagree with the code:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"OK — {len(cats)} body catalogue(s): {n_consumed} consumed claim(s) all read, "
        f"{n_not} not-consumed claim(s) all absent."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
