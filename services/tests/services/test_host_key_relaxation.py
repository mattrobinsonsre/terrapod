"""A workspace's host-key relaxation reaches both consumers, from one producer.

The setting is deliberately NOT an environment variable, and that is the whole
property worth pinning. `ANSIBLE_HOST_KEY_CHECKING` is per-process: forcing it
would override the operator's own `ansible.cfg` -- which this project reserves
for non-negotiables -- and would sit ABOVE a per-host override rather than below
it, making the per-host exception the setting exists to permit unreachable. So it
is expressed as `ansible_ssh_common_args` on the `all` group, where an inventory
host variable still beats it.

Measured against the published execution-environment image and a real sshd
before any of this was written (#2318):

* first contact with an unseen host is REFUSED -- `Host key verification failed`,
  rc=4, `UNREACHABLE!`. It does not hang (the agent gives ansible a blocking pipe
  and no TTY, so ssh never reaches a prompt) and does not silently trust.
* the exact string this producer emits, as a variable on `all`, connects -- and
  the same inventory WITHOUT it is refused, which is the control that makes the
  variable rather than something else in the fixture the thing that mattered.
* a host variable REPLACES a group `all` variable rather than merging, so a host
  may re-enable verification over a workspace-level relaxation -- at the cost of
  restating the whole argument string, which the docs say.

Those three are gated by `scripts/ssh-target-gate.sh`, which drives real ansible
against a real sshd in CI. This file pins the part that gate cannot see: that the
relaxation is produced ONCE and reaches BOTH places it has to.
"""

from __future__ import annotations

import ast
import inspect
import types

from terrapod.services import inventory_resolve as ir


class TestTheProducer:
    """Five cases, because the interesting ones are the two that do nothing."""

    def test_verification_on_emits_nothing(self):
        declared: dict = {"global_vars": {}}
        ir._relax_host_key_checking(types.SimpleNamespace(ansible_host_key_checking=True), declared)
        assert declared["global_vars"] == {}

    def test_verification_off_emits_the_relaxation(self):
        declared: dict = {"global_vars": {}}
        ir._relax_host_key_checking(
            types.SimpleNamespace(ansible_host_key_checking=False), declared
        )
        assert declared["global_vars"][ir._HOST_KEY_VAR] == ir._RELAXED_HOST_KEY_ARGS

    def test_an_absent_workspace_leaves_verification_on(self):
        """The safe direction for a row we could not read."""
        declared: dict = {"global_vars": {}}
        ir._relax_host_key_checking(None, declared)
        assert declared["global_vars"] == {}

    def test_the_operators_own_declaration_wins(self):
        """Theirs is the more specific statement, so it is used unchanged.

        A setting that silently does nothing is worse than one that refuses,
        which is why the producer logs this rather than overwriting in silence.
        """
        theirs = "-o UserKnownHostsFile=/theirs"
        declared: dict = {"global_vars": {ir._HOST_KEY_VAR: theirs}}
        ir._relax_host_key_checking(
            types.SimpleNamespace(ansible_host_key_checking=False), declared
        )
        assert declared["global_vars"][ir._HOST_KEY_VAR] == theirs

    def test_a_document_with_no_global_vars_key_still_gets_it(self):
        """`_declared_document` always supplies the key; this is the guard for a
        caller that does not, so the producer cannot raise on a shape it is
        handed rather than one it built."""
        declared: dict = {}
        ir._relax_host_key_checking(
            types.SimpleNamespace(ansible_host_key_checking=False), declared
        )
        assert declared["global_vars"][ir._HOST_KEY_VAR] == ir._RELAXED_HOST_KEY_ARGS


class TestTheRelaxationIsNotAnEnvironmentVariable:
    """The design decision, asserted where a reader will meet it.

    If someone later "simplifies" this into a forced env var, the per-host
    override stops working and nothing else in the suite notices -- the
    workspace-level behaviour would look identical.
    """

    def test_the_configure_phase_forces_no_host_key_setting(self):
        """Driven through `_forced_env`, not read off `_FORCED_ENV`.

        Measured: the literal dict holds three keys while the function returns
        six -- it adds `ANSIBLE_INVENTORY_ENABLED` and a conditional
        `ANSIBLE_FORKS`, and passes three temp-directory variables through. So an
        assertion on the literal has a blind spot exactly where a future setting
        would most likely be added, and this test had it until the forced-env
        documentation gate derived both halves and showed the gap.
        """
        import types

        from terrapod.runner.phases import configure

        cfg = types.SimpleNamespace(
            parallelism=4,
            dynamic_inventory_enabled=False,
            inventory_scripts_enabled=False,
        )
        forced = set(configure._forced_env(cfg))
        assert len(forced) > len(configure._FORCED_ENV), (
            "this test exists because the function returns more than the literal; "
            "if that is no longer true it is reading the wrong thing"
        )
        assert not any("HOST_KEY" in name for name in forced), (
            "the configure phase must force no host-key setting: it is per-process, "
            "so it would override the operator's own ansible.cfg and sit above the "
            f"per-host override rather than below it. Forced: {sorted(forced)}"
        )

    def test_the_relaxation_is_an_inventory_variable(self):
        assert ir._HOST_KEY_VAR == "ansible_ssh_common_args"

    def test_it_disables_the_known_hosts_file_too(self):
        """Without `UserKnownHostsFile=/dev/null` ssh warns on every connection
        and writes the key into a per-run `emptyDir` that is discarded anyway --
        a line in the operator's log for something they asked for."""
        assert "StrictHostKeyChecking=no" in ir._RELAXED_HOST_KEY_ARGS
        assert "UserKnownHostsFile=/dev/null" in ir._RELAXED_HOST_KEY_ARGS


class TestOneProducerReachesBothConsumers:
    """The positional invariant, which no behavioural test here can see.

    `_resolve_uncached` hands the declared document to TWO places -- the platform
    source the API's own `ansible-inventory` reads, and the document handed to the
    runner. Injecting at either call site alone would have left the other path
    quietly unrelaxed, and a test of the producer alone cannot tell the
    difference. Driving the real function instead would need a database, a
    repository fetch and an ansible binary.
    """

    @staticmethod
    def _call_names(fn) -> list[str]:
        tree = ast.parse(inspect.getsource(fn).lstrip())
        return [
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]

    def test_the_producer_is_called(self):
        assert "_relax_host_key_checking" in self._call_names(ir._resolve_uncached)

    def test_it_is_called_before_either_consumer(self):
        """Order matters and the file is the only place it is visible.

        Asserted on line offsets within the function rather than on text, so
        reformatting cannot silently satisfy it.
        """
        src = inspect.getsource(ir._resolve_uncached)
        tree = ast.parse(src.lstrip())
        pos: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pos.setdefault(node.func.id, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                pos.setdefault(node.func.attr, node.lineno)

        assert "_relax_host_key_checking" in pos, "the producer is not called at all"
        assert "render_inventory_yaml" in pos, "the platform source is no longer rendered here"
        assert "build_inventory_document" in pos, "the runner handoff is no longer built here"

        assert pos["_relax_host_key_checking"] < pos["render_inventory_yaml"], (
            "the relaxation must be applied BEFORE the platform source is rendered, "
            "or the API's own ansible-inventory run reads an unrelaxed document"
        )
        assert pos["_relax_host_key_checking"] < pos["build_inventory_document"], (
            "the relaxation must be applied BEFORE the runner handoff is built, or "
            "the runner executes against an unrelaxed document while the API's "
            "read of the same workspace shows it relaxed"
        )

    def test_both_consumers_receive_the_same_object(self):
        """One producer, so both consumers must be handed the SAME name.

        If one of them were rebuilt from a second `_declared_document` call, the
        ordering assertions above would still pass and the relaxation would reach
        only one path.
        """
        src = inspect.getsource(ir._resolve_uncached)
        tree = ast.parse(src.lstrip())
        handed: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {"render_inventory_yaml", "build_inventory_document"}:
                continue
            handed[name] = {
                kw.value.id
                for kw in node.keywords
                if kw.arg is None and isinstance(kw.value, ast.Name)
            }
        assert handed.get("render_inventory_yaml"), "the platform render takes no **declared"
        assert handed.get("build_inventory_document"), "the handoff takes no **declared"
        assert handed["render_inventory_yaml"] == handed["build_inventory_document"], (
            "both consumers must be handed the same document object, or a second "
            f"build would miss the relaxation: {handed}"
        )

    def test_the_declared_document_is_built_exactly_once(self):
        """A second build is how one of the two paths comes to miss it."""
        calls = self._call_names(ir._resolve_uncached)
        assert calls.count("_declared_document") == 1, (
            "the declared document must be built once and mutated once; a second "
            f"build would bypass the producer. Found {calls.count('_declared_document')}"
        )


class TestTheCredentialFieldIsNotTreatedAsASecret:
    """A host's PUBLIC key is public, and redacting it destroys a log.

    The runner's file loop registered every `MACHINE_FILE_FIELDS` value with the
    log redactor, which was correct while `private_key` was the only file field.
    Adding `known_hosts` made it wrong: the redactor would replace, in the run's
    own log, the one value an operator needs to read when a host key does not
    match -- in exchange for hiding nothing.
    """

    def test_known_hosts_is_a_file_field(self):
        from terrapod.services.ansible_auth import MACHINE_FILE_FIELDS

        assert "known_hosts" in MACHINE_FILE_FIELDS

    def test_known_hosts_is_not_a_secret_field(self):
        from terrapod.services.ansible_auth import MACHINE_SECRET_FIELDS

        assert "known_hosts" not in MACHINE_SECRET_FIELDS, (
            "a host's public key must not be registered with the log redactor"
        )

    def test_the_private_key_is_still_a_secret(self):
        """The control. Without it, satisfying the assertion above by emptying the
        secret set entirely would pass."""
        from terrapod.services.ansible_auth import MACHINE_SECRET_FIELDS

        assert "private_key" in MACHINE_SECRET_FIELDS

    def test_the_runner_registers_only_the_secret_file_fields(self):
        """Source-introspected because the alternative is asserting on a redactor
        side effect, and the defect is a missing CONDITION rather than a wrong
        value -- the unconditional call produced correct behaviour for every
        input that existed before `known_hosts` did.
        """
        from terrapod.runner.phases import ansible_auth as phase

        src = inspect.getsource(phase._apply_machine)
        tree = ast.parse(src.lstrip())

        # Scoped to the FILE-field loop, which is the whole point. There is a
        # second `if field in MACHINE_SECRET_FIELDS:` in the VALUE-field loop
        # below it, also guarding a `register_secret` -- so an unscoped search
        # finds that one and passes while the file-field call is unguarded.
        # Caught by mutation: unguarding the file-field call left this green.
        file_loops = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.For) and "MACHINE_FILE_FIELDS" in ast.dump(n.iter)
        ]
        assert len(file_loops) == 1, (
            f"expected exactly one loop over MACHINE_FILE_FIELDS, found {len(file_loops)} -- "
            "this test can no longer tell which call it is scoped to"
        )
        loop = file_loops[0]

        registers = [
            c
            for c in ast.walk(loop)
            if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "register_secret"
        ]
        assert registers, (
            "the file-field loop no longer registers anything with the redactor; a "
            "private key reaching the log unredacted is the defect in the other direction"
        )

        guarded = [
            n
            for n in ast.walk(loop)
            if isinstance(n, ast.If)
            and "MACHINE_SECRET_FIELDS" in ast.dump(n.test)
            and any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "register_secret"
                for c in ast.walk(n)
            )
        ]
        assert len(guarded) == len(registers), (
            f"{len(registers)} register_secret call(s) in the file-field loop but "
            f"{len(guarded)} guarded on MACHINE_SECRET_FIELDS -- an unguarded one "
            "redacts a host's public key out of the operator's own log"
        )
