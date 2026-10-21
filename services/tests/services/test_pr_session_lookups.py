"""Every `PRSession` lookup narrowed by PR number is also narrowed by repo (#2199).

`uq_pr_session` is `(vcs_connection_id, repo, pr_number)` and a VCS connection is
one app installation covering a whole organisation, so `(connection, pr_number)`
is **not** a unique key: two open pull requests numbered 111, in two repositories
of one organisation, match it equally.

Two separate modules resolved a session that way, and the two produced opposite
symptoms from the same mistake -- which is why neither was found from the other:

  * `vcs_status_comment.refresh_for_run` took `.first()` with no `ORDER BY`, so it
    silently picked one. Every run on the losing PR then enqueued its refresh
    against the winning PR's session id, and the losing comment froze for ever on
    the snapshot the poller enqueued at run creation -- the only enqueue that ever
    carried its real id. Silent: both sessions exist, both are open, and the
    handler happily refreshes the one it is handed.
  * `vcs_auto_merge.handle_vcs_apply_completed` took `scalar_one_or_none()`, which
    *raises* on two rows. The scheduler caught it, logged `Trigger handler failed`
    and counted it, and auto-merge never fired for either PR. Loud, and a feature
    outage rather than a stale comment.

This is derived from the tree rather than from a list of known call sites, because
a list is the thing that would have been written in 2024 and not revisited. It is
also per-FUNCTION rather than per-statement, deliberately: `refresh_for_run`
assembles its predicate into a local `where = [...]` before passing it to
`select()`, so a statement-scoped reader sees only `vcs_connection_id` and reports
the defective site as clean. Measured: every function in the tree holds exactly
one `select(PRSession)`, so per-function is exact here and not an approximation --
`test_one_select_per_function` pins that assumption so this stops being exact
loudly rather than quietly.

What is deliberately NOT required:

  * a lookup keyed on `merge_commit_sha` (`_attribute_merge_commit`, and
    `refresh_for_run`'s branch-run path) -- a merge commit's SHA is unique within
    a repository by git's own construction, so the repo adds nothing to its
    uniqueness. Both carry it anyway, and that is fine; the gate just does not
    insist.
  * a deliberately PLURAL lookup (`_poll_pr_comments`,
    `_reconcile_closed_pr_sessions`) -- those want every session for a repo and
    never mention `pr_number`, so they are outside the rule by construction.
"""

import ast
import pathlib

SERVICES = pathlib.Path(__file__).resolve().parents[2] / "terrapod"

#: A floor, so a rename that stops the matcher matching fails here instead of
#: passing on an empty set. Seven at the time of writing; the gate is about the
#: rule, not the count, so this only has to be a number below it.
MIN_LOOKUPS = 6


def _pr_session_lookups() -> list[tuple[str, int, str, set[str]]]:
    """Every function containing a `select(PRSession)`, with the columns it names."""
    found: list[tuple[str, int, str, set[str]]] = []
    for path in sorted(SERVICES.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            selects = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "select"
                and n.args
                and isinstance(n.args[0], ast.Name)
                and n.args[0].id == "PRSession"
            ]
            if not selects:
                continue
            cols = {
                n.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "PRSession"
            }
            found.append((path.name, fn.lineno, fn.name, cols))
    return found


class TestEveryPRSessionLookupIsRepoScoped:
    def test_the_matcher_finds_the_lookups_at_all(self):
        """A floor: a rename must fail here rather than pass on nothing."""
        lookups = _pr_session_lookups()
        assert len(lookups) >= MIN_LOOKUPS, (
            f"only found {len(lookups)} PRSession lookups, expected at least "
            f"{MIN_LOOKUPS} -- has `select(PRSession)` been renamed or wrapped? "
            "A gate that matches nothing passes silently."
        )

    def test_a_lookup_by_pr_number_also_filters_the_repo(self):
        offenders = [
            f"{name}:{line} {fn} filters {sorted(cols)}"
            for name, line, fn, cols in _pr_session_lookups()
            if "pr_number" in cols and "repo" not in cols
        ]
        assert not offenders, (
            "a PRSession lookup narrowed by pr_number must also be narrowed by "
            "repo -- `uq_pr_session` is (vcs_connection_id, repo, pr_number), and "
            "one connection covers a whole organisation, so without the repo this "
            "matches every same-numbered open PR in it (#2199):\n  "
            + "\n  ".join(offenders)
            + "\n\nDerive it with `vcs_provider.repo_path(ws.vcs_connection, "
            "ws.vcs_repo_url)`, which is the form `PRSession.repo` stores."
        )

    def test_one_select_per_function(self):
        """The assumption that makes a per-function reading exact.

        If a function ever holds two PRSession queries -- one by pr_number and one
        by repo -- the union of their columns satisfies the rule above while
        neither query does. That is a false negative, so it has to fail here
        rather than weaken the gate silently.
        """
        multi = []
        for path in sorted(SERVICES.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                selects = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "select"
                    and n.args
                    and isinstance(n.args[0], ast.Name)
                    and n.args[0].id == "PRSession"
                ]
                if len(selects) > 1:
                    multi.append(f"{path.name}:{fn.lineno} {fn.name} has {len(selects)}")
        assert not multi, (
            "a function now holds more than one `select(PRSession)`, so reading "
            "the columns per function is no longer exact and "
            "`test_a_lookup_by_pr_number_also_filters_the_repo` can pass on the "
            "union of two different queries. Split the function, or make that gate "
            "statement-scoped with the local-variable predicate resolved:\n  " + "\n  ".join(multi)
        )
