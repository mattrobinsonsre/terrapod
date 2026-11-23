"""Two host-key mechanisms are retired, and each retirement is guarded (#2592).

This file was `test_host_key_relaxation.py` and most of it had no subject left.
The relaxation it pinned -- `_relax_host_key_checking`, which emitted
`ansible_ssh_common_args` on the `all` group when the workspace's boolean was
false -- is gone with the boolean, along with `_HOST_KEY_VAR` and
`_RELAXED_HOST_KEY_ARGS`. Nine tests drove those three names directly, so they
were deleted rather than pointed at something adjacent: a test of a mechanism
that no longer exists cannot be made to pass without changing what it asserts,
and changing what it asserts is writing a new test under an old name.

Specifically deleted, and why each is not a loss:

* `TestTheProducer` (five tests). The producer is gone. The property it guarded
  -- that a workspace can relax verification -- survives as
  `ssh_host_key_policy = "off"`, delivered through the run's ssh anchor and
  covered in `test_ssh_host_key_policy.py`.
* `test_the_relaxation_is_an_inventory_variable`. It pinned `_HOST_KEY_VAR`,
  which this change deleted: the last thing that read it was this test, and
  `configure_known_hosts._statements_about_host_keys` names that variable from
  its own literal tuple. A constant kept alive by the test that asserts it is a
  constant nothing reads.
* `test_it_disables_the_known_hosts_file_too`. It pinned
  `UserKnownHostsFile=/dev/null` in the relaxed argument string. That property
  MOVED rather than died -- `ssh_args(policy="off")` points at `/dev/null` now --
  and `test_ssh_host_key_policy.py` asserts it there.
* `TestOneProducerReachesBothConsumers` (four tests). It existed because one
  producer MUTATED the declared document and two consumers read it, so injecting
  at either call site alone would have left the other path unrelaxed. Measured
  after the retirement: `_resolve_uncached` builds the document once and hands it
  to both consumers with nothing mutating it in between, so there is no mutation
  for a second build to miss. The positional assertions would still pass and
  would be guarding a refactoring preference rather than a defect.

What survives is the half that is about the SHAPE the policy must not take, plus
a structural guard that the retired relaxation cannot come back -- and that guard
is not housekeeping. A reinstated relaxation would put
`ansible_ssh_common_args` on `all` in every resolved inventory; collapsed into
each host's variables, `refuse_conflicting_variables` then fires on a workspace
that has approved keys, so every configure on every keyed workspace would be
refused outright. The cause would be an inventory variable nobody wrote.

`TestTheCredentialFieldIsRetired` is untouched: `known_hosts` on a machine
credential is a different retirement from the same issue.
"""

from __future__ import annotations

import ast
import inspect

from terrapod.services import inventory_resolve as ir


class TestThePolicyIsNotAnAnsibleFlag:
    """The design decision, asserted where a reader will meet it.

    The reason has changed with the mechanism and the conclusion has not. It used
    to be that forcing `ANSIBLE_HOST_KEY_CHECKING` would sit ABOVE a per-host
    override and make the exception the setting existed to permit unreachable.
    The anchor is now deliberately unbeatable, so that argument is spent -- and
    two better ones replace it:

    * **ansible's flag is a BOOLEAN.** It cannot express `accept-new` at all, so
      forcing it would collapse a three-state setting onto two states and quietly
      lose the one the whole issue exists to add. A workspace set to `accept-new`
      would enforce either `yes` or `no`, and the UI would say otherwise.
    * it is per-process, so it would override the operator's own `ansible.cfg` --
      which this project reserves for non-negotiables.

    The policy rides `ANSIBLE_SSH_ARGS` instead, where OpenSSH's own
    `StrictHostKeyChecking=accept-new` can be stated.
    """

    def test_the_configure_phase_forces_no_host_key_setting(self):
        """Driven through `_forced_env`, not read off `_FORCED_ENV`.

        Measured: the literal dict holds fewer keys than the function returns --
        it adds `ANSIBLE_INVENTORY_ENABLED` and a conditional `ANSIBLE_FORKS`, and
        passes three temp-directory variables through. So an assertion on the
        literal has a blind spot exactly where a future setting would most likely
        be added, and this test had it until the forced-env documentation gate
        derived both halves and showed the gap.
        """
        import types

        from terrapod.runner.phases import configure

        cfg = types.SimpleNamespace(
            parallelism=4,
            configure_parallelism=4,
            dynamic_inventory_enabled=False,
            inventory_scripts_enabled=False,
        )
        forced = set(configure._forced_env(cfg))
        assert len(forced) > len(configure._FORCED_ENV), (
            "this test exists because the function returns more than the literal; "
            "if that is no longer true it is reading the wrong thing"
        )
        assert not any("HOST_KEY" in name for name in forced), (
            "the configure phase must force no host-key setting: ansible's flag is a "
            "boolean and cannot say `accept-new`, so forcing it would collapse the "
            "three-state policy onto two -- and it is per-process, so it would also "
            f"override the operator's own ansible.cfg. Forced: {sorted(forced)}"
        )


class TestTheInventoryRelaxationIsRetired:
    """It cannot come back by accident, and the failure if it did is severe.

    Structural because the defect would be a RE-ADDED statement rather than a
    wrong value, and no behavioural test of the resolver can see it: a resolved
    inventory carrying `ansible_ssh_common_args` on `all` looks entirely ordinary
    on its own. It is the interaction with the anchor that breaks -- see the
    module docstring.
    """

    @staticmethod
    def _names_in(fn) -> set[str]:
        tree = ast.parse(inspect.getsource(fn).lstrip())
        return {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute | ast.Name)
        }

    def test_the_producer_is_gone_from_the_module(self):
        """Absent as a module attribute, so a re-import is caught as well as a
        re-definition."""
        for name in ("_relax_host_key_checking", "_RELAXED_HOST_KEY_ARGS", "_HOST_KEY_VAR"):
            assert not hasattr(ir, name), (
                f"`{name}` is back. The relaxation it belongs to is retired: the policy "
                "rides the run's ssh anchor, and a variable on `all` would contradict it"
            )

    def test_the_resolver_calls_nothing_that_relaxes_host_keys(self):
        """The half the attribute check cannot see: a statement written INLINE.

        The relaxation arrived the first time as a helper, so its return would be
        caught above. Re-adding it as two lines in `_resolve_uncached` would not.
        """
        names = self._names_in(ir._resolve_uncached)
        offenders = sorted(n for n in names if "host_key" in n.lower())
        assert offenders == [], (
            "`_resolve_uncached` names something host-key-shaped again: "
            f"{offenders}. The resolved inventory must state no host-key policy -- "
            "a workspace with approved keys would then have its own inventory "
            "contradict its anchor, and every configure on it would be refused"
        )

    def test_no_host_key_variable_is_written_into_the_declared_document(self):
        """And the source of the whole module, not just the one function.

        Scoped to the three variables ansible reads for this, by name, because the
        module legitimately discusses them in prose -- so the assertion is that
        none of them is ASSIGNED anywhere, which is what putting one back requires.
        """
        tree = ast.parse(inspect.getsource(ir))
        assigned: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                # A module constant holding the name, as `_HOST_KEY_VAR` did.
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
                # And `global_vars["<var>"] = ...`, which is how the relaxation
                # actually wrote it -- a literal subscript, never a bare name.
                elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                    assigned.add(str(target.slice.value))
        relaxing = {
            "ansible_ssh_common_args",
            "ansible_ssh_extra_args",
            "ansible_host_key_checking",
        }
        assert not (assigned & relaxing), (
            "the resolver assigns an ansible host-key variable again: "
            f"{sorted(assigned & relaxing)}"
        )


class TestTheCredentialFieldIsRetired:
    """`known_hosts` on a machine credential is retired (#2592).

    It was added by #2318 for a real problem and the shape was wrong: a CREDENTIAL
    holding nothing secret, one opaque blob per credential with no way to say which
    host a key belongs to and nowhere to put a marker, and an operator pasting a
    placeholder into an inventory variable so the platform's trust anchor was only
    as good as that one line of someone else's YAML. `inventory_host_keys` replaces
    it per host, per key type, and the platform renders the file itself.

    The class that was here guarded the field's one awkward property -- a file
    field that is NOT a secret, which the runner had to stop registering with the
    log redactor. Most of that is now vacuous, so what survives is the retirement's
    own asymmetry plus the structural invariant, which is latent rather than
    pointless (see the last test).
    """

    def test_known_hosts_is_no_longer_a_field(self):
        from terrapod.services.ansible_auth import MACHINE_FIELDS

        assert "known_hosts" not in MACHINE_FIELDS

    def test_it_is_retired_rather_than_merely_absent(self):
        """The distinction that matters, and the whole lesson of #2425 §0.3.

        An absent field is REFUSED as unknown on read, and `delivery_entries`
        catches that refusal and skips the credential -- so a credential still
        carrying `known_hosts` would silently stop delivering its PRIVATE KEY too,
        and the operator's only signal would be a wholly unreachable fleet naming
        neither the credential nor the field. Listing it as retired is what makes
        the read tolerate it.
        """
        from terrapod.services.ansible_auth import RETIRED_MACHINE_FIELDS

        assert "known_hosts" in RETIRED_MACHINE_FIELDS

    def test_a_legacy_envelope_still_delivers_its_private_key(self):
        """The half the test above cannot prove: tolerated on READ.

        This is the actual failure mode, driven rather than asserted about a set.
        """
        import json

        from terrapod.services import ansible_auth as aa

        legacy = json.dumps({"private_key": "KEY", "known_hosts": "h.example.com ssh-rsa AAA"})
        parsed = aa.validate_envelope(aa.MACHINE_CATEGORY, "web-key", legacy, tolerate_retired=True)

        assert parsed == {"private_key": "KEY"}, (
            "the retired field must be dropped and the private key must survive"
        )

    def test_it_is_refused_on_a_write(self):
        """The other half of the asymmetry. A test of either half alone passes
        whichever way the code is written, which is why both are here."""
        import json

        import pytest

        from terrapod.services import ansible_auth as aa

        with pytest.raises(aa.AnsibleAuthError) as exc:
            aa.validate_envelope(
                aa.MACHINE_CATEGORY,
                "web-key",
                json.dumps({"private_key": "KEY", "known_hosts": "h.example.com ssh-rsa AAA"}),
            )
        assert "known_hosts" in str(exc.value)

    def test_the_private_key_is_still_a_secret(self):
        """The control. Without it, the assertions above would be satisfied by a
        machine credential set that had been emptied entirely."""
        from terrapod.services.ansible_auth import MACHINE_FILE_FIELDS, MACHINE_SECRET_FIELDS

        assert "private_key" in MACHINE_SECRET_FIELDS
        assert "private_key" in MACHINE_FILE_FIELDS

    def test_the_runner_registers_only_the_secret_file_fields(self):
        """LATENT, and kept deliberately rather than deleted.

        With `known_hosts` gone, every member of `MACHINE_FILE_FIELDS` is also a
        secret, so the runner's condition is true for every input that exists and
        no current field exercises it. That makes this unexercised, NOT wrong: the
        invariant is "a file field is registered with the redactor only if it is
        secret", and the next non-secret file field would need it on the day it is
        added -- which is precisely when nobody would think to write it.

        Source-introspected because the defect it guards is a missing CONDITION
        rather than a wrong value, and an unconditional call produced correct
        behaviour for every input that existed before `known_hosts` did.

        The BEHAVIOURAL half of this pair is gone, and that is the reason this one
        is kept. It lived in `tests/runner/test_phase_ansible_auth.py` as
        `test_a_non_secret_field_is_never_registered_however_short`, and its
        subject had already been withdrawn once -- it was `user` until #2413 F9 --
        before `known_hosts` was withdrawn under it too. With no non-secret machine
        field left there is no value to drive it with, and a credential carrying
        only a retired field now raises before the assertion is reached. A test
        that cannot be written is not evidence the property is safe, so the
        structural one carries it alone until a non-secret file field exists again.
        """
        import ast
        import inspect

        from terrapod.runner.phases import ansible_auth as phase

        src = inspect.getsource(phase._apply_machine)
        tree = ast.parse(src.lstrip())
        names = {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute | ast.Name)
        }
        assert "MACHINE_SECRET_FIELDS" in names, (
            "the file loop must consult the secret set before registering a value "
            "with the log redactor; without it, a non-secret file field added later "
            "is redacted out of the operator's own run log"
        )
