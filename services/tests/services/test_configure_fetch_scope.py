"""The Ansible config root is part of the VCS fetch scope (#2117).

A workspace with a narrow `working_directory` -- the monorepo case the
narrowing exists to serve -- and a configure operation used to build a
configuration tarball containing no playbook, no `roles/`, no `group_vars/`
and no `host_vars/`, because the fetch scope was `working_directory` union
`trigger_prefixes` and nothing else.

Two kinds of test here, and the second is the point:

* **Behaviour**, on `git_fetch.workspace_fetch_paths`. Every one is a
  *contrast* -- both states instantiated -- because the property is a
  relationship between a configure-enabled workspace and a configure-disabled
  one, and a single-state fixture passes whichever way the rule is written,
  including not written at all.

* **Structure.** The defect was not that someone wrote the union wrongly; it
  was that it was written TWICE, in `vcs_poller._compute_paths_unions` and in
  `vcs_config_service`, with the latter asserting in a comment that the two
  agreed. They did, until a term was added to neither. So these gates are
  about there being one producer, and about each call site handing it the
  right values -- derived from the source, with no list of call sites to keep
  in step by hand.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from terrapod.services import git_fetch, vcs_config_service, vcs_poller

# The shared rule, under the name every call site uses.
RULE = "workspace_fetch_paths"

# The four values a fetch scope is computed from. Named here so a fifth
# cannot be added to the rule without this file saying what it means.
SCOPE_INPUTS = ("working_directory", "trigger_prefixes", "configure_enabled", "config_directory")


def _src(mod) -> str:
    return pathlib.Path(mod.__file__).read_text()


def _tree(mod) -> ast.Module:
    return ast.parse(_src(mod))


# ───────────────────────── behaviour, as contrasts ─────────────────────────


class TestTheConfigRootIsFetchedOnlyWhenThereIsAConfigureOperation:
    """`configure_enabled` decides, not the presence of a path (#1966).

    `config_directory` is an ordinary path with `""` meaning the root; it
    carries no capability meaning of its own. So a workspace that names an
    Ansible root and has configure switched off must keep its narrowing rather
    than pay for a fetch nothing will read.
    """

    def test_an_enabled_configure_adds_its_root_and_a_disabled_one_does_not(self):
        enabled = git_fetch.workspace_fetch_paths(
            working_directory="terraform",
            trigger_prefixes=None,
            configure_enabled=True,
            config_directory="ansible",
        )
        disabled = git_fetch.workspace_fetch_paths(
            working_directory="terraform",
            trigger_prefixes=None,
            configure_enabled=False,
            config_directory="ansible",
        )
        # The contrast IS the assertion: one path differs, and it is the root.
        assert enabled == ["ansible", "terraform"]
        assert disabled == ["terraform"]

    def test_the_root_is_absent_from_the_scope_of_a_plain_terraform_workspace(self):
        # The default shape (#1966 defaults configure OFF), which must be
        # byte-identical to the pre-#2117 behaviour for every existing
        # deployment: this is the regression half of the contrast.
        assert git_fetch.workspace_fetch_paths(
            working_directory="terraform", trigger_prefixes=["modules"]
        ) == ["modules", "terraform"]


class TestAConfigRootAtTheRepositoryRootForcesTheWholeRepo:
    """`""` is the root, so there is nothing left to narrow away.

    The asymmetry with `working_directory` is the subtle part and the reason
    this class exists: for `working_directory`, `""` contributes no path and
    the workspace stays narrowed by its `trigger_prefixes`. Treating the
    config root the same way -- appending nothing -- would leave the scope at
    `["terraform"]` and exclude the playbook, which is #2117 in a new costume
    and passes any test that only checks the enabled/disabled contrast above.
    """

    def test_an_empty_root_with_a_narrow_working_directory_widens_to_everything(self):
        narrowed = git_fetch.workspace_fetch_paths(
            working_directory="terraform",
            trigger_prefixes=None,
            configure_enabled=True,
            config_directory="ansible",
        )
        whole_repo = git_fetch.workspace_fetch_paths(
            working_directory="terraform",
            trigger_prefixes=None,
            configure_enabled=True,
            config_directory="",
        )
        assert narrowed == ["ansible", "terraform"]
        assert whole_repo == [], "an enabled configure rooted at the repo root cannot be narrowed"

    def test_an_empty_root_is_inert_while_configure_is_disabled(self):
        # The same `""` that forces a whole-repo fetch above must cost nothing
        # here, or declaring a root and leaving configure off would silently
        # un-narrow a monorepo workspace.
        assert git_fetch.workspace_fetch_paths(
            working_directory="terraform",
            trigger_prefixes=None,
            configure_enabled=False,
            config_directory="",
        ) == ["terraform"]


class TestTheScopeIsStillCollapsedAndDeduplicated:
    def test_a_prefix_beneath_the_config_root_is_subsumed_by_it(self):
        # `ansible` subsumes `ansible/roles`, so sparse-checkout is asked for
        # the shorter path only -- and the config root must take part in that
        # collapse rather than being appended after it.
        assert git_fetch.workspace_fetch_paths(
            working_directory="tf",
            trigger_prefixes=["ansible/roles"],
            configure_enabled=True,
            config_directory="ansible",
        ) == ["ansible", "tf"]

    def test_a_configure_only_workspace_is_narrowed_to_its_root_alone(self):
        # No Terraform to fetch, so the monorepo narrowing still applies --
        # the feature is not "configure means whole repo".
        assert git_fetch.workspace_fetch_paths(
            working_directory="",
            trigger_prefixes=None,
            configure_enabled=True,
            config_directory="ansible",
        ) == ["ansible"]

    @pytest.mark.parametrize("raw", ["ansible", "/ansible", "ansible/", " ansible "])
    def test_a_root_is_normalised_however_it_was_stored(self, raw):
        # `validate_config_directory` already refuses the slashed and
        # whitespaced forms on the write path, so this is belt-and-braces --
        # but the rule must not depend on that validator to be correct, since
        # a row could predate it or arrive from a restore.
        assert git_fetch.workspace_fetch_paths(
            working_directory="",
            trigger_prefixes=None,
            configure_enabled=True,
            config_directory=raw,
        ) == ["ansible"]


# ────────────────────────── structure: one producer ─────────────────────────


class TestNothingRebuildsThePlanApplyNarrowing:
    """Derived from the source, so a third fetch site is covered automatically.

    The invariant is deliberately narrower than "every archive fetch consults
    the rule", because that is not true and should not be: `inventory_resolve`
    fetches the config root **alone**, since resolving an inventory never reads
    Terraform. A deliberately different scope is fine. What is not fine is a
    second opinion about the SAME scope, which is what `vcs_config_service`
    held -- with a comment asserting it agreed with the poller's.

    Writing this found that `inventory_resolve` passes `paths` positionally,
    which an earlier version of the gate could not see while being named
    "every caller". Both forms are read now.
    """

    #: What a fetch scope may not be rebuilt from outside the rule.
    NARROWING_COLUMNS = ("working_directory", "trigger_prefixes")

    def _paths_arg(self, call: ast.Call) -> ast.expr | None:
        """The `paths` argument of a `get_or_fetch` call, keyword or positional.

        `VCSArchiveCache.get_or_fetch(conn, owner, repo, sha, paths)` -- so
        positionally it is the fifth. Reading only the keyword form is how a
        call site stays invisible to its own gate.
        """
        for k in call.keywords:
            if k.arg == "paths":
                return k.value
        return call.args[4] if len(call.args) >= 5 else None

    def test_no_archive_fetch_rebuilds_the_scope_from_the_narrowing_columns(self):
        root = pathlib.Path(git_fetch.__file__).parent
        offenders: list[str] = []
        checked = 0

        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                # Every name assigned in this function, mapped to whether its
                # value mentions a narrowing column, and whether it came from
                # the rule.
                assigned: dict[str, str] = {}
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Assign):
                        continue
                    dumped = ast.dump(node.value)
                    verdict = (
                        "rule"
                        if RULE in dumped
                        else (
                            "narrowing"
                            if any(c in dumped for c in self.NARROWING_COLUMNS)
                            else "other"
                        )
                    )
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            # A later plain assignment must not launder an
                            # earlier narrowing-derived one.
                            if assigned.get(t.id) == "narrowing" and verdict == "other":
                                continue
                            assigned[t.id] = verdict

                # `x = []` followed by `x.append(ws.working_directory)` is an
                # assignment that mentions no column at all, so the pass above
                # reads it as innocent. That is exactly the pre-#2117 shape, and
                # a gate that misses it is weaker than its own name -- which is
                # the defect class this file exists for.
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("append", "extend")
                        and isinstance(node.func.value, ast.Name)
                    ):
                        dumped = ast.dump(ast.Tuple(elts=list(node.args), ctx=ast.Load()))
                        if any(c in dumped for c in self.NARROWING_COLUMNS):
                            assigned[node.func.value.id] = "narrowing"

                for call in ast.walk(fn):
                    if not isinstance(call, ast.Call):
                        continue
                    if not (
                        isinstance(call.func, ast.Attribute) and call.func.attr == "get_or_fetch"
                    ):
                        continue
                    arg = self._paths_arg(call)
                    if arg is None:
                        continue
                    checked += 1
                    # `fetch_paths or None` is the whole-repo-sentinel idiom.
                    if isinstance(arg, ast.BoolOp):
                        arg = arg.values[0]
                    if isinstance(arg, ast.Constant) and arg.value is None:
                        continue  # deliberately the whole repository
                    if isinstance(arg, ast.Name):
                        if arg.id in params:
                            continue  # decided by this function's caller
                        if assigned.get(arg.id) in ("rule", "other", None):
                            continue
                    offenders.append(f"{path.name}:{call.lineno} paths={ast.unparse(arg)}")

        assert checked >= 4, (
            f"only found {checked} archive-fetch call sites -- this gate has stopped "
            "looking at the thing it is about (renamed helper? positional form missed?)"
        )
        assert not offenders, (
            "these rebuild the plan/apply fetch narrowing instead of calling "
            f"`git_fetch.{RULE}`: {offenders}"
        )

    def test_only_the_rule_assembles_a_path_list_from_the_narrowing_columns(self):
        """The pre-#2117 shape, wherever it might come back.

        Scoped to the whole service package rather than to the two call sites
        that happened to have it, because the next one will be somewhere else
        -- and it will come back without the config root, exactly as before.
        """
        root = pathlib.Path(git_fetch.__file__).parent
        rule_module = pathlib.Path(git_fetch.__file__).name
        offenders: list[str] = []

        for path in sorted(root.glob("*.py")):
            if path.name == rule_module:
                continue  # the rule is allowed to be the rule
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if ".append(" not in line and ".extend(" not in line:
                    continue
                if any(c in line for c in self.NARROWING_COLUMNS):
                    offenders.append(f"{path.name}:{lineno} {line.strip()}")

        assert not offenders, (
            f"a fetch path list is being assembled outside `{rule_module}`: {offenders}"
        )


class TestEachCallSiteHandsTheRuleTheRightValues:
    """A keyword existing is not its value being right.

    Both gates follow the link from the keyword to what it resolves to, rather
    than checking the keyword is present -- `configure_enabled=False` would
    satisfy the latter while restoring the whole defect.
    """

    def _rule_call(self, mod) -> ast.Call:
        calls = [
            n for n in ast.walk(_tree(mod)) if isinstance(n, ast.Call) and RULE in ast.dump(n.func)
        ]
        assert len(calls) == 1, (
            f"expected exactly one `{RULE}` call in "
            f"{pathlib.Path(mod.__file__).name}, found {len(calls)}"
        )
        return calls[0]

    def test_the_single_workspace_path_passes_the_workspace_s_own_columns(self):
        call = self._rule_call(vcs_config_service)
        passed = {
            k.arg: k.value
            for k in call.keywords
            if isinstance(k.value, ast.Attribute) and isinstance(k.value.value, ast.Name)
        }
        assert set(passed) == set(SCOPE_INPUTS), (
            f"vcs_config_service passes {sorted(passed)}; expected all of {list(SCOPE_INPUTS)} "
            "as attributes of the workspace"
        )
        for name in SCOPE_INPUTS:
            # The keyword must carry the column OF THE SAME NAME -- a
            # transposition (config_directory=ws.working_directory) is the
            # failure this follows the link to catch.
            assert passed[name].attr == name, (
                f"vcs_config_service passes {name}=ws.{passed[name].attr}, not ws.{name}"
            )

    def test_the_poll_path_s_row_unpacking_agrees_with_its_select(self):
        """The transposition risk, which has bitten this file before.

        `_poll_workspace`'s `(cache, meta, fetch_paths)` were once passed
        positionally as `(cache, fetch_paths, meta)`, so a list landed in the
        metadata-cache slot and every poll died. The same hazard sits in this
        function: the SELECT's column order and the row unpack's name order
        are two hand-written sequences that must agree, and a mismatch between
        `configure_enabled` and `config_directory` would silently swap a
        boolean for a path -- which is falsy-but-present, so the config root
        would be dropped from every scope with no error anywhere.
        """
        fn = next(
            n
            for n in ast.walk(_tree(vcs_poller))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_compute_paths_unions"
        )

        # The Workspace/VCSConnection columns the select asks for, in order.
        select_call = next(
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "select"
        )
        columns = [a.attr for a in select_call.args if isinstance(a, ast.Attribute)]

        # The names the row is unpacked into, in order.
        loop = next(
            n for n in ast.walk(fn) if isinstance(n, ast.For) and isinstance(n.target, ast.Tuple)
        )
        names = [e.id for e in loop.target.elts if isinstance(e, ast.Name)]

        assert len(columns) == len(names), (
            f"the select asks for {len(columns)} columns {columns} and the row is unpacked "
            f"into {len(names)} names {names} -- they cannot be in step"
        )

        # Which unpacked name carries each scope input, per the select order.
        carrier = dict(zip(columns, names, strict=True))
        call = self._rule_call(vcs_poller)
        passed = {k.arg: k.value.id for k in call.keywords if isinstance(k.value, ast.Name)}
        assert set(passed) == set(SCOPE_INPUTS), (
            f"_compute_paths_unions passes {sorted(passed)}; expected {list(SCOPE_INPUTS)}"
        )
        for name in SCOPE_INPUTS:
            assert passed[name] == carrier[name], (
                f"{name}= is handed `{passed[name]}`, which the select binds to "
                f"`{next(c for c, v in carrier.items() if v == passed[name])}`, not `{name}`"
            )
