"""The configure phase (#1972): argv, the forced environment, and fail-closed.

`ee_client` is stubbed here rather than driven -- its own transport properties
have their own tests against a real server. What these pin is what the phase
DECIDES: which arguments ansible gets, which environment it runs under, where it
runs, and which conditions refuse rather than returning an exit code.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from terrapod.runner import ee_client
from terrapod.runner.phases import configure
from terrapod.runner.phases.configure import ConfigureNotConfigured

#: Stands in for the materialised resolved inventory. A fixed path rather than
#: a tmp_path fixture because every assertion below is about the ARGUMENTS, and
#: `argv` never touches the file -- `configure_inventory` owns producing it and
#: has its own tests.
_INV = Path("/tmp/terrapod-resolved-inventory.yml")


@pytest.fixture
def cfg():
    from terrapod.runner.runner_config import RunnerConfig

    base = RunnerConfig.from_env(
        {
            "TP_RUN_ID": "run-1",
            "TP_PHASE": "configure",
            "TP_WORKSPACE_ID": "ws-1",
            "TP_API_URL": "http://api",
            "TP_AUTH_TOKEN": "t",
        }
    )
    return replace(
        base,
        config_directory="ansible",
        playbook="site.yml",
        ansible_limit="",
        parallelism=10,
    )


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "ansible").mkdir()
    return tmp_path


@pytest.fixture
def ee(monkeypatch):
    """A stub execution environment that records how it was driven."""
    calls: list[dict] = []

    def fake_run(binary, args, *, cwd, env=None, log_file=None, tee_to_stdout=True):
        calls.append({"bin": binary, "args": list(args), "cwd": cwd, "env": dict(env or {})})
        return ee_client.EEResult(bin=binary, rc=fake_run.rc, error=fake_run.error)

    fake_run.rc = 0
    fake_run.error = ""
    monkeypatch.setattr(ee_client, "configured", lambda: True)
    monkeypatch.setattr(ee_client, "run", fake_run)
    fake_run.calls = calls
    return fake_run


class TestArgv:
    def test_there_is_exactly_one_inventory_and_it_is_the_artifact(self, cfg):
        """One `-i`, the run's resolved artifact (#2021).

        Asserted as the WHOLE argument list, and as a count, because the defect
        this replaces is an extra source rather than a wrong one: the API merged
        the workspace's `inventory_paths` when it resolved this run, so passing
        them here too would hand ansible the same hosts from two sources and let
        `-i` ordering decide a precedence the operator never expressed. An
        earlier version of this phase did exactly that, which is why the count
        is pinned and not just the contents.
        """
        assert configure.argv(cfg, inventory=_INV) == ["-i", str(_INV), "site.yml"]
        assert configure.argv(cfg, inventory=_INV).count("-i") == 1

    def test_the_limit_narrows_this_run_only_when_set(self, cfg):
        assert "--limit" not in configure.argv(cfg, inventory=_INV)
        assert configure.argv(replace(cfg, ansible_limit="web"), inventory=_INV)[-3:] == [
            "--limit",
            "web",
            "site.yml",
        ]

    def test_the_playbook_is_last_so_it_is_never_read_as_a_flag_value(self, cfg):
        assert configure.argv(replace(cfg, ansible_limit="web"), inventory=_INV)[-1] == "site.yml"

    def test_no_playbook_refuses_rather_than_guessing(self, cfg):
        """Every available fallback is wrong: guessing runs something nobody
        asked for, and running none reports success for a configure that did
        nothing."""
        with pytest.raises(ConfigureNotConfigured) as e:
            configure.argv(replace(cfg, playbook=""), inventory=_INV)
        assert "default_playbook" in str(e.value)

    def test_there_is_no_check_flag(self, cfg):
        """No dry run exists (user, 2026-10-14) -- check mode is unreliable per
        module, is not a diff, and misleads on anything gated by `when: changed`."""
        assert "--check" not in configure.argv(replace(cfg, ansible_limit="web"), inventory=_INV)


class TestTheForcedEnvironment:
    def test_the_plugin_dispatcher_is_disabled(self, cfg, tree, ee):
        """`auto` reads a file's own `plugin:` key and runs whatever it names,
        and `script` executes an inventory file. Neither is enabled (#1970)."""
        configure.run(cfg, work_dir=tree, inventory=_INV)
        env = ee.calls[0]["env"]
        assert env["ANSIBLE_INVENTORY_ENABLED"] == "yaml,ini"
        assert "auto" not in env["ANSIBLE_INVENTORY_ENABLED"]
        assert "script" not in env["ANSIBLE_INVENTORY_ENABLED"]

    def test_an_unparseable_source_fails_closed(self, cfg, tree, ee):
        """#1967: a source that cannot be read must not resolve to a partial
        host set. A configure reaching half a fleet and reporting success is
        worse than one that refuses."""
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert ee.calls[0]["env"]["ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED"] == "True"

    def test_no_ansible_config_is_ever_written(self, cfg, tree, ee):
        """Their `ansible.cfg` is single-file with no merging, so writing one
        would discard `roles_path`, `forks`, `become`, `remote_user` and
        `inventory` itself. Env beats the file, which is why this works."""
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert "ANSIBLE_CONFIG" not in ee.calls[0]["env"]
        assert not list((tree / "ansible").glob("ansible.cfg"))

    def test_an_inherited_ansible_setting_does_not_reach_the_process(
        self, cfg, tree, ee, monkeypatch
    ):
        """Stripping, not overriding. Env beats `ansible.cfg`, so an inherited
        `ANSIBLE_INVENTORY_ENABLED=auto` would re-enable the dispatcher the
        forced set exists to disable -- and ansible has far more settings than
        the ones we think to override."""
        monkeypatch.setenv("ANSIBLE_INVENTORY_ENABLED", "auto")
        monkeypatch.setenv("ANSIBLE_STRATEGY", "free")
        configure.run(cfg, work_dir=tree, inventory=_INV)
        env = ee.calls[0]["env"]
        assert env["ANSIBLE_INVENTORY_ENABLED"] == "yaml,ini"
        assert "ANSIBLE_STRATEGY" not in env

    def test_writable_temp_directories_are_passed_through(self, cfg, tree, ee, monkeypatch):
        """A read-only root filesystem makes ansible exit `rc 250` with "No
        usable temporary directory found", naming nothing useful. The Job
        supplies them (#2154)."""
        monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", "/tp/tmp/local")
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert ee.calls[0]["env"]["ANSIBLE_LOCAL_TEMP"] == "/tp/tmp/local"

    def test_the_workspaces_parallelism_is_what_sets_forks(self, cfg, tree, ee):
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert ee.calls[0]["env"]["ANSIBLE_FORKS"] == "10"


class TestTheCredentialEnvironment:
    """#1933. The vault identity list reaches ansible as an ARGUMENT.

    It cannot travel through `os.environ`: `_forced_env` strips every inherited
    `ANSIBLE_*` before setting its own, which is what stops an inherited
    `ANSIBLE_INVENTORY_ENABLED=auto` re-enabling the plugin dispatcher. So an
    exported identity list would be stripped on the way out and the configure
    would run with no vault password, silently -- the exact shape these tests
    exist to pin, because nothing else in the suite would notice.
    """

    def test_the_credential_environment_reaches_the_execution_environment(self, cfg, tree, ee):
        configure.run(
            cfg,
            work_dir=tree,
            inventory=_INV,
            extra_env={"ANSIBLE_VAULT_IDENTITY_LIST": "prod@/tp/credentials/vault-prod"},
        )
        assert (
            ee.calls[0]["env"]["ANSIBLE_VAULT_IDENTITY_LIST"] == "prod@/tp/credentials/vault-prod"
        )

    def test_an_EXPORTED_vault_list_is_stripped_which_is_why_it_is_an_argument(
        self, cfg, tree, ee, monkeypatch
    ):
        """The negative half, and the reason the argument exists at all."""
        monkeypatch.setenv("ANSIBLE_VAULT_IDENTITY_LIST", "prod@/exported")
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert "ANSIBLE_VAULT_IDENTITY_LIST" not in ee.calls[0]["env"]

    def test_a_credential_cannot_override_a_forced_setting(self, cfg, tree, ee):
        """`_forced_env` is merged LAST. Otherwise a credential -- or a variable
        that reached this dict by any future route -- could re-enable the plugin
        dispatcher #1970 disabled, which is the one thing the forced set is
        for."""
        configure.run(
            cfg,
            work_dir=tree,
            inventory=_INV,
            extra_env={"ANSIBLE_INVENTORY_ENABLED": "auto", "ANSIBLE_FORCE_COLOR": "1"},
        )
        assert ee.calls[0]["env"]["ANSIBLE_INVENTORY_ENABLED"] == "yaml,ini"
        assert ee.calls[0]["env"]["ANSIBLE_FORCE_COLOR"] == "0"

    def test_no_credentials_changes_nothing(self, cfg, tree, ee):
        configure.run(cfg, work_dir=tree, inventory=_INV, extra_env={})
        assert "ANSIBLE_VAULT_IDENTITY_LIST" not in ee.calls[0]["env"]


class TestTheConfigureFlowIsWired:
    """The call site, not the functions.

    Every test above drives `configure.run` directly, so dropping `extra_env`
    from the entrypoint's call would leave all of them green while the vault
    password never left the phase that materialised it. There is no seam in the
    middle of `_run_configure_phase`, so the assertion is over its source --
    the same shape as the `db.flush()`-inside-`try` gate.
    """

    def _function(self):
        import ast
        import pathlib

        from terrapod.runner import job_entrypoint

        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        found = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_run_configure_phase"
        ]
        assert len(found) == 1, "_run_configure_phase was renamed; fix this gate"
        return found[0]

    def _call(self, fn, *, module: str, func_attr: str):
        """The one `module.func_attr(...)` call in the function.

        Matched on the MODULE as well as the attribute, because `.run` alone is
        ambiguous here -- `ansible_auth.run()` and `configure.run()` are both in
        this function, and a matcher that took the first would have asserted the
        ordering between the wrong two positions while passing.
        """
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == func_attr
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == module
        ]
        assert len(calls) == 1, (
            f"expected exactly one {module}.{func_attr}() call, found {len(calls)}"
        )
        return calls[0]

    def test_the_credential_phase_is_called(self):
        src = __import__("ast").dump(self._function())
        assert "ansible_auth" in src, "nothing materialises the credentials"

    def test_the_vault_environment_is_PASSED_to_the_configure(self):
        """Inspecting the keyword's VALUE, not merely that a keyword named
        `extra_env` exists: `extra_env={}` would satisfy the weaker check and
        restore the defect in full.

        **The assertion is that the credential phase's name REACHES the
        expression, not that it IS the expression.** It used to require a bare
        `ast.Name`, which pinned the FORM rather than the property -- and the
        form stopped being the only correct one as soon as a second concern
        contributed to the same environment (#1928's callback settings, merged
        as `{**credential_env, **capture_env}`). A gate written for one instance
        of a class is blind to the rest; the property here is that the
        credentials travel as an ARGUMENT rather than through `os.environ`,
        because `configure._forced_env` strips every inherited `ANSIBLE_*` and
        an exported vault identity list would be stripped on the way out.
        """
        fn = self._function()
        call = self._call(fn, module="configure", func_attr="run")
        passed = {k.arg: k.value for k in call.keywords}
        assert "extra_env" in passed, "configure.run is called without extra_env"

        # The name(s) `ansible_auth.run()` was unpacked into.
        assigned = {
            t.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and "ansible_auth" in ast.dump(node.value)
            for t in ast.walk(node.targets[0])
            if isinstance(t, ast.Name)
        }
        assert assigned, "nothing is assigned from ansible_auth.run()"

        # One of them must appear in whatever is passed, at any depth: a bare
        # name, a merge, a call around it. What is refused is an expression that
        # does not mention it at all -- `{}`, or a dict of someone else's env.
        mentioned = {n.id for n in ast.walk(passed["extra_env"]) if isinstance(n, ast.Name)}
        assert mentioned & assigned, (
            f"extra_env mentions {sorted(mentioned)}, none of which is assigned from "
            f"ansible_auth.run() (that assigns {sorted(assigned)}) -- so the vault "
            "identity list never reaches the configure"
        )

    def test_the_credential_REFERENCES_are_PASSED_to_the_inventory(self):
        """Its other half. The machine credential reaches ansible through the
        rendered inventory -- the inventory names it with a placeholder and the
        render substitutes -- so a `materialise` call without them renders a
        document whose references cannot resolve, and the render refuses."""
        fn = self._function()
        call = self._call(fn, module="configure_inventory", func_attr="materialise")
        passed = {k.arg: k.value for k in call.keywords}
        assert "resolved" in passed, "materialise is called without resolved"
        assert isinstance(passed["resolved"], ast.Name), (
            "resolved must be the name the credential phase returned, not a literal"
        )

    def test_credentials_are_resolved_BEFORE_the_inventory_and_the_install(self):
        """They are a local file read that can refuse without touching the
        network, so they are the cheapest of the three to fail in -- and a
        configure with an unusable credential is not going to run whatever the
        other two produce."""
        fn = self._function()
        cred = min(
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and "ansible_auth" in ast.dump(n)
        )
        assert cred < self._call(fn, module="configure_inventory", func_attr="materialise").lineno
        assert cred < self._call(fn, module="ansible_collections", func_attr="install").lineno


class TestWhereItRuns:
    def test_the_working_directory_is_the_config_root(self, cfg, tree, ee):
        """Not the repository root. `group_vars` beside an inventory is found
        cwd-relative and nowhere else, and relative paths in their own
        `ansible.cfg` resolve against the cwd rather than the file (#1929)."""
        configure.run(cfg, work_dir=tree, inventory=_INV)
        assert ee.calls[0]["cwd"] == str((tree / "ansible").resolve())

    def test_an_empty_config_directory_means_the_repository_root(self, cfg, tree, ee):
        """`""` is the repo root; NULL means no configure operation at all, and
        the API does not dispatch one."""
        configure.run(replace(cfg, config_directory=""), work_dir=tree, inventory=_INV)
        assert ee.calls[0]["cwd"] == str(tree)

    def test_a_config_root_missing_from_the_tree_refuses(self, cfg, tree, ee):
        with pytest.raises(ConfigureNotConfigured) as e:
            configure.run(replace(cfg, config_directory="nope"), work_dir=tree, inventory=_INV)
        assert "nope" in str(e.value)


class TestItFailsClosed:
    def test_without_an_execution_environment_it_raises(self, cfg, tree, monkeypatch):
        """The orchestrator ships no ansible by design, so there is nothing to
        fall back to -- and a returned exit code would read as ansible's."""
        monkeypatch.setattr(ee_client, "configured", lambda: False)
        with pytest.raises(ee_client.ExecutionEnvironmentUnavailable) as e:
            configure.run(cfg, work_dir=tree, inventory=_INV)
        assert "no execution environment" in str(e.value)

    def test_an_agent_error_is_an_environment_failure_not_an_estate_one(self, cfg, tree, ee):
        """rc=126 with a reason means the EE cannot run playbooks at all. The
        operator's response differs from "the playbook failed", so this must not
        arrive as an ordinary non-zero exit."""
        ee.rc = 126
        ee.error = '"ansible-playbook" is not an ansible entry point this agent will run'
        with pytest.raises(ee_client.ExecutionEnvironmentUnavailable) as e:
            configure.run(cfg, work_dir=tree, inventory=_INV)
        assert "could not run ansible-playbook" in str(e.value)

    def test_ansibles_own_non_zero_exit_is_returned_not_raised(self, cfg, tree, ee):
        """rc=2 is "some hosts failed" -- a real result the terminal mapping
        (#1973) reads, not a transport failure."""
        ee.rc = 2
        assert configure.run(cfg, work_dir=tree, inventory=_INV) == 2

    def test_rc_four_is_returned_unmolested_because_it_is_overloaded(self, cfg, tree, ee):
        """`4` is BOTH "hosts unreachable" and "playbook malformed". The
        discriminator is the stream and the PLAY RECAP, not the status, so this
        phase reports the code rather than interpreting it."""
        ee.rc = 4
        assert configure.run(cfg, work_dir=tree, inventory=_INV) == 4


class TestTheGalaxyTokenDoesNotReachThePlaybook:
    """#2154 item 9: the token crosses a container boundary, and only one way.

    `ansible-galaxy` needs it to authenticate against Terrapod's own registry.
    `ansible-playbook` must never see it: a playbook is the operator's code, it
    runs in the same container, and a credential in its environment is one it can
    read and exfiltrate. The two invocations are separate `/exec` calls with
    separate environments, which is what makes the boundary real rather than a
    convention -- so it is worth asserting, because the cheap mistake is to build
    one environment and reuse it.
    """

    def test_the_playbook_environment_carries_no_galaxy_credential(self, cfg, tree, ee):
        configure.run(cfg, work_dir=tree, inventory=_INV)
        env = ee.calls[0]["env"]
        leaked = [k for k in env if "GALAXY" in k.upper() or "TOKEN" in k.upper()]
        assert leaked == [], f"the playbook was handed {leaked}"

    def test_nor_the_runner_token(self, cfg, tree, ee):
        """`TP_AUTH_TOKEN` is the run's own credential, which `exec_subprocess`
        already scrubs from an engine's environment for the same reason."""
        configure.run(cfg, work_dir=tree, inventory=_INV)
        env = ee.calls[0]["env"]
        assert not any(k.startswith("TP_") for k in env), sorted(env)
