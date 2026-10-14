"""One resolver for a workspace's ansible-core version, and only one (#2035).

#1407 point 10 records that inventory resolution shells out to
`ansible-inventory` instead of importing ansible as a library for exactly one
reason: **version skew** between the copy that parses an inventory and the copy
that runs the playbook. The implementation did not deliver that property --
`api_ansible` installed `api.config.default_ansible_version` and never consulted
the workspace, so any workspace pinning `ansible_version` had its inventory
parsed by a different ansible-core than its playbook would run under.

Two sites independently deciding "which version" IS that skew arriving by the
back door, so these tests are mostly about there being ONE place the fallback is
written.

**On the issue's own framing, corrected by measurement.** It says the two sides
"read different settings", with the runner reading `Workspace.ansible_version`.
`runner/phases/ansible_env.py` does no such read: it takes `version` as a
parameter, and at the time of writing **nothing calls it** -- no production
caller, no ansible phase in `job_entrypoint`. So the API's value was wrong on
its own terms rather than wrong relative to a second resolution. That changes
the shape of the guard: a test asserting "both sides resolve through the same
helper" can assert nothing about a side that does not resolve. What is
assertable, and what these do, is that neither installer resolves for itself and
that exactly one function in the tree owns the fallback -- so the runner's
eventual caller cannot hand-roll one without failing this file.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from terrapod.services.workspace_settings import effective_ansible_version

#: The only sites allowed to read the deployment-wide default, with the reason.
#:
#: Tiny and derived rather than a growable ledger: every reader is found by
#: walking the AST, so a hand-rolled fallback anywhere else fails this test
#: rather than quietly becoming the second resolver.
ALLOWED_DEFAULT_READERS: dict[str, str] = {
    "terrapod/services/workspace_settings.py": (
        "the one resolver -- `effective_ansible_version`, which every consumer imports"
    ),
    "terrapod/api/routers/tfe_v2.py": (
        "workspace CREATE, which is a different concern: it writes the deployment "
        "default INTO the column so a new workspace carries a concrete version. "
        "Resolution is what happens later, at use time, to a column that may be empty"
    ),
}


def _source_root() -> pathlib.Path:
    """`terrapod/`, found from this file rather than from the CWD.

    A CWD-relative path makes the test depend on where pytest was invoked, which
    is how a source-introspection test comes to read nothing and pass.
    """
    import terrapod

    return pathlib.Path(terrapod.__path__[0])


def _default_readers() -> dict[str, list[int]]:
    """Every module reading `.default_ansible_version`, by AST not by grep.

    AST because the string appears in docstrings and comments throughout this
    area -- including in the modules that must NOT read it, where the comment
    explains why. A text match would call those readers.
    """
    root = _source_root()
    found: dict[str, list[int]] = {}
    for f in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:  # pragma: no cover -- a broken file fails elsewhere
            continue
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "default_ansible_version"
        ]
        if lines:
            found[f"terrapod/{f.relative_to(root)}"] = lines
    return found


class TestThereIsExactlyOneResolver:
    def test_the_walker_finds_something(self):
        """A floor. An AST walk that matches nothing passes every assertion
        below it, which is the shape that makes an introspection test green and
        meaningless."""
        assert _default_readers(), (
            "no module reads `default_ansible_version` at all -- the walker has "
            "stopped matching rather than the code having stopped reading"
        )

    def test_no_module_hand_rolls_the_fallback(self):
        readers = _default_readers()
        unexpected = sorted(set(readers) - set(ALLOWED_DEFAULT_READERS))
        assert unexpected == [], (
            "these modules read the deployment-wide ansible default themselves "
            "instead of calling `workspace_settings.effective_ansible_version`. Two "
            "sites deciding which version to use IS the skew that shelling out to "
            f"`ansible-inventory` exists to prevent: {unexpected}"
        )

    def test_every_allowance_is_still_used(self):
        """A ledger entry for a site that no longer reads it is a stale
        allowance, and a stale allowance is a hole left open for the next
        reader to walk through."""
        readers = _default_readers()
        stale = sorted(set(ALLOWED_DEFAULT_READERS) - set(readers))
        assert stale == [], f"remove these allowances; they read nothing now: {stale}"

    @pytest.mark.parametrize(
        "module",
        [
            "terrapod/services/api_ansible.py",
            "terrapod/runner/phases/ansible_env.py",
        ],
    )
    def test_neither_installer_resolves_for_itself(self, module):
        """The two modules that INSTALL ansible must be handed the version.

        This is the structural half of "one resolver": an installer that can
        read the config can also silently disagree with whoever called it. Both
        take `version` as a required parameter instead, so the only way to get
        one is to ask the resolver.
        """
        assert module not in _default_readers(), (
            f"{module} resolves its own version. It must take `version` and let "
            "`effective_ansible_version` decide, or it becomes a second resolver"
        )


class TestTheResolverItself:
    def test_a_workspace_pin_wins(self):
        assert effective_ansible_version("2.18.3") == "2.18.3"

    def test_empty_inherits_the_deployment_default(self, monkeypatch):
        """`""` is a value meaning "the deployment's default", and this is the
        READ that turns it into a version to install.

        Not a contradiction of `engine_version_attr`'s rule, which governs the
        WRITE boundary -- there, an explicit `""` is what records "inherit",
        and only an absent key falls through to the column default.
        """
        from terrapod.config import settings

        monkeypatch.setattr(settings, "default_ansible_version", "2.20.4")
        assert effective_ansible_version("") == "2.20.4"

    def test_none_inherits_too(self, monkeypatch):
        """A workspace with no row at all -- `resolve` tolerates `ws is None`
        -- has no pin, so it inherits like an unset one."""
        from terrapod.config import settings

        monkeypatch.setattr(settings, "default_ansible_version", "2.20.4")
        assert effective_ansible_version(None) == "2.20.4"

    def test_it_returns_empty_when_there_is_no_default_either(self, monkeypatch):
        """It does not raise. The two callers raise their OWN domain errors --
        a failed inventory read and a failed configure are different events and
        say different things -- so a resolver that guessed one would put the
        wrong message in front of an operator."""
        from terrapod.config import settings

        monkeypatch.setattr(settings, "default_ansible_version", "")
        assert effective_ansible_version("") == ""
        assert effective_ansible_version(None) == ""


class TestTheCallSiteActuallyResolvesFromTheWorkspace:
    """The rule and the call site are two things, and only one is a function.

    `test_neither_installer_resolves_for_itself` proves nobody hand-rolls the
    fallback; these prove the one live consumer *uses* the resolver and feeds it
    the workspace. A gate that only checked the former would pass with
    `effective_ansible_version("")` wired in, which resolves every workspace to
    the deployment default -- the defect, restored, through the resolver.

    Source introspection because `resolve()` is driven end to end only by the
    integration tier, which needs a real ansible install: the positional shape
    is the same as the `db.flush()`-inside-`try` gate, for the same reason.
    """

    @staticmethod
    def _resolve_uncached_ast() -> ast.FunctionDef:
        from terrapod.services import inventory_resolve

        tree = ast.parse(pathlib.Path(inventory_resolve.__file__).read_text())
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_resolve_uncached"
        )
        return fn

    def test_it_calls_the_resolver(self):
        calls = [
            n
            for n in ast.walk(self._resolve_uncached_ast())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "effective_ansible_version"
        ]
        assert len(calls) == 1, (
            "the resolution path does not call `effective_ansible_version` exactly "
            f"once (found {len(calls)}), so it is deciding the version some other way"
        )

    def test_the_resolver_is_handed_the_workspaces_own_value(self):
        """`effective_ansible_version("")` would type-check, pass the
        no-hand-rolled-fallback gate, and resolve every workspace to the
        deployment default. The ARGUMENT has to read the column.

        Inspecting the arguments rather than `ast.dump(call)` of the whole node:
        the dump contains the callee's own name, `effective_ansible_version`,
        which has `ansible_version` inside it -- so a substring check over the
        dump is satisfied by the call existing at all. Mutation-checked: the
        earlier version of this test passed with `effective_ansible_version("")`
        wired in, which is the whole defect restored through the resolver.
        """
        call = next(
            n
            for n in ast.walk(self._resolve_uncached_ast())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "effective_ansible_version"
        )
        reads_the_column = any(
            isinstance(node, ast.Attribute) and node.attr == "ansible_version"
            for arg in call.args
            for node in ast.walk(arg)
        )
        assert reads_the_column, (
            "the resolver is not handed the workspace's own `ansible_version`, so "
            f"every workspace resolves to the deployment default: {ast.unparse(call)}"
        )

    def test_the_version_reaches_ansible_inventory(self):
        """And it is the RESOLVED one that is passed on.

        Asserting only that a `version=` keyword exists is not enough --
        mutation-checked, `version="2.21.5"` satisfies that while ignoring the
        workspace entirely. So this follows the link: the keyword's value must
        be a name, and that name must be the one assigned from
        `effective_ansible_version` in this same function.
        """
        fn = self._resolve_uncached_ast()

        resolved_names = {
            target.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "effective_ansible_version"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert resolved_names, "nothing is bound from `effective_ansible_version`"

        call = next(
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "run_ansible_inventory"
        )
        passed = {k.arg: k.value for k in call.keywords}
        assert "version" in passed, (
            f"`run_ansible_inventory` is called without `version=`: {sorted(passed)}"
        )
        value = passed["version"]
        assert isinstance(value, ast.Name) and value.id in resolved_names, (
            f"`version=` does not carry the value resolved from the workspace: {ast.unparse(value)}"
        )
