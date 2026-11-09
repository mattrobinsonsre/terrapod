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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from terrapod.runner import ee_client
from terrapod.runner.phases import configure, opa
from terrapod.runner.phases.configure import ConfigureNotConfigured

#: Stands in for the materialised resolved inventory. A fixed path rather than
#: a tmp_path fixture because every assertion below is about the ARGUMENTS, and
#: `argv` never touches the file -- `configure_inventory` owns producing it and
#: has its own tests.
#: The `-i` list `materialise` returns. A LIST because that is its shape (#2033):
#: one entry in the ordinary case, several when the API handed the repository's
#: own sources over for this run to resolve.
_INV = [Path("/tmp/terrapod-resolved-inventory.yml")]


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
        assert configure.argv(cfg, inventory=_INV) == ["-i", str(_INV[0]), "site.yml"]
        assert configure.argv(cfg, inventory=_INV).count("-i") == 1

    def test_several_sources_each_get_their_own_flag_in_order(self, cfg):
        """The dynamic handoff (#2033). Order is precedence order in ansible, so
        this must pass them in the order given and reorder nothing."""
        handed = [Path("/tp/x/10_static.yml"), Path("/tp/x/30_aws.yml"), Path("/tp/declared.yml")]
        assert configure.argv(cfg, inventory=handed) == [
            "-i",
            "/tp/x/10_static.yml",
            "-i",
            "/tp/x/30_aws.yml",
            "-i",
            "/tp/declared.yml",
            "site.yml",
        ]

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
            extra_env={"ANSIBLE_INVENTORY_ENABLED": "auto", "ANSIBLE_FORCE_COLOR": "0"},
        )
        assert ee.calls[0]["env"]["ANSIBLE_INVENTORY_ENABLED"] == "yaml,ini"
        # The operator passes "0" and the forced set says "1" (#2413 F18), so
        # the override is still OBSERVABLE here. Passing the same value the
        # forced set holds would make this assertion pass without testing the
        # merge order it exists for.
        assert ee.calls[0]["env"]["ANSIBLE_FORCE_COLOR"] == "1"

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

    def test_the_cloud_identity_phase_is_called_at_all(self):
        """The whole of #2259's first half. `_run_configure_phase` returns from
        step 7c, BEFORE the entrypoint reaches `cloud_identity.run` at step 10,
        so for as long as the configure phase has existed nothing minted a
        token for one -- a federated workspace ran its playbook with the agent
        pool's identity and nothing said so.
        """
        fn = self._function()
        # A CALL, not a mention. `"cloud_identity" in ast.dump(fn)` passes on
        # the strength of this function's own DOCSTRING, which names
        # `cloud_identity.TOKEN_DIR` to explain why the tokens go elsewhere --
        # so that form survived deleting the call outright. Found by reading
        # which tests a mutation killed rather than how many.
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "cloud_identity"
        ]
        assert len(calls) == 1, (
            "nothing mints this configure's cloud identity, so a federated "
            "workspace silently falls through to the agent pool's"
        )

    def test_it_is_asked_NOT_to_discover(self):
        """`discover_providers=False`, as a literal.

        A playbook has no provider graph to walk, and the keyword is also what
        the API keys its own non-discovering branch on. Asserting the keyword
        merely EXISTS would be satisfied by `discover_providers=True`, which is
        the defect restored -- so the value is read.
        """
        call = self._call(fn=self._function(), module="cloud_identity", func_attr="run")
        passed = {k.arg: k.value for k in call.keywords}
        assert "discover_providers" in passed, "the configure path asks the engine to discover"
        value = passed["discover_providers"]
        assert isinstance(value, ast.Constant) and value.value is False, (
            "expected the literal False; a playbook has no provider graph to walk"
        )

    def test_the_tokens_GO_TO_THE_SHARED_VOLUME(self):
        """The second half, and the one no behavioural test can see.

        Without `token_dir` the phase writes to `cloud_identity.TOKEN_DIR`,
        which is an emptyDir mounted into the ORCHESTRATOR alone. Ansible runs
        in the execution environment, which mounts only the shared volume and
        the workspace -- so the token would be minted, delivered, logged as
        delivered, and unreadable by the one process that needs it. Every
        assertion about minting would still pass.
        """
        call = self._call(fn=self._function(), module="cloud_identity", func_attr="run")
        passed = {k.arg: k.value for k in call.keywords}
        assert "token_dir" in passed, (
            "the tokens land at the engine's directory, which the execution "
            "environment does not mount"
        )
        # The resolver, not a literal: the path is a property of the Job's
        # volume layout, so a literal here would be a second place to change it.
        assert "ee_token_dir" in ast.unparse(passed["token_dir"]), (
            "expected cloud_identity.ee_token_dir(), which reads TP_EE_OIDC_DIR"
        )

    def test_the_identity_environment_is_PASSED_to_the_configure(self):
        """The third half. Minting and delivering a token the playbook's
        environment never learns about leaves it findable only by hard-coding a
        path -- and `TERRAPOD_OIDC_TOKEN_DIR` is what the documentation tells an
        operator to read, precisely because a configure's directory differs
        from the engine's.

        Same shape as the vault assertion below: the name the phase's return
        was bound to has to REACH the `extra_env` expression. Merged rather
        than replacing, because three concerns now contribute to it.
        """
        fn = self._function()
        configure_call = self._call(fn=fn, module="configure", func_attr="run")
        passed = {k.arg: k.value for k in configure_call.keywords}
        assert "extra_env" in passed

        bound = {
            t.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and "cloud_identity" in ast.dump(node.value)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        assert bound, "cloud_identity.run()'s return is not bound to anything"
        reached = ast.unparse(passed["extra_env"])
        assert any(name in reached for name in bound), (
            f"none of {sorted(bound)} reaches extra_env ({reached!r}), so the token "
            "directory never reaches the playbook's environment"
        )

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

    def test_the_policy_gate_runs_before_anything_executes(self):
        """#2435. A configure is a SINGLE Job -- it is the action -- so there is
        no between-Jobs moment for the API to hold it at, the way it holds a
        plan before an apply. The gate is therefore in the Job, and the only
        thing that makes it a gate rather than a report is that it runs first.

        Ordering by line number over the function's own AST, because there is no
        seam in the middle of `_run_configure_phase` to observe it at -- the same
        shape as the `db.flush()`-inside-`try` gate.
        """
        fn = self._function()
        gate = self._call(fn=fn, module="opa", func_attr="evaluate_policies")
        play = self._call(fn=fn, module="configure", func_attr="run")
        install = self._call(fn=fn, module="ansible_collections", func_attr="install")

        assert gate.lineno < play.lineno, (
            "the playbook runs before the policy gate, so a denied configure "
            "has already changed hosts by the time anything refuses it"
        )
        assert gate.lineno < install.lineno, (
            "a denied configure installs collections first -- it should do no "
            "work beyond resolving its own inventory"
        )

    def test_the_gate_reads_the_policy_input_and_not_the_rendered_inventory(self):
        """The two must name the SAME file, and it must not be the `-i` source.

        `materialise` substitutes `${terrapod:...}` placeholders in the RENDERED
        inventory and nowhere else, so handing OPA that file would put a
        delivered password into the policy input -- and from there into any
        `deny` message a policy chose to emit. Asserting only that `plan_json=`
        is passed would be satisfied by passing the rendered path.
        """
        fn = self._function()
        gate = self._call(fn=fn, module="opa", func_attr="evaluate_policies")
        materialise = self._call(fn=fn, module="configure_inventory", func_attr="materialise")

        gate_input = {k.arg: k.value for k in gate.keywords}.get("plan_json")
        assert isinstance(gate_input, ast.Name), (
            "the gate's input should be the module constant, not an expression"
        )
        kept = {k.arg: k.value for k in materialise.keywords}.get("policy_input")
        assert isinstance(kept, ast.Name), "materialise is not asked to keep the policy input"
        assert kept.id == gate_input.id, (
            f"materialise writes {kept.id} and the gate reads {gate_input.id} -- "
            "they must be the same file"
        )
        # And that file is NOT what the playbook is pointed at.
        play = self._call(fn=fn, module="configure", func_attr="run")
        inv = {k.arg: k.value for k in play.keywords}.get("inventory")
        assert not (isinstance(inv, ast.Name) and inv.id == gate_input.id), (
            "the policy input and the playbook's inventory are the same object; "
            "the rendered inventory carries resolved secrets"
        )


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


class TestTheGateActuallyBlocks:
    """A mandatory deny must STOP the configure, not merely be recorded.

    The gate shipped inverted: `evaluate_policies` returned a count and raised
    only on bundle-fetch or results-POST failure, so it failed CLOSED on an
    infrastructure fault and OPEN on an actual policy denial. A mandatory set
    could deny, the row be POSTed and shown in the UI, and the playbook run
    against the whole fleet anyway -- a failed mandatory policy set sitting
    beside a successful configure.

    The only guarding test asserted LINE ORDERING, and its own docstring said
    "the only thing that makes it a gate rather than a report is that it runs
    first". That is true of the implementation and nowhere near sufficient:
    running first is necessary, and refusing is the point.

    The verdict is the API's, never recomputed here -- "blocking" means a
    mandatory set that failed or errored and was not overridden, and that rule
    lives in `policy_set_service.run_is_policy_blocked`. The runner POSTs its
    results and is told. These tests drive `evaluate_policies` against a fake
    transport so the decision, not its shape, is what is asserted.
    """

    @staticmethod
    def _cfg(tmp_path):
        return SimpleNamespace(
            has_api=True,
            api_url="http://api",
            run_id="abc",
            auth_token="t",
            upload_timeout_seconds=30,
        )

    @staticmethod
    def _client(*, blocked: bool):
        """A transport that serves one mandatory set and reports the verdict."""
        posted: list[dict] = []

        class _Resp:
            def __init__(self, code, payload):
                self.status_code = code
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def get(self, url, **_kw):
                return _Resp(
                    200,
                    {
                        "policy_sets": [
                            {
                                "id": "polset-1",
                                "name": "prod-guardrails",
                                "enforcement_level": "mandatory",
                                "policies": [{"name": "p", "rego": "package terrapod"}],
                                "shared_evaluation": False,
                                "support_files": {},
                            }
                        ],
                        "context": {},
                    },
                )

            def post(self, url, json=None, **_kw):
                posted.append(json or {})
                return _Resp(201, {"recorded": 1, "blocked": blocked})

            def close(self):
                pass

        return _Client(), posted

    def test_a_blocking_verdict_refuses_when_the_phase_enforces(self, tmp_path):
        client, posted = self._client(blocked=True)
        with patch.object(opa, "evaluate_set", return_value={"policy_set_id": "polset-1"}):
            with pytest.raises(opa.PolicyGateBlocked):
                opa.evaluate_policies(
                    self._cfg(tmp_path),
                    plan_json=tmp_path / "input.json",
                    work_dir=tmp_path / "work",
                    enforce=True,
                    opa_binary="/bin/true",
                    client=client,
                )
        # The results are POSTed BEFORE the refusal, so the UI shows which set
        # denied beside the failed run rather than only that it failed.
        assert posted, "the refusal must not discard the evidence"

    def test_a_blocking_verdict_does_NOT_refuse_when_the_phase_does_not_enforce(self, tmp_path):
        """The plan phase, which the API holds between Jobs.

        Paired with the test above deliberately: a gate that raised
        unconditionally would pass that one and break every plan on a denying
        policy, losing the plan artifact and the override path with it.
        """
        client, _posted = self._client(blocked=True)
        with patch.object(opa, "evaluate_set", return_value={"policy_set_id": "polset-1"}):
            count = opa.evaluate_policies(
                self._cfg(tmp_path),
                plan_json=tmp_path / "input.json",
                work_dir=tmp_path / "work",
                enforce=False,
                opa_binary="/bin/true",
                client=client,
            )
        assert count == 1

    def test_a_passing_verdict_refuses_nothing(self, tmp_path):
        client, _posted = self._client(blocked=False)
        with patch.object(opa, "evaluate_set", return_value={"policy_set_id": "polset-1"}):
            count = opa.evaluate_policies(
                self._cfg(tmp_path),
                plan_json=tmp_path / "input.json",
                work_dir=tmp_path / "work",
                enforce=True,
                opa_binary="/bin/true",
                client=client,
            )
        assert count == 1

    def test_an_api_that_sends_no_verdict_is_not_treated_as_blocking(self, tmp_path):
        """Skew: an older API has no `blocked` field.

        Absent reads as False, matching what that API does -- it holds the run
        itself for a plan and had no configure gate at all -- rather than
        failing every configure against it.
        """
        client, _posted = self._client(blocked=False)

        class _NoVerdict(type(client)):
            def post(self, url, json=None, **_kw):
                class _R:
                    status_code = 201

                    @staticmethod
                    def json():
                        return {"recorded": 1}

                return _R()

        with patch.object(opa, "evaluate_set", return_value={"policy_set_id": "polset-1"}):
            count = opa.evaluate_policies(
                self._cfg(tmp_path),
                plan_json=tmp_path / "input.json",
                work_dir=tmp_path / "work",
                enforce=True,
                opa_binary="/bin/true",
                client=_NoVerdict(),
            )
        assert count == 1

    def test_every_call_site_states_whether_it_enforces(self):
        """`enforce` is required, so no phase can acquire a reporting gate by
        omission -- and the three sites' answers are pinned here because the
        right answer is a property of the phase, not of OPA."""
        import ast
        import pathlib

        from terrapod.runner import job_entrypoint

        tree = ast.parse(pathlib.Path(job_entrypoint.__file__).read_text())
        answers = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "evaluate_policies"
            ):
                kw = {k.arg: k.value for k in node.keywords}
                assert "enforce" in kw, "a call site omits `enforce`"
                answers.append(ast.literal_eval(kw["enforce"]))
        # plan, configure, pulumi-preview -- exactly one enforces.
        assert len(answers) == 3, answers
        assert sum(1 for a in answers if a) == 1, answers
