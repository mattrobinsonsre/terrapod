"""The runner's Ansible credential phase (#1933).

Driven through `run()` rather than through its helpers wherever the property is
about delivery, because the recurring failure in this repo is a test that calls
the helper while nothing drives the caller: the fix is always "the phase now
returns X", and a test that hands X to a helper proves the helper formats it.

The class at the bottom drives REAL ansible over the rendered result. A shape
assertion on the document proves nothing about whether a credential scoped to a
group reaches that group's hosts and no others, which is the entire feature.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.phases import ansible_auth, configure_inventory
from terrapod.services.ansible_auth import MACHINE_CATEGORY, VAULT_CATEGORY

ANSIBLE = shutil.which("ansible-inventory")


@pytest.fixture(autouse=True)
def _isolated_credentials_dir(tmp_path, monkeypatch):
    """Point the shared directory at tmp_path.

    Without it the phase writes to `/tp/credentials`, which in a test process is
    a read-only filesystem -- an `OSError` naming `/tp` that reads as a defect
    in the phase rather than as the absence of a Job.
    """
    monkeypatch.setenv("TP_EE_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_leaked_secrets():
    """The redactor's registry is process-global, so a test that registers one
    would otherwise redact an unrelated test's output."""
    exec_subprocess._reset_secrets_for_tests()
    yield
    exec_subprocess._reset_secrets_for_tests()


def _auth_file(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "ansible-auth.json"
    path.write_text(json.dumps(entries))
    return path


def _vault(key: str, password: str) -> dict:
    return {"category": VAULT_CATEGORY, "key": key, "value": json.dumps({"password": password})}


def _machine(key: str, **fields) -> dict:
    return {"category": MACHINE_CATEGORY, "key": key, "value": json.dumps(fields)}


class TestAbsenceIsNotAFailure:
    def test_no_file_means_no_credentials_and_no_error(self, tmp_path):
        """The ordinary state of every plan and apply: the API sends the
        attribute only for a configure."""
        env, injections = ansible_auth.run(path=tmp_path / "nothing.json")
        assert env == {}
        assert injections == {}

    def test_an_empty_list_is_the_same(self, tmp_path):
        env, injections = ansible_auth.run(path=_auth_file(tmp_path, []))
        assert (env, injections) == ({}, {})


class TestDeclaredAndUnusableIsFatal:
    """#1442: warning and continuing spends the mistake part way through a fleet."""

    def test_an_unreadable_file_raises(self, tmp_path):
        path = tmp_path / "ansible-auth.json"
        path.write_text("{not json")
        with pytest.raises(ansible_auth.AnsibleAuthUnavailable, match="could not be read"):
            ansible_auth.run(path=path)

    def test_a_file_that_is_not_a_list_raises(self, tmp_path):
        path = tmp_path / "ansible-auth.json"
        path.write_text(json.dumps({"category": VAULT_CATEGORY}))
        with pytest.raises(ansible_auth.AnsibleAuthUnavailable, match="not a list"):
            ansible_auth.run(path=path)

    def test_a_vault_entry_with_no_password_raises(self, tmp_path):
        entries = [{"category": VAULT_CATEGORY, "key": "prod", "value": json.dumps({})}]
        with pytest.raises(ansible_auth.AnsibleAuthUnavailable, match="nothing to decrypt"):
            ansible_auth.run(path=_auth_file(tmp_path, entries))

    def test_every_failure_is_named_rather_than_the_first(self, tmp_path):
        """Fixing one of three and re-running to find the next is the experience
        this exists to avoid."""
        entries = [
            {"category": VAULT_CATEGORY, "key": "a", "value": json.dumps({})},
            {"category": MACHINE_CATEGORY, "key": "b", "value": json.dumps({})},
        ]
        with pytest.raises(ansible_auth.AnsibleAuthUnavailable) as exc:
            ansible_auth.run(path=_auth_file(tmp_path, entries))
        assert "'a'" in str(exc.value) and "'b'" in str(exc.value)

    def test_a_category_this_phase_does_not_handle_is_skipped_not_fatal(self, tmp_path):
        """A lagging runner meets a future category this way, and refusing would
        turn a forward-compatible delivery into a broken run."""
        entries = [
            _vault("prod", "a-long-enough-vault-password"),
            {"category": "future", "key": "x", "value": "{}"},
        ]
        env, _ = ansible_auth.run(path=_auth_file(tmp_path, entries))
        assert "ANSIBLE_VAULT_IDENTITY_LIST" in env


class TestTheVaultPassword:
    def test_one_password_becomes_one_labelled_identity(self, tmp_path):
        env, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_vault("prod", "pw-long-enough-to-register")])
        )
        assert injections == {}
        value = env["ANSIBLE_VAULT_IDENTITY_LIST"]
        label, _, path = value.partition("@")
        assert label == "prod"
        assert Path(path).read_text() == "pw-long-enough-to-register"

    def test_several_become_one_sorted_list(self, tmp_path):
        """Several vault IDs through ONE variable is how multiple IDs work at
        all. Sorted so the same credentials render the same value, which is what
        makes it assertable and keeps a log line stable between runs."""
        entries = [
            _vault("prod", "prod-password-long-enough"),
            _vault("dev", "dev-password-long-enough"),
        ]
        env, _ = ansible_auth.run(path=_auth_file(tmp_path, entries))
        labels = [part.split("@", 1)[0] for part in env["ANSIBLE_VAULT_IDENTITY_LIST"].split(",")]
        assert labels == ["dev", "prod"]

    def test_the_password_is_group_readable_and_nothing_wider(self, tmp_path):
        """`0640`, and the group bit is load-bearing rather than lax.

        `job_template._ee_security_context` sets NO `runAsUser` on the EE
        sidecar -- deliberately, so an operator-supplied image may run as its
        own uid -- and `fsGroup` is what grants that uid access to the shared
        volume. `fsGroup` works through the GROUP, which `0600` excludes. So
        `0600` would have worked only for an EE running as uid 1000, ours, and
        an operator's image on any other uid would have failed to read its own
        credentials after the configure had started.

        "Other" must still get nothing: the group is the pod's, so group-read
        means this Job's containers and nothing else.
        """
        env, _ = ansible_auth.run(
            path=_auth_file(tmp_path, [_vault("prod", "pw-long-enough-to-register")])
        )
        path = Path(env["ANSIBLE_VAULT_IDENTITY_LIST"].split("@", 1)[1])
        mode = path.stat().st_mode & 0o777
        assert mode == 0o640, f"expected 0o640, got {oct(mode)}"

    def test_the_private_key_has_the_same_mode(self, tmp_path):
        """ssh refuses a key it considers too permissive, so this is the mode
        that has to be right in both directions at once."""
        _, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("all", private_key="-----BEGIN-----\nk")])
        )
        path = Path(injections["all"]["ansible_ssh_private_key_file"])
        assert path.stat().st_mode & 0o777 == 0o640

    def test_no_credential_file_is_readable_by_other(self, tmp_path):
        """Derived over everything the phase wrote, so a future credential kind
        cannot be the one that lands world-readable."""
        entries = [
            _vault("prod", "pw-long-enough-to-register"),
            _machine(
                "all",
                private_key="-----BEGIN-----\nk",
                password="a-long-connection-password",
            ),
        ]
        ansible_auth.run(path=_auth_file(tmp_path, entries))
        written = sorted((tmp_path / "credentials").iterdir())
        assert written, "the phase wrote nothing, so this proves nothing"
        for path in written:
            assert not path.stat().st_mode & 0o007, f"{path.name} is readable by other"

    def test_it_is_registered_with_the_log_redactor(self, tmp_path):
        ansible_auth.run(path=_auth_file(tmp_path, [_vault("prod", "pw-long-enough-to-register")]))
        assert b"pw-long-enough-to-register" in exec_subprocess._SECRETS

    def test_a_short_password_still_works_though_it_cannot_be_redacted(self, tmp_path):
        """`register_secret` skips a value under its minimum because a short one
        would match ordinary output and mangle the log -- and an operator picks
        their own vault password. So the FILE is the protection, not the
        redactor, exactly as git auth states for tokens. The credential must
        still be delivered."""
        env, _ = ansible_auth.run(path=_auth_file(tmp_path, [_vault("prod", "short")]))
        path = Path(env["ANSIBLE_VAULT_IDENTITY_LIST"].split("@", 1)[1])
        assert path.read_text() == "short"
        assert b"short" not in exec_subprocess._SECRETS


class TestTheMachineCredential:
    def test_the_key_becomes_a_file_and_the_document_gets_its_PATH(self, tmp_path):
        """The distinction is the security property: the key never enters the
        inventory, only the path to it."""
        _, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("web", private_key="-----BEGIN-----\nkeymaterial")])
        )
        value = injections["web"]["ansible_ssh_private_key_file"]
        assert "keymaterial" not in json.dumps(injections)
        assert Path(value).read_text().startswith("-----BEGIN-----")

    def test_the_key_file_ends_in_a_newline(self, tmp_path):
        """ssh refuses a key without one as "invalid format", which names
        neither the key nor the missing byte."""
        _, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("web", private_key="-----BEGIN-----\nnotrailing")])
        )
        assert Path(injections["web"]["ansible_ssh_private_key_file"]).read_text().endswith("\n")

    def test_every_value_field_becomes_its_ansible_variable(self, tmp_path):
        _, injections = ansible_auth.run(
            path=_auth_file(
                tmp_path,
                [
                    _machine(
                        "web",
                        password="a-long-connection-password",
                        user="deploy",
                        become_method="sudo",
                        become_user="root",
                    )
                ],
            )
        )
        assert injections["web"] == {
            "ansible_password": "a-long-connection-password",
            "ansible_user": "deploy",
            "ansible_become_method": "sudo",
            "ansible_become_user": "root",
        }

    def test_become_arrives_as_a_boolean_not_a_string(self, tmp_path):
        """ "yes" as a string is truthy to Python and NOT a boolean to ansible's
        own templating, so the string form is the one that silently misbehaves."""
        _, injections = ansible_auth.run(
            path=_auth_file(
                tmp_path, [_machine("web", password="a-long-connection-password", become="yes")]
            )
        )
        assert injections["web"]["ansible_become"] is True

    def test_an_empty_scope_falls_back_to_every_host(self, tmp_path):
        _, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("", password="a-long-connection-password")])
        )
        assert set(injections) == {"all"}

    def test_the_secret_fields_are_registered_and_the_others_are_not(self, tmp_path):
        """A username is not a secret, and registering one would redact an
        ordinary word out of every line of the log."""
        ansible_auth.run(
            path=_auth_file(
                tmp_path,
                [
                    _machine(
                        "web",
                        password="a-long-connection-password",
                        user="deploy",
                        become_password="a-become-password-long-enough-to-register",
                    )
                ],
            )
        )
        assert b"a-long-connection-password" in exec_subprocess._SECRETS
        assert b"a-become-password-long-enough-to-register" in exec_subprocess._SECRETS
        assert b"deploy" not in exec_subprocess._SECRETS

    def test_a_scope_cannot_escape_its_directory(self, tmp_path):
        """Belt to the write guard's braces: a row written before the guard
        existed, or a future rule that widens, must not name `../`."""
        _, injections = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("../../etc/evil", private_key="k")])
        )
        written = Path(next(iter(injections.values()))["ansible_ssh_private_key_file"])
        assert written.parent == (tmp_path / "credentials")


@pytest.mark.skipif(ANSIBLE is None, reason="needs a real ansible-inventory on PATH")
class TestRealAnsibleResolvesTheInjectedInventory:
    """The behaviour, not the shape.

    Everything above asserts what the phase RETURNS. These assert what ansible
    does with it, which is the only form in which "a credential scoped to a
    group reaches that group" is a claim about anything.
    """

    DOCUMENT = {
        "hosts": {"w1": {"ansible_port": 22}, "d1": {}},
        "groups": {"web": ["w1"], "db": ["d1"]},
        "children": {"infra": ["web", "db"]},
    }

    def _resolve(self, tmp_path, injections, *extra):
        source = tmp_path / configure_inventory.RENDERED_NAME
        source.write_text(
            configure_inventory.document_to_source(self.DOCUMENT, injections=injections)
        )
        out = subprocess.run(  # noqa: S603
            [ANSIBLE, "-i", str(source), "--list", *extra],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "ANSIBLE_INVENTORY_ENABLED": "yaml,ini",
                "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
            },
            check=False,
        )
        assert out.returncode == 0, f"ansible refused the document:\n{out.stderr}"
        return json.loads(out.stdout)["_meta"]["hostvars"]

    def test_a_group_scoped_credential_reaches_that_group_and_no_other(self, tmp_path):
        hostvars = self._resolve(
            tmp_path,
            {
                "web": {
                    "ansible_user": "deploy",
                    "ansible_ssh_private_key_file": "/tp/credentials/k",
                }
            },
        )
        assert hostvars["w1"]["ansible_user"] == "deploy"
        assert "ansible_user" not in hostvars.get("d1", {}), (
            "the credential leaked to another group"
        )

    def test_an_all_scoped_credential_reaches_every_host(self, tmp_path):
        hostvars = self._resolve(tmp_path, {"all": {"ansible_password": "global-password"}})
        assert hostvars["w1"]["ansible_password"] == "global-password"
        assert hostvars["d1"]["ansible_password"] == "global-password"

    def test_become_survives_as_a_boolean_through_ansible(self, tmp_path):
        hostvars = self._resolve(tmp_path, {"web": {"ansible_become": True}})
        assert hostvars["w1"]["ansible_become"] is True

    def test_a_host_variable_outranks_an_injected_group_variable(self, tmp_path):
        """The documented order, and it has to be ansible's answer rather than
        ours: an operator who declared `ansible_user` in their own inventory
        keeps it, because host beats group."""
        document = {
            "hosts": {"w1": {"ansible_user": "theirs"}},
            "groups": {"web": ["w1"]},
            "children": {},
        }
        source = tmp_path / configure_inventory.RENDERED_NAME
        source.write_text(
            configure_inventory.document_to_source(
                document, injections={"web": {"ansible_user": "ours"}}
            )
        )
        out = subprocess.run(  # noqa: S603
            [ANSIBLE, "-i", str(source), "--list"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
            check=False,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["_meta"]["hostvars"]["w1"]["ansible_user"] == "theirs"

    def test_limit_still_expands_through_the_nesting(self, tmp_path):
        """Injecting group variables must not disturb what `--limit` selects --
        the nesting is what a limit expands through."""
        injections = {"web": {"ansible_user": "deploy"}, "all": {"ansible_password": "p"}}
        assert sorted(self._resolve(tmp_path, injections, "--limit", "web")) == ["w1"]
        assert sorted(self._resolve(tmp_path, injections, "--limit", "infra")) == ["d1", "w1"]
