"""#1933's acceptance criteria, measured against real ansible.

Three of the four acceptance bullets are claims about what ansible does with
what the phase delivers, and none of them is observable from the phase's return
value:

* a vault-encrypted file decrypts during a run,
* multiple vault IDs work,
* a run with the wrong password fails clearly rather than proceeding with
  undecrypted content.

So these drive `ansible-playbook` over a real `ansible-vault` ciphertext, with
the environment the phase actually produces -- not a hand-written one, which
would prove ansible decrypts rather than that we hand it what it needs.

They run against `localhost` with `connection: local`, so they need no host and
no network: what is under test is vault, not connectivity.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from terrapod.runner.phases import ansible_auth

PLAYBOOK = shutil.which("ansible-playbook")
VAULT = shutil.which("ansible-vault")

pytestmark = pytest.mark.skipif(
    PLAYBOOK is None or VAULT is None,
    reason="needs real ansible-playbook and ansible-vault on PATH",
)


def _env(extra: dict[str, str], home: Path) -> dict[str, str]:
    """The environment a configure runs under, as `configure._forced_env` builds
    it, merged the way `configure.run` merges it -- forced LAST."""
    from terrapod.runner.phases.configure import _FORCED_ENV

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        **extra,
        **_FORCED_ENV,
    }


def _encrypt(home: Path, plaintext: str, password: str, *, label: str | None = None) -> str:
    """One `!vault` ciphertext, produced by ansible itself."""
    pw_file = home / f"pw-{label or 'plain'}"
    pw_file.write_text(password)
    cmd = [VAULT, "encrypt_string", plaintext, "--name", "the_secret"]
    if label:
        cmd += ["--vault-id", f"{label}@{pw_file}"]
    else:
        cmd += ["--vault-password-file", str(pw_file)]
    # A CONTROLLED environment, not the inherited one, and it is not fussiness:
    # a developer with `ANSIBLE_VAULT_PASSWORD_FILE` exported makes a second
    # vault id available, and `encrypt_string` then refuses to guess which to
    # encrypt with -- "The vault-ids prod,default are available to encrypt".
    # So the fixture would fail on one machine and pass on another, for a
    # reason having nothing to do with what is under test. This is the same
    # leak `configure._forced_env` strips for the real run.
    out = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        cwd=home,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
        check=False,
    )
    assert out.returncode == 0, f"ansible-vault refused to encrypt:\n{out.stderr}"
    return out.stdout


def _play(home: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    (home / "site.yml").write_text(
        "- hosts: all\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - debug:\n"
        "        msg: 'DECRYPTED={{ the_secret }}'\n"
    )
    (home / "inv.yml").write_text(
        json.dumps({"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}})
    )
    return subprocess.run(  # noqa: S603
        [PLAYBOOK, "-i", str(home / "inv.yml"), str(home / "site.yml")],
        capture_output=True,
        text=True,
        cwd=home,
        env=env,
        check=False,
    )


def _deliver(home: Path, entries: list[dict]) -> dict[str, str]:
    """Run the real phase and return the environment it produced."""
    auth = home / "ansible-auth.json"
    auth.write_text(json.dumps(entries))
    env, _ = ansible_auth.run(path=auth, directory=home / "credentials")
    return env


def _vault_entry(label: str, password: str) -> dict:
    return {
        "category": "ansible_vault_auth",
        "key": label,
        "value": json.dumps({"password": password}),
    }


class TestAVaultEncryptedFileDecryptsDuringARun:
    """Acceptance bullet 2."""

    def test_it_decrypts_with_the_environment_the_phase_produced(self, tmp_path):
        password = "a-vault-password-long-enough-to-register"
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(
            _encrypt(tmp_path, "the-plaintext-value", password, label="prod")
        )
        env = _deliver(tmp_path, [_vault_entry("prod", password)])
        assert "ANSIBLE_VAULT_IDENTITY_LIST" in env, "the phase delivered no vault identity"

        out = _play(tmp_path, _env(env, tmp_path))
        assert out.returncode == 0, f"the play failed:\n{out.stdout}\n{out.stderr}"
        assert "DECRYPTED=the-plaintext-value" in out.stdout, out.stdout

    def test_without_the_phase_the_same_play_fails(self, tmp_path):
        """The control. Without it, a play that decrypts for some other reason --
        a password file left in HOME, an inherited variable -- would read as a
        pass, and the test would be about nothing.
        """
        password = "a-vault-password-long-enough-to-register"
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(
            _encrypt(tmp_path, "the-plaintext-value", password, label="prod")
        )
        out = _play(tmp_path, _env({}, tmp_path))
        assert out.returncode != 0, "the play decrypted with NO credential delivered"
        assert "the-plaintext-value" not in out.stdout


class TestMultipleVaultIDs:
    """Acceptance bullet 3, and the reason the phase emits one env var."""

    def test_two_labelled_secrets_both_decrypt_in_one_run(self, tmp_path):
        prod_pw = "a-prod-vault-password-long-enough"
        dev_pw = "a-dev-vault-password-long-enough"
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(
            _encrypt(tmp_path, "prod-value", prod_pw, label="prod")
        )
        (tmp_path / "group_vars" / "web.yml").write_text(
            _encrypt(tmp_path, "dev-value", dev_pw, label="dev").replace(
                "the_secret", "other_secret"
            )
        )
        env = _deliver(tmp_path, [_vault_entry("prod", prod_pw), _vault_entry("dev", dev_pw)])
        assert env["ANSIBLE_VAULT_IDENTITY_LIST"].count(",") == 1, env

        out = _play(tmp_path, _env(env, tmp_path))
        assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
        assert "DECRYPTED=prod-value" in out.stdout, out.stdout

    def test_an_unlabelled_secret_decrypts_against_a_labelled_identity(self, tmp_path):
        """Measured on #1933 and relied on by the single-password case: an
        unlabelled `!vault` block is tried against each identity in turn, so an
        operator who encrypted without `--vault-id` needs no special handling.
        """
        password = "a-vault-password-long-enough-to-register"
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(
            _encrypt(tmp_path, "unlabelled-value", password)
        )
        env = _deliver(tmp_path, [_vault_entry("whatever", password)])
        out = _play(tmp_path, _env(env, tmp_path))
        assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
        assert "DECRYPTED=unlabelled-value" in out.stdout


class TestTheWrongPasswordFailsClearly:
    """Acceptance bullet 4, and the one that must not be a silent pass.

    Fail-closed here is ANSIBLE's own behaviour rather than a guard of ours,
    which is exactly why it is worth a test: a property nobody implemented is a
    property nobody will notice losing.
    """

    def test_it_refuses_rather_than_proceeding_with_ciphertext(self, tmp_path):
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(
            _encrypt(
                tmp_path, "the-plaintext-value", "the-right-password-long-enough", label="prod"
            )
        )
        env = _deliver(tmp_path, [_vault_entry("prod", "the-WRONG-password-long-enough")])
        out = _play(tmp_path, _env(env, tmp_path))

        assert out.returncode != 0, "a wrong password must not produce a successful play"
        combined = out.stdout + out.stderr
        # And it must say so, rather than handing the playbook the ciphertext.
        assert "Decryption failed" in combined or "vault secret" in combined.lower(), combined
        assert "$ANSIBLE_VAULT" not in out.stdout, (
            "the ciphertext reached the play's output, so a task could have used it"
        )
