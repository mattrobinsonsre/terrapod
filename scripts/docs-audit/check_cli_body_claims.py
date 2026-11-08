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

The three floors below prove the claims and the catalogues are non-empty and
say nothing about whether any predicate discriminates (#2425 §2.7). A table
reader that quietly stops seeing one marker spelling still counts twenty-odd
claims from the rest; a key reader that drops one AST shape still finds plenty;
and the comparison itself -- the read-check in both directions, plus the refusal
of a both-ways claim that names no verb -- has no floor at all. So `SELF_TEST_*`
pins each form and each decision against literal fixtures, asserted BEFORE the
document is read, including inputs that must NOT be read.

Two of those fixtures exist because this gate has already been wrong in exactly
those places: a row whose cell contains a marker's words must not reclassify the
rows below it (#2425 §5.2, which failed the gate on a `tags` row nobody had
touched), and a both-ways claim with no verb named must be refused rather than
silencing both read-checks (found by mutating `serial` into the not-consumed
table and watching the gate pass).

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


def read_keys_from_source(src: str, fn_names: list[str]) -> tuple[set[str], set[str]]:
    """Every string key the named functions read off a request body.

    Returns (keys, missing_function_names). A `.get("x")` on anything, plus
    `"x" in thing` -- deliberately broad, because the question is whether the
    handler looks at the key at all, and narrowing by receiver name is how a
    gate comes to miss a read through a differently-named local.

    Pure, over source text, so `SELF_TEST_READS` drives exactly what the scan
    drives rather than a paraphrase of it.
    """
    tree = ast.parse(src)
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


def read_keys(path: pathlib.Path, fn_names: list[str]) -> tuple[set[str], set[str]]:
    return read_keys_from_source(path.read_text(), fn_names)


#: An attribute claimed BOTH consumed and not-consumed is only credible if its
#: not-consumed row says which verb is which. Without that the reader cannot use
#: it either, and -- the reason this is enforced -- leaving the consumed row in
#: place would otherwise silence a false not-consumed claim. Found by mutating
#: `serial` into the not-consumed table and watching the gate pass.
ASYMMETRY = re.compile(r"\b(?:POST|PATCH|PUT|create|update|on `POST`|on `PATCH`)\b")

#: A heading that is not a real catalogue, so the fixtures below do not depend
#: on what `HANDLERS` happens to hold today.
_FIXTURE_HEADING = "Fixture attributes"

#: Markdown -> `(consumed, not_consumed)` the table reader must yield.
SELF_TEST_TABLES: list[tuple[str, list[str], list[str]]] = [
    # The consumed marker, and only the FIRST cell is read -- the go-tfe field
    # in column two is a Go name, not a wire attribute.
    (
        f"### {_FIXTURE_HEADING}\n\n"
        "Consumed, on both `POST` and `PATCH` unless noted:\n\n"
        "| Attribute | go-tfe field | Terrapod behaviour |\n|---|---|---|\n"
        "| `name` | `name-field` | The workspace name |\n"
        "| `auto-apply` | `AutoApply` | Apply without confirmation |\n",
        ["auto-apply", "name"],
        [],
    ),
    # Both not-consumed marker spellings.
    (
        f"### {_FIXTURE_HEADING}\n\n"
        "**Accepted and not consumed** — the class that is a defect:\n\n"
        "| Attribute | go-tfe field | What happens |\n|---|---|---|\n"
        "| `vcs-repo` | `VCSRepo` | Dropped. |\n",
        [],
        ["vcs-repo"],
    ),
    (
        f"### {_FIXTURE_HEADING}\n\n"
        "**Not supported — Terrapod has no such feature.**\n\n"
        "| Attribute | go-tfe field |\n|---|---|\n"
        "| `hyok-enabled` | `HYOKEnabled` |\n",
        [],
        ["hyok-enabled"],
    ),
    # THE RECORDED DEFECT (#2425 §5.2): A TABLE ROW IS NEVER A MARKER. A cell
    # containing a marker's words used to reclassify every row BELOW it, which
    # failed this gate on `tags` -- a row nobody had touched.
    (
        f"### {_FIXTURE_HEADING}\n\n"
        "Consumed, on both `POST` and `PATCH` unless noted:\n\n"
        "| Attribute | go-tfe field | Terrapod behaviour |\n|---|---|---|\n"
        "| `vcs-repo` | `VCSRepo` | Not supported on the native surface either |\n"
        "| `tags` | `Tags` | Accepted and not consumed is what the row below used to say |\n"
        "| `name` | `Name` | Still consumed |\n",
        ["name", "tags", "vcs-repo"],
        [],
    ),
    # Several names in one cell, as the auto-destroy row really is.
    (
        f"### {_FIXTURE_HEADING}\n\n"
        "**Not supported — Terrapod has no such feature.**\n\n"
        "| Attribute | go-tfe field |\n|---|---|\n"
        "| `auto-destroy-at`, `auto-destroy-activity-duration` | the `AutoDestroy*` set |\n",
        [],
        ["auto-destroy-activity-duration", "auto-destroy-at"],
    ),
    # MUST NOT MATCH: a heading that is not a catalogue contributes nothing, so
    # the fixture heading is absent and both sets come back empty.
    (
        "### Some other subsection\n\nConsumed:\n\n| Attribute |\n|---|\n| `smuggled-in` |\n",
        [],
        [],
    ),
    # MUST NOT MATCH: header and separator rows, and a wire attribute must be
    # lower-case -- `Name` in the first cell is a Go field, not an attribute.
    (
        f"### {_FIXTURE_HEADING}\n\nConsumed:\n\n"
        "| Attribute | go-tfe field |\n|---|---|\n| `Name` | `Name` |\n",
        [],
        [],
    ),
]

#: A not-consumed row -> whether it names the verb that drops the attribute. The
#: pair is the discrimination: without it, a duplicated row silences the read
#: check, which is how `serial` passed when it was mutated into both tables.
SELF_TEST_ASYMMETRY: list[tuple[str, bool]] = [
    ("| `terraform-version` | `TerraformVersion` | Dropped on `POST`, consumed on `PATCH` |", True),
    ("| `tags` | `Tags` | Dropped on create. |", True),
    ("| `serial` | `Serial` | Dropped. |", False),
    ("| `vcs-repo` | `VCSRepo` | Dropped; the caller's whole intent is lost |", False),
]

#: `(source, functions, keys, missing)` the key reader must yield.
SELF_TEST_READS: list[tuple[str, tuple[str, ...], list[str], list[str]]] = [
    ('def f(body):\n    return body.get("comment")\n', ("f",), ["comment"], []),
    ('def f(body):\n    return body["lineage"]\n', ("f",), ["lineage"], []),
    ('def f(body):\n    if "serial" in body:\n        pass\n', ("f",), ["serial"], []),
    ('def f(body):\n    if "serial" not in body:\n        pass\n', ("f",), ["serial"], []),
    # The recorded loop form: the keys are in the iterable, never in the `.get`,
    # so a constant-argument walk alone reports a true claim as unread.
    (
        'def f(body):\n    for key in ("reason", "Info", "Operation"):\n        body.get(key)\n',
        ("f",),
        ["Info", "Operation", "reason"],
        [],
    ),
    # A read inside a nested function still counts: the question is whether the
    # route looks at the key, not which frame does.
    (
        'def outer(body):\n    def inner():\n        return body.get("nested")\n'
        "    return inner()\n",
        ("outer",),
        ["nested"],
        [],
    ),
    # Deliberately broad: a `.get` on any receiver, because narrowing by
    # receiver name is how a gate misses a read through a renamed local.
    ('def f(req):\n    return req.headers.get("x-run-id")\n', ("f",), ["x-run-id"], []),
    # MUST NOT MATCH: a key read by a function that was not named. This is the
    # scoping property -- without it the gate would pass any claim satisfied
    # anywhere in the module.
    (
        'def a(body):\n    return body.get("mine")\n\n\n'
        'def b(body):\n    return body.get("theirs")\n',
        ("a",),
        ["mine"],
        [],
    ),
    # A renamed handler is reported, not silently skipped.
    (
        'def a(body):\n    return body.get("mine")\n',
        ("a", "gone"),
        ["mine"],
        ["gone"],
    ),
    # MUST NOT MATCH: a non-constant key cannot be checked. A documented limit.
    ("def f(body, k):\n    return body.get(k)\n", ("f",), [], []),
]

#: `(label, claims, keys, expected problem count)`. The floors cannot see any of
#: this: they count claims, not whether the comparison discriminates.
SELF_TEST_DECISION: list[tuple[str, dict[str, set[str]], set[str], int]] = [
    ("consumed and read", {"consumed": {"name"}, "not_consumed": set()}, {"name"}, 0),
    # The #1903 shape: the catalogue promises a read nobody performs.
    ("consumed, not read", {"consumed": {"name"}, "not_consumed": set()}, set(), 1),
    ("not consumed, not read", {"consumed": set(), "not_consumed": {"tags"}}, set(), 0),
    # The direction that rots: a wrong REASON, which reads as considered.
    ("not consumed, but read", {"consumed": set(), "not_consumed": {"tags"}}, {"tags"}, 1),
    # Claimed both ways WITHOUT a verb: refused, because accepting it would
    # silence both read-checks for that attribute. Measured, the count is TWO
    # either way, and that is the property rather than an accident: the
    # attribute stays in both read-checks, and the two claims contradict each
    # other, so exactly one of them must also fire whichever way `keys` falls --
    # "consumed but not read" when it is unread, "not consumed but is read"
    # when it is read. Both key states are pinned so a change that silences one
    # arm is visible.
    (
        "both ways, no verb, unread",
        {"consumed": {"serial"}, "not_consumed": {"serial"}, "_rowtext": {"serial": "Dropped."}},
        set(),
        2,
    ),
    (
        "both ways, no verb, read",
        {"consumed": {"serial"}, "not_consumed": {"serial"}, "_rowtext": {"serial": "Dropped."}},
        {"serial"},
        2,
    ),
    # Claimed both ways WITH a verb: legitimate, and neither read-check applies.
    (
        "both ways, verb named",
        {
            "consumed": {"terraform-version"},
            "not_consumed": {"terraform-version"},
            "_rowtext": {"terraform-version": "Dropped on `POST`, consumed on `PATCH`"},
        },
        set(),
        0,
    ),
]


def catalogues(
    doc: str, headings: dict[str, object] | None = None
) -> dict[str, dict[str, set[str]]]:
    """Heading -> {"consumed": {...}, "not_consumed": {...}}.

    A table's class comes from the nearest preceding prose marker, because that
    is what a reader goes by too. `headings` exists so the fixtures can use a
    synthetic heading rather than depending on what `HANDLERS` happens to hold.
    """
    wanted = HANDLERS if headings is None else headings
    out: dict[str, dict[str, set[str]]] = {}
    blocks = re.split(r"^### ", doc, flags=re.M)[1:]
    for b in blocks:
        heading = b.split("\n", 1)[0].strip()
        if heading not in wanted:
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


def claim_problems(
    heading: str,
    claims: dict[str, set[str]],
    keys: set[str],
    handler_files: list[str],
) -> tuple[list[str], int, int]:
    """Both read-checks for one catalogue, plus the claimed-both-ways refusal.

    Pure, over the derived sets, because this is the half no floor can see: the
    counts below prove the claims are non-empty and say nothing about whether
    the comparison discriminates. `(problems, n_consumed_checked,
    n_not_consumed_checked)`.
    """
    problems: list[str] = []
    # An attribute in BOTH tables is the asymmetric case -- consumed on one
    # verb and dropped on another (`terraform-version` on workspaces). Both
    # claims are true, so neither read-check applies; it is derived from the
    # document rather than exempted by hand.
    rowtext: dict[str, str] = claims.get("_rowtext", {})  # type: ignore[assignment]
    both = set()
    for a in sorted(claims["consumed"] & claims["not_consumed"]):
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

    n_consumed = n_not = 0
    for a in sorted(claims["consumed"] - both):
        n_consumed += 1
        if a not in keys:
            problems.append(
                f"[{heading}] CLAIMED CONSUMED BUT NOT READ: `{a}`\n"
                f"      No handler in {','.join(handler_files)} reads it. Either the "
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
    return problems, n_consumed, n_not


def self_test() -> list[str]:
    """Pin the table reader, the key reader and the decision, before any scan.

    The three floors prove the claims and the catalogues are non-empty and say
    nothing about whether any predicate discriminates (#2425 §2.7). A table
    reader that quietly stops seeing one marker spelling still counts twenty-odd
    claims from the others; a key reader that drops one AST shape still finds
    plenty. These fixtures are what pin the individual forms.
    """
    out: list[str] = []
    for text, want_consumed, want_not in SELF_TEST_TABLES:
        cats = catalogues(text, {_FIXTURE_HEADING: {}})
        got = cats.get(_FIXTURE_HEADING, {"consumed": set(), "not_consumed": set()})
        if sorted(got["consumed"]) != sorted(want_consumed) or sorted(
            got["not_consumed"]
        ) != sorted(want_not):
            out.append(
                f"the table reader no longer reads {text!r} the way it must: expected "
                f"consumed={sorted(want_consumed)} not_consumed={sorted(want_not)}, read "
                f"consumed={sorted(got['consumed'])} not_consumed={sorted(got['not_consumed'])}."
            )
    for row, want_verb in SELF_TEST_ASYMMETRY:
        if bool(ASYMMETRY.search(row)) is not want_verb:
            out.append(
                f"ASYMMETRY no longer decides {row!r} the way it must: expected "
                f"names-a-verb={want_verb}."
            )
    for src, fns, want_keys, want_missing in SELF_TEST_READS:
        got_keys, got_missing = read_keys_from_source(src, list(fns))
        if sorted(got_keys) != sorted(want_keys) or sorted(got_missing) != sorted(want_missing):
            out.append(
                f"the key reader no longer reads {src!r} for {list(fns)} the way it must: "
                f"expected keys={sorted(want_keys)} missing={sorted(want_missing)}, read "
                f"keys={sorted(got_keys)} missing={sorted(got_missing)}."
            )
    for label, claims, keys, want_n in SELF_TEST_DECISION:
        got_problems, _, _ = claim_problems("fixture", dict(claims), set(keys), ["x.py"])
        if len(got_problems) != want_n:
            out.append(
                f"the claim decision for {label} reported {len(got_problems)} problem(s), "
                f"not {want_n}: {got_problems}"
            )
    return out


def main() -> int:
    # The gate's own health first: a reader or a comparison that has stopped
    # discriminating makes every claim below meaningless.
    broken = self_test()
    if broken:
        print("the CLI-surface-body-claims gate cannot do its job:")
        for problem in broken:
            print(f"  - {problem}")
        return 1

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

        got_problems, c, n = claim_problems(heading, claims, keys, list(HANDLERS[heading]))
        problems += got_problems
        n_consumed += c
        n_not += n

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
        f"{n_not} not-consumed claim(s) all absent. "
        f"{len(SELF_TEST_TABLES) + len(SELF_TEST_ASYMMETRY) + len(SELF_TEST_READS) + len(SELF_TEST_DECISION)} "
        "reader and decision fixtures hold."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
