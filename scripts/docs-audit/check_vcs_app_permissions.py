#!/usr/bin/env python3
"""The GitHub App permissions we ask for are the ones we use (#2707).

`docs/vcs-integration.md` tells an operator which repository permissions to
grant a GitHub App. That list is the App's blast radius, and it is prose: nothing
held it to the endpoints the code calls, in either direction.

Both directions had a real failure waiting.

**Asking for a permission nothing uses.** The list carried **Checks: Read &
write** -- "Post check runs on commits" -- from the day it was written until
#2707, and `grep -rn 'check-runs' services/terrapod` answered **0**. Terrapod
reports back with commit statuses and has never created a check run, so every
operator following the page granted a write permission over check runs for
nothing. Least privilege is not a nice-to-have on a credential that spans an
organization's repositories, and a documented *purpose* that the code does not
perform is worse than a bare over-grant: it reads as considered, so nobody
re-checks it.

**Calling an endpoint the list does not cover.** The mirror defect has no
symptom here at all -- it has one in the operator's installation, as a 403 from
a path they were never told to permit, on whichever feature happens to touch it
first.

So this gate asks both questions of the same ledger:

* a permission the table asks for, with no call in the code that needs it, fails;
* a call in the code whose permission the table does not ask for, fails.

**The evidence is read from the AST, not from the file text.** A path in a
comment or a docstring is prose *about* a call, not a call -- and this module's
own subject is a permission that was documented and never exercised, so counting
the sentence that describes it would make the gate agree with the defect. The
reader therefore collects string literals only, which excludes comments by
construction (they are not in the AST) and excludes docstrings explicitly.

**Read as TEXT and parsed, never imported.** Importing `terrapod.services`
drags in the models and so SQLAlchemy and the rest of the API's dependency
closure, which the job running this installs none of -- the recorded way a
docs-audit gate passes locally and fails in CI with `ModuleNotFoundError`.

**What it cannot see**, stated so nobody reads more into a pass than is there:
whether the *access level* in the table (read-only against read & write) matches
what the call needs. That is a property of each endpoint's verb and of GitHub's
own mapping, neither of which is derivable from this repository.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The doc that tells an operator what to grant.
DOC = Path("docs/vcs-integration.md")

#: The GitHub App's call surface, derived rather than listed: the module that
#: mints an installation token is the one that calls the App's endpoints.
SURFACE_DIR = Path("services/terrapod/services")
SURFACE_MARKER = "_api_call_token"


@dataclass(frozen=True)
class Permission:
    """A GitHub App repository permission, and what in the code would need it.

    `endpoints` are path fragments as they appear in a URL literal. One
    fragment is enough: the question is whether ANY call needs the permission,
    not how many do. They are kept distinct across entries so a fragment cannot
    vouch for two permissions at once.
    """

    endpoints: tuple[str, ...]
    #: Why this permission, in the operator's words rather than the endpoint's.
    purpose: str


#: The ledger. Every permission GitHub would make an operator grant for anything
#: this code does, whether or not the doc currently asks for it -- a permission
#: absent from here is one this gate cannot reason about, so adding a call on a
#: new endpoint family means adding its row.
PERMISSIONS: dict[str, Permission] = {
    "Contents": Permission(
        endpoints=("/tarball/", "/compare/", "/git/trees/", "/tags", "/branches"),
        purpose="download archives, compare commits, list branches and tags",
    ),
    "Metadata": Permission(
        endpoints=("/collaborators/",),
        purpose="the collaborator-permission read behind the comment commands' push-access check",
    ),
    "Checks": Permission(
        endpoints=("/check-runs", "/check-suites"),
        purpose="create or read check runs",
    ),
    "Commit statuses": Permission(
        endpoints=("/statuses/",),
        purpose="post the plan/policy/apply statuses",
    ),
    "Pull requests": Permission(
        endpoints=("/pulls",),
        purpose="read pull requests, their mergeability and their reviews; merge them",
    ),
    "Issues": Permission(
        endpoints=("/issues/",),
        purpose="pull-request comments and reactions, which GitHub serves through the Issues API",
    ),
}

# Three bounds against a reader that could stop matching and leave this gate
# passing on an empty set. Each is a LOWER bound on purpose, because in all
# three the growing direction is covered by the checks themselves rather than by
# a count -- an equality would fail a correct addition and teach people to edit
# the gate instead of reviewing the grant.

# A THIRD permission table is in scope the moment it is read: every row of every
# table is held to the ledger, and an unledgered name fails by name. So the
# other direction is closed already and a lower bound is what is meant.
MIN_TABLES = 2

# A lower bound, deliberately: a SIXTH documented permission is checked rather
# than counted. Either the ledger knows it, and its endpoints must be exercised,
# or it does not, and the row fails. So the other direction is already closed.
MIN_DOCUMENTED = 5

# 550 today. More string literals in the surface module says nothing about
# permissions either way, so the other direction is meaningless here: this is a
# floor against the AST reader breaking and nothing else.
MIN_LITERALS = 40


# ── readers ──────────────────────────────────────────────────────────────


def permission_tables(text: str) -> list[list[str]]:
    """Every markdown table in `text` whose first column is `Permission`.

    One list of names per table, because the two tables in the doc are not the
    same list: the setup table is the whole grant and the status-reporting one
    names the subset that section needs. So "every row is exercised" is asked of
    each table and "every exercised permission is asked for" of their union.
    """
    tables: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            current = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            current = None
            continue
        first = cells[0].strip().strip("*").strip("`").strip()
        if current is None:
            if first == "Permission":
                current = []
                tables.append(current)
            continue
        # The `|---|---|` separator under the header.
        if set(first) <= {"-", ":"} and first:
            continue
        if first:
            current.append(first)
    return [t for t in tables if t]


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()` of every string Constant that is a docstring.

    Prose about a call is not a call. This module's whole subject is a
    permission that was documented and never used, so a reader that counted the
    sentence describing an endpoint would agree with the defect it exists to
    find.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def url_literals(source: str, where: str) -> list[str]:
    """Every string literal in `source`, docstrings excluded.

    An f-string arrives as a `JoinedStr`, so its literal spans are returned
    individually: `f"{api}/repos/{owner}/{repo}/statuses/{sha}"` yields
    `/statuses/` as its own span. Returning the spans separately rather than
    joined is deliberate -- a fragment must not be able to match across a
    placeholder and claim a path nobody writes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SystemExit(f"FAIL — {where} does not parse ({exc})") from exc

    skip = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
        ):
            out.append(node.value)
    return out


def surface_files() -> list[Path]:
    """The modules that call the GitHub App's endpoints, derived from the tree."""
    return sorted(p for p in (ROOT / SURFACE_DIR).glob("*.py") if SURFACE_MARKER in p.read_text())


def exercised(literals: list[str]) -> dict[str, list[str]]:
    """Permission name -> the endpoint fragments found. Absent means no evidence."""
    out: dict[str, list[str]] = {}
    for name, perm in PERMISSIONS.items():
        hits = [e for e in perm.endpoints if any(e in lit for lit in literals)]
        if hits:
            out[name] = hits
    return out


# ── self-test ────────────────────────────────────────────────────────────

_TABLE_DOC = """
Prose before.

   | Permission | Access | Purpose |
   |---|---|---|
   | **Contents** | Read-only | Archives |
   | Metadata | Read-only | Metadata |

| Other | Access |
|---|---|
| **Ignored** | Read |

| Permission | Access |
|---|---|
| **Commit statuses** | Read & write |
"""

#: (label, markdown, expected tables).
SELF_TEST_TABLES: list[tuple[str, str, list[list[str]]]] = [
    (
        "two permission tables, one unrelated",
        _TABLE_DOC,
        [["Contents", "Metadata"], ["Commit statuses"]],
    ),
    ("no permission table at all", "| Thing | Value |\n|---|---|\n| a | b |\n", []),
    ("a blank line ends the table", "| Permission |\n|---|\n| **A** |\n\n| **B** |\n", [["A"]]),
]

_SOURCE_WITH_DOCSTRING_ONLY = '''
"""A module that only TALKS about /check-runs and /statuses/ in prose."""


def f(conn):
    """Calls GET /repos/{o}/{r}/check-runs, one day."""
    # /check-runs in a comment is not a call either.
    return None
'''

_SOURCE_WITH_CALLS = '''
"""Module docstring naming /check-runs, which must not count."""


def post(api_url, owner, repo, sha):
    return _req(f"{api_url}/repos/{owner}/{repo}/statuses/{sha}")


def perm(conn, owner, repo, login):
    return _req(
        f"{_api_url(conn)}/repos/{owner}/{repo}"
        f"/collaborators/{login}/permission"
    )
'''

#: (label, python source, fragments that must be present, fragments that must not).
SELF_TEST_SOURCE: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "prose only",
        _SOURCE_WITH_DOCSTRING_ONLY,
        (),
        ("/check-runs", "/statuses/"),
    ),
    (
        "real calls, prose naming an endpoint nobody calls",
        _SOURCE_WITH_CALLS,
        ("/statuses/", "/collaborators/"),
        ("/check-runs",),
    ),
]


def self_test() -> list[str]:
    """Pin both readers against literals, before either real file is opened."""
    out: list[str] = []
    for label, text, expected in SELF_TEST_TABLES:
        got = permission_tables(text)
        if got != expected:
            out.append(
                f"the table reader no longer handles {label!r} the way it must: "
                f"expected {expected}, got {got}. Widen the matcher rather than the "
                "fixture -- a table spelling it cannot see is a grant nobody checks."
            )
    for label, source, present, absent in SELF_TEST_SOURCE:
        literals = url_literals(source, f"<fixture {label}>")
        for frag in present:
            if not any(frag in lit for lit in literals):
                out.append(
                    f"the literal reader no longer finds {frag!r} in {label!r}. A call "
                    "it cannot see reads as a permission nobody needs, which is the "
                    "direction that DELETES a grant an installation depends on."
                )
        for frag in absent:
            if any(frag in lit for lit in literals):
                out.append(
                    f"the literal reader now counts {frag!r} in {label!r}, where it "
                    "appears only in prose. A comment or docstring describing an "
                    "endpoint would then vouch for the permission, which is exactly "
                    "how a documented-but-unused permission survives."
                )
    overlapping = [
        (a, b, sorted(set(PERMISSIONS[a].endpoints) & set(PERMISSIONS[b].endpoints)))
        for i, a in enumerate(PERMISSIONS)
        for b in list(PERMISSIONS)[i + 1 :]
        if set(PERMISSIONS[a].endpoints) & set(PERMISSIONS[b].endpoints)
    ]
    for a, b, shared in overlapping:
        out.append(
            f"{a} and {b} both claim {shared}; a fragment that vouches for two "
            "permissions cannot tell them apart."
        )
    for name, perm in PERMISSIONS.items():
        if not perm.endpoints:
            out.append(f"{name} names no endpoint, so this gate can say nothing about it")
        if not perm.purpose.strip():
            out.append(f"{name} carries no purpose, so a reported row cannot be acted on")
    return out


# ── main ─────────────────────────────────────────────────────────────────


def main() -> int:
    blockers = self_test()
    if blockers:
        print("check_vcs_app_permissions cannot do its job:\n")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 1

    doc = ROOT / DOC
    if not doc.is_file():
        print(f"FAIL — {DOC} does not exist")
        return 1

    tables = permission_tables(doc.read_text())
    if len(tables) < MIN_TABLES:
        print(
            f"FAIL — found {len(tables)} permission table(s) in {DOC}, below the floor "
            f"of {MIN_TABLES}. The reader has probably stopped matching; fix it rather "
            "than lowering the floor."
        )
        return 1

    documented: set[str] = {name for table in tables for name in table}
    if len(documented) < MIN_DOCUMENTED:
        print(
            f"FAIL — read {len(documented)} distinct permission(s) out of {DOC}, below "
            f"the floor of {MIN_DOCUMENTED}."
        )
        return 1

    files = surface_files()
    if not files:
        print(
            f"FAIL — no module under {SURFACE_DIR} contains {SURFACE_MARKER!r}, so this "
            "gate found no GitHub App call surface to read. If the token mint moved, "
            "move SURFACE_MARKER with it."
        )
        return 1

    literals: list[str] = []
    for f in files:
        literals.extend(url_literals(f.read_text(), str(f.relative_to(ROOT))))
    if len(literals) < MIN_LITERALS:
        print(
            f"FAIL — read {len(literals)} string literal(s) out of {len(files)} module(s), "
            f"below the floor of {MIN_LITERALS}."
        )
        return 1

    used = exercised(literals)
    problems: list[str] = []

    for table, names in enumerate(tables, 1):
        for name in names:
            if name not in PERMISSIONS:
                problems.append(
                    f"{DOC} table {table} asks for {name!r}, which this gate's ledger "
                    "does not know. Add it to PERMISSIONS with the endpoints that need "
                    "it, or remove the row -- an unledgered grant is unchecked."
                )
            elif name not in used:
                problems.append(
                    f"{DOC} table {table} asks an operator to grant {name!r} "
                    f"({PERMISSIONS[name].purpose}), and no call in "
                    f"{', '.join(str(f.relative_to(ROOT)) for f in files)} needs it "
                    f"(looked for {', '.join(PERMISSIONS[name].endpoints)}). Either use "
                    "it or stop asking for it."
                )

    for name, hits in sorted(used.items()):
        if name not in documented:
            problems.append(
                f"the code calls {', '.join(hits)}, which needs {name!r}, and no "
                f"permission table in {DOC} asks for it. An installation that followed "
                "the page gets a 403 from that call."
            )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the documented GitHub App permissions:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    unused = sorted(set(PERMISSIONS) - set(used))
    print(
        f"PASS — all {len(documented)} documented permission(s) across {len(tables)} "
        f"table(s) are exercised by {len(files)} module(s), and every exercised "
        f"permission is documented."
    )
    if unused:
        print(f"       Ledgered and deliberately not asked for: {', '.join(unused)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
