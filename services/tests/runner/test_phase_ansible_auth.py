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
import tempfile
from pathlib import Path

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.phases import ansible_auth, configure_inventory
from terrapod.services.ansible_auth import (
    MACHINE_CATEGORY,
    VAULT_CATEGORY,
    placeholder_for,
)


def _working(name: str) -> str | None:
    """A usable `name` on PATH, or None -- **probed, not merely located**.

    `shutil.which` finding a file says nothing about it working. This machine
    carries leftover `~/Library/Python/3.13/bin/ansible-*` shims whose `ansible`
    module is long gone, so the located-only guard did not skip and every test
    below instead failed inside the subprocess with `ModuleNotFoundError: No
    module named 'ansible'` -- a host-environment failure wearing the shape of a
    product defect, which is exactly the noise that makes a known-failing set
    unreadable.

    The pattern is `test_inventory_resolve._working_ansible`, whose docstring
    found this first; these two files simply did not adopt it. Skipping rather
    than failing is right HERE and nowhere else: a stale shim on a developer's
    machine must not fail the suite, while the test image's own install is gated
    at BUILD time by `ansible-inventory --version` in `docker/Dockerfile.test`,
    so a broken install in CI fails the image build where it belongs.
    """
    found = shutil.which(name)
    if found is None:
        return None
    # Probed under the SAME env the tests impose, which is the whole point.
    # These tests override HOME to isolate ansible, and Python derives the user
    # site-packages directory FROM HOME -- so on a machine whose ansible is a
    # user-site install (`~/Library/Python/3.x/...`), relocating HOME hides it
    # and the console script dies with `ModuleNotFoundError: No module named
    # 'ansible'`. Probing with the inherited HOME reports the binary as usable
    # and the tests then fail anyway, which is how the first version of this
    # guard changed nothing.
    with tempfile.TemporaryDirectory() as probe_home:
        try:
            probe = subprocess.run(  # noqa: S603 -- the path came from `which`
                [found, "--version"],
                capture_output=True,
                timeout=30,
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "HOME": probe_home},
            )
        except (OSError, subprocess.SubprocessError):
            return None
    return found if probe.returncode == 0 else None


ANSIBLE = _working("ansible-inventory")


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

    def test_the_password_is_owner_only_and_nothing_wider(self, tmp_path):
        """`0600`, and the group bit is no longer load-bearing.

        It was `0640` and the reasoning was sound at the time:
        `_ee_security_context` set NO `runAsUser` on the sidecar so an
        operator-supplied image could run as its own uid, `fsGroup` granted that
        uid the shared volume, and `fsGroup` works through the GROUP -- which
        `0600` excludes. **#1928 pins `runAsUser` to 1000**, so there is no
        longer a reader that does not share our uid, and these are the files it
        would be worst to leave open: SSH private keys, connection passwords and
        vault passphrases.

        The narrowing is a breaking change for an operator EE that cannot run as
        1000, recorded in `docs/upgrading-to-2.0.md` and refused at the sidecar's
        startup probe rather than met here as an unreadable file mid-playbook.
        """
        env, _ = ansible_auth.run(
            path=_auth_file(tmp_path, [_vault("prod", "pw-long-enough-to-register")])
        )
        path = Path(env["ANSIBLE_VAULT_IDENTITY_LIST"].split("@", 1)[1])
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_the_private_key_has_the_same_mode(self, tmp_path):
        """ssh refuses a key it considers too permissive, so this is the mode
        that has to be right in both directions at once."""
        _, resolved = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("all", private_key="-----BEGIN-----\nk")])
        )
        path = Path(resolved["all.private_key"])
        assert path.stat().st_mode & 0o777 == 0o600

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
    """Every field resolves to a REFERENCE an inventory can substitute (#1933).

    The phase no longer decides where a credential applies -- the inventory does,
    by naming it -- so what these assert is the mapping from
    `"<name>.<field>"` to what a placeholder standing for it becomes: a PATH for
    a field delivered as a file, the value itself for the rest.
    """

    def test_a_file_field_resolves_to_a_PATH_and_the_key_is_not_in_the_mapping(self, tmp_path):
        """The security property: only the path is ever substituted into a
        document, so the key cannot reach the inventory however the operator
        refers to it."""
        _, resolved = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("web", private_key="-----BEGIN-----\nkeymaterial")])
        )
        assert set(resolved) == {"web.private_key"}
        assert "keymaterial" not in json.dumps(resolved)
        assert Path(resolved["web.private_key"]).read_text().startswith("-----BEGIN-----")

    def test_the_key_file_ends_in_a_newline(self, tmp_path):
        """ssh refuses a key without one as "invalid format", which names
        neither the key nor the missing byte."""
        _, resolved = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("web", private_key="-----BEGIN-----\nnotrailing")])
        )
        assert Path(resolved["web.private_key"]).read_text().endswith("\n")

    def test_a_value_field_resolves_to_its_own_value(self, tmp_path):
        _, resolved = ansible_auth.run(
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
        assert resolved == {
            "web.password": "a-long-connection-password",
            "web.user": "deploy",
            "web.become_method": "sudo",
            "web.become_user": "root",
        }

    def test_become_resolves_to_a_string_ansible_reads_as_a_boolean(self, tmp_path):
        """It is substituted INTO a document, so it has to be a string -- and
        one ansible's own templating reads as true, not the operator's spelling
        of it."""
        _, resolved = ansible_auth.run(
            path=_auth_file(
                tmp_path, [_machine("web", password="a-long-connection-password", become="yes")]
            )
        )
        assert resolved["web.become"] == "true"
        _, off = ansible_auth.run(
            path=_auth_file(
                tmp_path, [_machine("web", password="a-long-connection-password", become="no")]
            )
        )
        assert off["web.become"] == "false"

    def test_several_credentials_resolve_side_by_side(self, tmp_path):
        """Which is what makes per-host keys expressible at all: the operator
        declares one credential per key and the inventory says which host gets
        which."""
        _, resolved = ansible_auth.run(
            path=_auth_file(
                tmp_path,
                [
                    _machine("web-key", private_key="-----BEGIN-----\nWEB"),
                    _machine("db-key", private_key="-----BEGIN-----\nDB"),
                ],
            )
        )
        assert set(resolved) == {"web-key.private_key", "db-key.private_key"}
        assert resolved["web-key.private_key"] != resolved["db-key.private_key"]
        assert "WEB" in Path(resolved["web-key.private_key"]).read_text()
        assert "DB" in Path(resolved["db-key.private_key"]).read_text()

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

    def test_a_name_cannot_escape_the_credentials_directory(self, tmp_path):
        """Belt to the write guard's braces: a row written before the guard
        existed must not name `../`."""
        _, resolved = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("../../etc/evil", private_key="k")])
        )
        written = Path(next(iter(resolved.values())))
        assert written.parent == (tmp_path / "credentials")

    def test_a_credential_nothing_refers_to_is_still_resolved(self, tmp_path):
        """Deliberately. Which credentials the inventory uses is its business,
        and a phase that resolved only the referenced ones would have to read
        the inventory to find out -- coupling the two steps for nothing, since
        an unreferenced credential costs one file nobody opens."""
        _, resolved = ansible_auth.run(
            path=_auth_file(tmp_path, [_machine("never-referred-to", private_key="k")])
        )
        assert set(resolved) == {"never-referred-to.private_key"}


@pytest.mark.skipif(ANSIBLE is None, reason="needs a WORKING ansible-inventory on PATH")
class TestRealAnsibleResolvesTheSubstitutedInventory:
    """The behaviour, not the shape.

    Everything above asserts what the phase RETURNS. These assert what ansible
    does with a document the inventory placed the references in -- which is the
    only form in which "the operator decides where each key goes" is a claim
    about anything.
    """

    def _resolve(self, tmp_path, document, resolved, *extra):
        source = tmp_path / configure_inventory.RENDERED_NAME
        source.write_text(configure_inventory.document_to_source(document, resolved=resolved))
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

    def test_a_per_host_reference_reaches_only_that_host(self, tmp_path):
        """The case the scope model could not express: two hosts, two keys,
        chosen in the inventory."""
        document = {
            "hosts": {
                "w1": {"ansible_ssh_private_key_file": placeholder_for("web-key", "private_key")},
                "d1": {"ansible_ssh_private_key_file": placeholder_for("db-key", "private_key")},
            },
            "groups": {"all_hosts": ["w1", "d1"]},
            "children": {},
        }
        hostvars = self._resolve(
            tmp_path,
            document,
            {
                "web-key.private_key": "/tp/credentials/WEB",
                "db-key.private_key": "/tp/credentials/DB",
            },
        )
        assert hostvars["w1"]["ansible_ssh_private_key_file"] == "/tp/credentials/WEB"
        assert hostvars["d1"]["ansible_ssh_private_key_file"] == "/tp/credentials/DB"

    def test_a_reference_inside_ssh_common_args_resolves(self, tmp_path):
        """The mechanism a real estate actually uses for a per-group key: the
        reference is one fragment of an ssh argument list, not the whole value.
        A scope-based injection could never have filled this."""
        document = {
            "hosts": {
                "w1": {
                    "ansible_ssh_common_args": (
                        f"-o IdentityFile={placeholder_for('web-key', 'private_key')} -o Port=2222"
                    )
                }
            },
            "groups": {},
            "children": {},
        }
        hostvars = self._resolve(tmp_path, document, {"web-key.private_key": "/tp/credentials/WEB"})
        assert hostvars["w1"]["ansible_ssh_common_args"] == (
            "-o IdentityFile=/tp/credentials/WEB -o Port=2222"
        )

    def test_become_resolves_to_something_ansible_reads_as_true(self, tmp_path):
        document = {
            "hosts": {"w1": {"ansible_become": placeholder_for("web", "become")}},
            "groups": {},
            "children": {},
        }
        hostvars = self._resolve(tmp_path, document, {"web.become": "true"})
        # Substituted as a string; ansible's own boolean coercion does the rest,
        # which is why "true" and not "yes".
        assert hostvars["w1"]["ansible_become"] in (True, "true")

    def test_limit_still_expands_through_the_nesting(self, tmp_path):
        """Substituting must not disturb what `--limit` selects -- the nesting
        is what a limit expands through."""
        # Every host carries a variable, deliberately: `ansible-inventory --list`
        # OMITS a host from `_meta.hostvars` when it has none, so a var-less `d1`
        # would be absent from the result and read as "the limit did not select
        # it" -- which is the opposite of what this asserts.
        document = {
            "hosts": {
                "w1": {"k": placeholder_for("web", "user")},
                "d1": {"ansible_port": 22},
            },
            "groups": {"web": ["w1"], "db": ["d1"]},
            "children": {"infra": ["web", "db"]},
        }
        resolved = {"web.user": "deploy"}
        assert sorted(self._resolve(tmp_path, document, resolved, "--limit", "web")) == ["w1"]
        assert sorted(self._resolve(tmp_path, document, resolved, "--limit", "infra")) == [
            "d1",
            "w1",
        ]
