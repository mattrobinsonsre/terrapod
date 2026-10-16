"""Cloud identity discovery evaluates the config with the plan's inputs (#2132).

Provider discovery runs a bare `tofu graph` / `terraform graph` and passed none
of the workspace's variable inputs, so a configuration that plans cleanly could
fail discovery — and with it the whole run, before planning started. The error
the operator then saw was a 300-character ANSI-coloured tail, cut again
downstream, so what survived was `Error: Faile`.

Everything asserted here was measured against OpenTofu 1.12.6 rather than read
off the flag's name:

* a variable needed during **static evaluation** (a module version) makes bare
  `graph` fail: *"Failed to request input from user for variable
  var.module_version"*. With `-var-file`, or with `terrapod.auto.tfvars`
  present, that error is gone;
* a variable used only in a **resource argument** is tolerated — `graph`
  returns 0 and lists the provider nodes. That asymmetry is why this went
  unnoticed, and why the fix is about static evaluation rather than variables
  in general;
* **`graph` rejects `-input=false`** — *"flag provided but not defined:
  -input"*. The issue suggested it; passing it would have broken every
  discovery. The no-prompt guarantee comes from giving the subprocess no stdin,
  which no engine can refuse;
* `graph -help` advertises `-var-file` on tofu, and the locally available
  `terraform` is 0.15.5, which does not. So support is probed per binary rather
  than assumed from either engine.

The workspace's Terraform **variables** already reached `graph` without help:
`terrapod.auto.tfvars` is rendered at step 4b of the entrypoint and the engine
auto-loads `*.auto.tfvars`, while discovery runs at 10b. Only the explicit
`var_files` were missing.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
from unittest.mock import patch

from terrapod.runner import job_entrypoint
from terrapod.runner.phases import cloud_identity, tf_args


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


_GRAPH_OK = (
    'digraph { "[root] provider[\\"registry.opentofu.org/hashicorp/aws\\"]" [shape = "diamond"] }'
)


class TestTheVarFilesReachTheGraph:
    """A contrast on the capability probe, because passing a flag an engine
    does not accept is as broken as omitting one it needs."""

    def test_they_are_passed_when_the_binary_supports_them(self):
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=True),
            patch.object(subprocess, "run", return_value=_Proc(stdout=_GRAPH_OK)) as run,
        ):
            cloud_identity.discover(
                binary="tofu",
                cwd=pathlib.Path("/w"),
                var_file_args=["-var-file=envs/example.tfvars"],
            )
        argv = run.call_args.args[0]
        assert argv[:2] == ["tofu", "graph"]
        assert "-var-file=envs/example.tfvars" in argv, (
            "the workspace's var_files did not reach graph, so a config whose module "
            "version comes from a variable fails discovery before planning (#2132)"
        )

    def test_they_are_omitted_when_the_binary_does_not(self):
        """The other half. `terraform` 0.15.5's `graph -help` lists no
        `-var-file`; passing it would fail with a flag-parse error, turning a
        fix into a different outage."""
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=False),
            patch.object(subprocess, "run", return_value=_Proc(stdout=_GRAPH_OK)) as run,
        ):
            cloud_identity.discover(
                binary="terraform",
                cwd=pathlib.Path("/w"),
                var_file_args=["-var-file=envs/example.tfvars"],
            )
        assert run.call_args.args[0] == ["terraform", "graph"]

    def test_no_var_files_is_not_an_empty_flag(self):
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=True),
            patch.object(subprocess, "run", return_value=_Proc(stdout=_GRAPH_OK)) as run,
        ):
            cloud_identity.discover(binary="tofu", cwd=pathlib.Path("/w"), var_file_args=[])
        assert run.call_args.args[0] == ["tofu", "graph"]


class TestItCannotWaitForAPrompt:
    def test_the_subprocess_is_given_no_stdin(self):
        """`-input=false` is the obvious answer and `graph` REJECTS it, so this
        is the guarantee instead. `capture_output=True` redirects stdout and
        stderr only — stdin stayed inherited."""
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=True),
            patch.object(subprocess, "run", return_value=_Proc(stdout=_GRAPH_OK)) as run,
        ):
            cloud_identity.discover(binary="tofu", cwd=pathlib.Path("/w"))
        assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_the_rejected_flag_is_not_passed(self):
        """Measured: `flag provided but not defined: -input`. A well-meant
        addition here would break every discovery on every engine."""
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=True),
            patch.object(subprocess, "run", return_value=_Proc(stdout=_GRAPH_OK)) as run,
        ):
            cloud_identity.discover(binary="tofu", cwd=pathlib.Path("/w"))
        assert not [a for a in run.call_args.args[0] if a.startswith("-input")]


class TestTheFailureNamesItsCause:
    #: Verbatim shape of what the engine emits, ANSI included.
    RAW = (
        "\x1b[31m╷\x1b[0m\x1b[0m\n"
        "\x1b[31m│\x1b[0m \x1b[0m\x1b[1m\x1b[31mError: \x1b[0m\x1b[0m\x1b[1m"
        "Failed to request input from user for variable var.module_version\x1b[0m\n"
        "\x1b[31m│\x1b[0m \x1b[0m\n"
        "\x1b[31m│\x1b[0m \x1b[0mOpenTofu needs a value but cannot prompt.\n"
        "\x1b[31m╵\x1b[0m\x1b[0m\n"
    )

    def test_the_variable_is_named_where_a_tail_named_nothing(self):
        detail = cloud_identity._engine_failure_detail(self.RAW)
        assert "var.module_version" in detail, (
            "the operator needs the variable's name; the previous 300-char tail "
            "delivered `Error: Faile`"
        )
        assert "\x1b[" not in detail, "ANSI codes must not reach the operator"
        assert "Failed to request input" in detail

    def test_the_contrast_a_raw_tail_would_have_lost_it(self):
        """Why the unit changed from a tail to the summary: the useful part of a
        terraform/tofu diagnostic is at the START of the block."""
        assert "var.module_version" not in self.RAW.strip()[-40:]

    def test_output_with_no_diagnostic_says_it_is_a_fragment(self):
        detail = cloud_identity._engine_failure_detail("some unstructured noise")
        assert "no Error: line found" in detail, (
            "a fallback that looks like a real diagnostic is worse than one that admits what it is"
        )

    def test_empty_output_is_said_plainly(self):
        assert cloud_identity._engine_failure_detail("") == "no output"

    def test_discover_ACTUALLY_USES_it(self):
        """The test this class was missing, found by mutation.

        Every assertion above hands the raw output to `_engine_failure_detail`
        by hand, so reverting `discover` to its old 300-character tail left all
        of them passing -- a correct helper nobody calls. This drives
        `discover` with a failing engine and asserts on what it RETURNS.
        """
        with (
            patch.object(tf_args, "graph_supports_var_file", return_value=True),
            patch.object(subprocess, "run", return_value=_Proc(rc=1, stderr=self.RAW)),
        ):
            found = cloud_identity.discover(binary="tofu", cwd=pathlib.Path("/w"))

        assert found.outcome == "failed"
        assert "var.module_version" in found.detail, (
            "discovery's own detail does not name the variable, so the operator still "
            "gets a fragment -- the helper is not being used (#2132)"
        )
        assert "\x1b[" not in found.detail
        assert "exited 1" in found.detail, "the exit code is still worth reporting"


class TestTheCallSitePassesThem:
    """The gate, and the reason it exists.

    #2036 recorded this exact failure: a gate was added to this very phase,
    tested six ways, and then the entrypoint's call dropped the argument —
    1061 tests passed, because every test drove the function directly and the
    default was the broken case. A fix that is not wired is not a fix.
    """

    def _plan_path_call(self) -> ast.Call:
        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        # Match the CALLEE exactly, not the unparsed text: every site is
        # `cloud_identity.run(...).items()`, so a substring test counts the
        # enclosing `.items()` call as well and reports twice as many.
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and ast.unparse(n.func.value) == "cloud_identity"
        ]
        assert len(calls) == 2, (
            f"expected two cloud_identity.run call sites (Terraform and Pulumi), found "
            f"{len(calls)}; this gate has drifted from the entrypoint"
        )
        # The Pulumi path passes `discover_providers=False` and has no var files.
        plan = [c for c in calls if "discover_providers" not in {k.arg for k in c.keywords}]
        assert len(plan) == 1, "could not identify the discovering call site"
        return plan[0]

    def test_the_discovering_call_site_passes_var_file_args(self):
        call = self._plan_path_call()
        passed = {k.arg: k.value for k in call.keywords}
        assert "var_file_args" in passed, (
            "discovery runs without the workspace's var_files, which is #2132 exactly"
        )
        assert isinstance(passed["var_file_args"], ast.Name), (
            "expected the argv built at step 8, not a literal"
        )

    def test_it_is_handed_the_same_argv_the_plan_uses(self):
        """Not a freshly built list: the point is that discovery and the plan
        agree, so it must be the SAME name the other phases are given."""
        name = self._plan_path_call()
        passed = {k.arg: k.value for k in name.keywords}["var_file_args"]
        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        others = {
            k.value.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            for k in n.keywords
            if k.arg == "var_file_args" and isinstance(k.value, ast.Name)
        }
        assert others, "no call passes var_file_args by name anywhere in the entrypoint"
        assert passed.id in others
        assert len(others) == 1, (
            f"more than one name is passed as var_file_args ({sorted(others)}); the phases "
            "may no longer agree on the inputs"
        )
