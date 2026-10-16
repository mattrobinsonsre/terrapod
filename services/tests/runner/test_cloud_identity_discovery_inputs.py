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

    def test_it_is_handed_a_list_resolved_against_the_plans_directory(self):
        """Supersedes an earlier assertion that this had to be the SAME name
        the other phases are given.

        That assertion's reason still holds -- discovery and the plan must see
        the same inputs -- but it had the mechanism wrong, and the wrong way
        round. The phases do not share a working directory: `exec_subprocess.run`
        takes no `cwd`, so plan and apply run in the PROCESS directory, while
        discovery is given `cwd=cwd`, which step 9b has already moved to the
        `.terragrunt-cache` copy on every Terragrunt run. Handing both the same
        relative `-var-file` therefore made them read DIFFERENT files, which is
        the opposite of agreeing. The same list is the appearance of agreement;
        a list resolved against the plan's own directory is the substance.
        """
        passed = {k.arg: k.value for k in self._plan_path_call().keywords}["var_file_args"]
        assert isinstance(passed, ast.Name), "expected a name built at step 8"

        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        # Follow the name to its assignment and require a resolution base.
        builds = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == passed.id
        ]
        assert len(builds) == 1, (
            f"expected exactly one assignment to {passed.id}, found {len(builds)}"
        )
        call = builds[0].value
        assert isinstance(call, ast.Call) and ast.unparse(call.func).endswith("var_file_args"), (
            f"{passed.id} is not built by tf_args.var_file_args"
        )
        base = {k.arg: k.value for k in call.keywords}.get("base_dir")
        assert base is not None, (
            f"{passed.id} is built without base_dir, so a relative -var-file resolves "
            "against whatever directory discovery happens to run in (#2132 follow-up)"
        )
        assert isinstance(base, ast.Name) and base.id == "plan_cwd", (
            f"base_dir is {ast.unparse(base)}; it must be the captured process directory, "
            "because that is where the plan reads these files"
        )

    def test_the_other_phases_still_get_the_operators_paths_verbatim(self):
        """Init, plan and apply run in the process directory already, so
        resolving their paths would change commands that work today."""
        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        verbatim = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "var_file_argv"
            and isinstance(n.value, ast.Call)
        ]
        assert verbatim, "var_file_argv is no longer built in the entrypoint"
        for n in verbatim:
            assert "base_dir" not in {k.arg for k in n.value.keywords}, (
                "var_file_argv is resolved, which changes the init/plan/apply command line"
            )

    def test_plan_cwd_is_captured_before_anything_can_move_cwd(self):
        """Positional, because a behavioural test cannot see it.

        `plan_cwd` is only the plan's directory if it is taken before step 9b
        reassigns `cwd` to the Terragrunt cache copy. Captured after, it would
        equal `cwd` and the fix would be a no-op -- silently, and exactly on
        the workspaces it exists for.
        """
        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        captures = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "plan_cwd"
        ]
        assert len(captures) == 1, f"expected one plan_cwd capture, found {len(captures)}"
        moves = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "cwd"
            and "tg_work" in ast.unparse(n.value)
        ]
        assert moves, "the Terragrunt reassignment of cwd is gone; re-read this gate"
        assert captures[0] < min(moves), (
            f"plan_cwd is captured at line {captures[0]}, after cwd is moved at "
            f"{min(moves)} -- so it is the cache dir, not the plan's directory"
        )


class TestTheResolutionBase:
    """`var_file_args`' optional base, which is what makes the two agree."""

    def test_a_relative_path_resolves_against_the_base(self):
        out = tf_args.var_file_args(["../../env/prod.tfvars"], base_dir=pathlib.Path("/w/unit"))
        assert out == ["-var-file=/w/unit/../../env/prod.tfvars"]

    def test_an_absolute_path_is_left_alone(self):
        out = tf_args.var_file_args(["/etc/prod.tfvars"], base_dir=pathlib.Path("/w/unit"))
        assert out == ["-var-file=/etc/prod.tfvars"]

    def test_without_a_base_the_path_is_byte_for_byte(self):
        """The plan's command line must not change. `Path("./a/")` stringifies
        to `a`, so round-tripping through Path would rewrite it."""
        assert tf_args.var_file_args(["./env/prod.tfvars"]) == ["-var-file=./env/prod.tfvars"]
        assert tf_args.var_file_args(["env/"]) == ["-var-file=env/"]

    def test_empty_entries_are_still_dropped_either_way(self):
        assert tf_args.var_file_args(["", "a.tfvars"]) == ["-var-file=a.tfvars"]
        assert tf_args.var_file_args(["", "a.tfvars"], base_dir=pathlib.Path("/w")) == [
            "-var-file=/w/a.tfvars"
        ]


class TestTheProbeCannotRaiseIntoThePhase:
    """`discover` documents itself as never raising, and the probe execs a
    binary -- so every exec failure must be the probe's to absorb."""

    @staticmethod
    def _exec_format_error(*a, **k):
        raise OSError(8, "Exec format error")

    def test_the_probe_answers_false_on_a_binary_it_cannot_execute(self):
        with patch("subprocess.run", side_effect=self._exec_format_error):
            assert tf_args.graph_supports_var_file("tofu") is False
            assert tf_args.init_supports_var_file("tofu") is False

    def test_the_probe_answers_false_when_the_runner_cannot_fork(self):
        with patch("subprocess.run", side_effect=OSError(12, "Cannot allocate memory")):
            assert tf_args.graph_supports_var_file("tofu") is False

    def test_discover_degrades_rather_than_raising_when_the_probe_explodes(self):
        """The whole point: a workspace with var_files and a broken binary must
        get a failed discovery, not a runner traceback. Before this, a run that
        configured no cloud identity at all died on an unrelated exec error."""
        with patch.object(tf_args, "graph_supports_var_file", side_effect=self._exec_format_error):
            got = cloud_identity.discover(
                binary="tofu", cwd=pathlib.Path("/w"), var_file_args=["-var-file=a.tfvars"]
            )
        assert got.outcome == "failed"
        assert "Exec format error" in got.detail


class TestADiagnosticIsPairedWithItsOwnDetail:
    """An engine repeats a summary verbatim; the detail is what differs."""

    def test_two_identical_summaries_keep_their_own_details(self):
        got = cloud_identity._engine_failure_detail(
            "Error: Failed to read variables file\n"
            "  Given variables file prod.tfvars does not exist.\n"
            "\n"
            "Error: Failed to read variables file\n"
            "  Given variables file staging.tfvars does not exist.\n"
        )
        assert "prod.tfvars" in got, got
        assert "staging.tfvars" in got, (
            "the second summary was paired with the first one's detail, so the operator "
            "is told the wrong file"
        )
        assert got.index("prod.tfvars") < got.index("staging.tfvars"), "details swapped"

    def test_every_ansi_form_an_engine_emits_is_stripped(self):
        """Not only colour: a progress line erases itself with CSI K and a
        registry URL arrives wrapped in an OSC 8 hyperlink."""
        got = cloud_identity._engine_failure_detail(
            "\x1b[31mError: boom\x1b[0m\n"
            "  see \x1b]8;;https://example.test\x07the docs\x1b]8;;\x07\x1b[K\n"
        )
        assert "\x1b" not in got, f"an escape survived: {got!r}"
        assert "Error: boom" in got
        assert "the docs" in got
