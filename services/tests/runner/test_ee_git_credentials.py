"""The git credential the execution environment clones a role with (#2320).

`git_auth` does all the work -- a per-host key, a `known_hosts`, an ssh config
with `StrictHostKeyChecking yes`, and a gitconfig wiring them in through
`core.sshCommand` -- and wrote every byte of it under the ORCHESTRATOR's `$HOME`.
The container that actually runs `ansible-galaxy` is the execution environment,
which mounts a different `/home`, so none of it was visible where a git-sourced
role is cloned. An in-house role, which is the common case rather than a corner,
could not be installed from anywhere needing a credential.

WHAT THIS FILE PINS, AND WHAT IT DELIBERATELY DOES NOT. Here: the wiring --
which container is told which path, that the directory is created before anyone
writes to it, and that the two sides agree on the filename. Whether git and
`ansible-galaxy` then ACCEPT that material is a different question and not one
any spec assertion can answer, so it is `scripts/git-source-gate.sh`, which
drives a real clone against a real server through the real phase.

The split matters because the two halves fail differently and a test that
conflated them would be weaker than either. A wiring defect is silent: the
material is written somewhere nothing reads and the clone fails for want of a
credential that exists. An acceptance defect is loud but needs a server.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from terrapod.runner import job_template
from tests.runner.test_job_spec_ee import EE, _init, _pod


def _env_of(container) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container.get("env", [])}


class TestTheTwoSidesAgreeOnOneFile:
    """The property, rather than two assertions that happen to use one constant.

    The orchestrator writes a gitconfig and the EE reads one, and the whole
    mechanism is that it is the SAME file. Asserting each side against the
    module constant separately would pass if one of them were repointed, because
    each assertion would still agree with itself.
    """

    def test_the_path_the_ee_reads_is_the_path_the_orchestrator_is_given(self):
        pod = _pod(ee_image=EE)
        runner = next(c for c in pod["containers"] if c["name"] == "runner")
        ee = _init(pod, "ee")

        written_under = _env_of(runner)["TP_EE_GIT_DIR"]
        read_from = _env_of(ee)["GIT_CONFIG_GLOBAL"]

        assert read_from.startswith(written_under + "/"), (
            f"the EE reads {read_from}, which is not inside the directory the "
            f"orchestrator was told to write to ({written_under}) -- so the "
            "credential would be written somewhere nothing reads and the clone "
            "would fail for want of a credential that exists"
        )

    def test_both_paths_are_on_the_shared_volume_and_absolute(self):
        """Absolute because the generated gitconfig contains absolute paths, and
        on the SHARED volume because that is the only mount both containers have
        at the same path -- which is what lets one copy serve both."""
        pod = _pod(ee_image=EE)
        runner = next(c for c in pod["containers"] if c["name"] == "runner")
        ee = _init(pod, "ee")

        git_dir = _env_of(runner)["TP_EE_GIT_DIR"]
        assert git_dir.startswith("/"), git_dir

        def mount_path(container, volume):
            return next(m["mountPath"] for m in container["volumeMounts"] if m["name"] == volume)

        shared = "tp"
        assert mount_path(runner, shared) == mount_path(ee, shared), (
            "the shared volume is mounted at different paths in the two "
            "containers, so an absolute path written by one does not resolve in "
            "the other"
        )
        assert git_dir.startswith(mount_path(runner, shared) + "/")
        assert _env_of(ee)["GIT_CONFIG_GLOBAL"].startswith(mount_path(ee, shared) + "/")


class TestTheDirectoryExistsBeforeAnyoneWritesToIt:
    def test_the_init_container_creates_it(self):
        """Reading `mkdir -p`'s OWN arguments, not the whole script.

        `git_dir in script` is satisfied by the `touch {gitconfig}` a few words
        later, because the gitconfig's path contains the directory's -- so that
        version passed with the mkdir argument deleted, which is the one defect
        this test exists to catch. The mutation found it; reading the assertion
        did not.
        """
        pod = _pod(ee_image=EE)
        git_dir = _env_of(next(c for c in pod["containers"] if c["name"] == "runner"))[
            "TP_EE_GIT_DIR"
        ]
        script = " ".join(_init(pod, "agent-copy")["command"])
        mkdir = script.split("mkdir -p", 1)
        assert len(mkdir) == 2, f"no `mkdir -p` in the init script: {script}"
        # Up to the next shell operator, which is where the argument list ends.
        created = mkdir[1].split("&&")[0].split(";")[0].split()
        assert git_dir in created, (
            f"`mkdir -p` creates {created} and not {git_dir}, so the "
            "orchestrator's first write fails on a read-only root -- which reads "
            "as a credential defect rather than a missing directory"
        )

    def test_the_gitconfig_itself_is_TOUCHED(self):
        """`GIT_CONFIG_GLOBAL` is set unconditionally, and a run that declares no
        git credential writes nothing. git tolerates a missing config for a
        clone, but `git config --list` answers "fatal: unable to read config
        file" -- which turns a diagnostic into a false alarm. An empty file
        costs nothing and removes the wrinkle."""
        pod = _pod(ee_image=EE)
        gitconfig = _env_of(_init(pod, "ee"))["GIT_CONFIG_GLOBAL"]
        script = " ".join(_init(pod, "agent-copy")["command"])
        assert f"touch {gitconfig}" in script, script


class TestAPlanOrApplyRunIsUNCHANGED:
    """A terraform/tofu-only deployment pays nothing, and neither does a
    plan/apply run on a workspace that also has a configure. The absence is the
    mechanism rather than a saving: `TP_EE_GIT_DIR` missing is how the
    orchestrator knows there is no second container to share with, so it falls
    back to HOME exactly as before."""

    def test_neither_variable_appears_without_an_execution_environment(self):
        pod = _pod()
        runner = next(c for c in pod["containers"] if c["name"] == "runner")
        assert "TP_EE_GIT_DIR" not in _env_of(runner)
        assert "GIT_CONFIG_GLOBAL" not in _env_of(runner)
        assert not pod.get("initContainers")


class TestTheOrchestratorActuallyPassesItThrough:
    """A source test, because there is no seam: the call sits mid-way through
    `_run_body` and the only behavioural alternative would be to run the whole
    entrypoint. It asserts the ARGUMENT is passed and that it is derived from the
    environment variable -- not merely that the name appears, which an unused
    assignment would satisfy."""

    def _run_body(self) -> ast.FunctionDef:
        from terrapod.runner import job_entrypoint

        tree = ast.parse(Path(inspect.getsourcefile(job_entrypoint)).read_text())
        return next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_body"
        )

    def test_git_auth_run_is_called_with_a_base_dir(self):
        calls = [
            n
            for n in ast.walk(self._run_body())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "git_auth"
        ]
        assert len(calls) == 1, f"expected one git_auth.run call, found {len(calls)}"
        kwargs = {k.arg for k in calls[0].keywords}
        assert "base_dir" in kwargs, (
            "git_auth.run is called without base_dir, so it falls back to HOME "
            "and the EE sees nothing -- the whole of #2320, restored"
        )

    def test_the_base_dir_comes_from_the_environment_variable(self):
        """Following the keyword's VALUE to the name it holds, and that name to
        the assignment that reads the variable. A keyword existing is satisfied
        by any value at all, including a hard-coded path that would be right on
        one container and wrong on the other."""
        body = self._run_body()
        call = next(
            n
            for n in ast.walk(body)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "git_auth"
        )
        arg = next(k.value for k in call.keywords if k.arg == "base_dir")
        assert isinstance(arg, ast.Name), ast.dump(arg)

        src = Path(
            inspect.getsourcefile(__import__("terrapod.runner.job_entrypoint", fromlist=["x"]))
        ).read_text()
        # The name is assigned from a conditional over `TP_EE_GIT_DIR`; assert
        # the variable is what decides, rather than that the name exists.
        assigns = [
            n
            for n in ast.walk(body)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == arg.id for t in n.targets)
        ]
        assert len(assigns) == 1, f"{arg.id} is assigned {len(assigns)} times"
        assert "TP_EE_GIT_DIR" in ast.dump(assigns[0]), ast.dump(assigns[0])
        assert "TP_EE_GIT_DIR" in src


class TestTheConstantsAreNotRESTATED:
    """One producer for each path. A second spelling of `/tp/git` anywhere is how
    the two sides come to disagree about a filename, which is precisely the
    failure the first class in this file exists to catch -- so forbid the way it
    would arrive rather than only detecting it afterwards."""

    def test_the_shared_dir_is_built_from_the_shared_constant(self):
        assert job_template._EE_GIT_DIR.startswith(job_template._EE_SHARED_DIR + "/")
        assert job_template._EE_GITCONFIG.startswith(job_template._EE_GIT_DIR + "/")

    def test_no_literal_tp_git_path_elsewhere_in_the_module(self):
        src = Path(inspect.getsourcefile(job_template)).read_text()
        literal = f'"{job_template._EE_GIT_DIR}"'
        assert src.count(literal) == 0, (
            f"{literal} is written as a literal in job_template.py; build it from "
            "_EE_SHARED_DIR so there is one producer"
        )
