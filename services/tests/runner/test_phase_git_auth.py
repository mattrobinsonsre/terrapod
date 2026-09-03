"""Tests for the git-module-auth runner phase (#1028).

The phase materializes ``git_http_auth`` / ``git_ssh_auth`` credential entries
(delivered via the per-run Secret) into git config + credential/key files before
``tofu init``. These tests pin BOTH the functional behaviour (credentials wired,
ssh↔https rewrite applied) AND the hard **log-safety invariants**: tokens live
only in 0600 files, the (non-secret) gitconfig is tokenless, and nothing sensitive
could reach an argv/stdout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from unittest.mock import patch

import pytest

from terrapod.runner.phases import git_auth

# Some assertions below drive git's OWN `insteadOf` engine to prove the rewrite
# semantics (not just that we wrote the right config strings). Skip cleanly where
# the git binary is unavailable (it is present in the runner + test images).
_HAS_GIT = shutil.which("git") is not None


def _http(pattern, token="ghp_SECRETTOKEN", username=None, rewrite="to_https"):
    v = {"token": token, "rewrite": rewrite}
    if username:
        v["username"] = username
    return {"category": "git_http_auth", "key": pattern, "value": json.dumps(v)}


def _ssh(pattern, key="-----BEGIN KEY-----\nSECRETKEYBYTES\n-----END KEY-----", rewrite="to_ssh"):
    v = {
        "private_key": key,
        "known_hosts": "gitlab.example.com ssh-ed25519 AAAA",
        "rewrite": rewrite,
    }
    return {"category": "git_ssh_auth", "key": pattern, "value": json.dumps(v)}


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# --- http --------------------------------------------------------------------


def test_http_writes_credential_store_and_tokenless_rewrite(tmp_path):
    env = git_auth.configure([_http("github.com/myorg")], base_dir=tmp_path)

    # GIT_CONFIG_GLOBAL isolates our config.
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")

    # One store file per entry; the LINE is keyed to the bare host and the
    # org scoping is done by the `[credential "<url>"]` section that selects
    # this store (#1449 — a path-keyed line plus useHttpPath can never match a
    # real repo path).
    store = tmp_path / "git-credentials-0"
    assert store.read_text().splitlines() == ["https://x-access-token:ghp_SECRETTOKEN@github.com"]
    assert _mode(store) == 0o600  # secret file, owner-only

    cfg = (tmp_path / "gitconfig").read_text()
    assert 'credential "https://github.com/myorg"' in cfg
    assert "helper = store --file=" in cfg
    # useHttpPath is the bug, not the mechanism: with it on, credential-store
    # matches only an EXACT path, so an org-keyed credential never serves
    # `org/repo.git`.
    assert "useHttpPath" not in cfg
    # ssh→https rewrite is present, TOKENLESS, and PATH-SCOPED to the org (the
    # pattern names one) so it can't hijack another org's ssh source on the host.
    assert 'url "https://github.com/myorg/"' in cfg
    assert "insteadOf = ssh://git@github.com/myorg/" in cfg
    assert "insteadOf = git@github.com:myorg/" in cfg


def test_http_default_username_is_x_access_token(tmp_path):
    git_auth.configure([_http("github.com", username=None)], base_dir=tmp_path)
    creds = (tmp_path / "git-credentials-0").read_text().splitlines()
    assert creds == ["https://x-access-token:ghp_SECRETTOKEN@github.com"]


def test_http_bare_host_is_not_path_scoped(tmp_path):
    git_auth.configure([_http("github.com", rewrite="none")], base_dir=tmp_path)
    cfg = (tmp_path / "gitconfig").read_text()
    assert "useHttpPath" not in cfg
    assert 'credential "https://github.com"' in cfg  # bare host → host-level section
    assert "insteadOf" not in cfg  # rewrite none


def test_multiple_orgs_same_host_get_distinct_credential_lines(tmp_path):
    git_auth.configure(
        [_http("github.com/orgA", token="TOKEN_A"), _http("github.com/orgB", token="TOKEN_B")],
        base_dir=tmp_path,
    )
    # A store file each, and a section each: the section discriminates by org,
    # the store simply hands over its one host-keyed line.
    stores = sorted(f.read_text().strip() for f in tmp_path.glob("git-credentials-*"))
    assert stores == [
        "https://x-access-token:TOKEN_A@github.com",
        "https://x-access-token:TOKEN_B@github.com",
    ]
    cfg = (tmp_path / "gitconfig").read_text()
    assert 'credential "https://github.com/orgA"' in cfg
    assert 'credential "https://github.com/orgB"' in cfg
    assert "useHttpPath" not in cfg


def test_path_scoped_sections_are_declared_before_a_bare_host_one(tmp_path):
    """git consults matching credential helpers in CONFIG ORDER and takes the
    first that answers — it does NOT prefer the more specific section. A
    bare-host entry declared first would serve every org on that host with the
    wrong token."""
    git_auth.configure(
        [_http("github.com", token="TOKEN_HOST"), _http("github.com/orgA", token="TOKEN_A")],
        base_dir=tmp_path,
    )
    cfg = (tmp_path / "gitconfig").read_text()
    assert cfg.index('credential "https://github.com/orgA"]') < cfg.index(
        'credential "https://github.com"]'
    ), "the org-scoped section must come first or the bare host hijacks it"


# --- ssh ---------------------------------------------------------------------


def test_ssh_writes_key_known_hosts_and_config(tmp_path):
    env = git_auth.configure([_ssh("gitlab.example.com")], base_dir=tmp_path)
    key = tmp_path / "ssh" / "id_gitlab_example_com"
    assert "SECRETKEYBYTES" in key.read_text()
    assert _mode(key) == 0o600  # private key owner-only
    assert (tmp_path / "ssh" / "known_hosts_gitlab_example_com").exists()

    cfg = (tmp_path / "gitconfig").read_text()
    assert "sshCommand = ssh -F" in cfg
    ssh_cfg = (tmp_path / "ssh" / "config").read_text()
    assert "Host gitlab.example.com" in ssh_cfg
    assert "IdentitiesOnly yes" in ssh_cfg
    # https→ssh rewrite.
    assert "insteadOf = https://gitlab.example.com/" in cfg
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")


# --- real-git CREDENTIAL lookup (git's own credential engine) ----------------
# The string tests above prove we emit the right sections; these prove git
# actually RESOLVES a credential with them. This is the class of test whose
# absence let #1449 ship: every existing test asserted on the emitted config,
# and the config looked perfectly reasonable — it was git's matching semantics
# that differed from what the code assumed.


def _credential_for(env, path):
    """Ask git which credential it would use for a URL, with no network I/O.

    GIT_CONFIG_NOSYSTEM stops a developer machine's system gitconfig (macOS
    ships credential.helper=osxkeychain) from answering instead and making the
    test pass for the wrong reason.
    """
    r = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={_H}\npath={path}\n\n",
        env={**os.environ, **env, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line.removeprefix("password=")
    return None


_H = "gitauth-test.invalid"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_an_org_scoped_credential_serves_a_repo_UNDER_that_org(tmp_path):
    """#1449: the credential is keyed to the org, but every real fetch asks for
    a path including the REPO. Under the old `useHttpPath` scheme
    credential-store required an exact path match, so this could never resolve
    and git fell through to prompting for a username."""
    env = git_auth.configure([_http(f"{_H}/org-a", token="TOKEN_A")], base_dir=tmp_path)
    assert _credential_for(env, "org-a/some-repo.git") == "TOKEN_A"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_two_orgs_on_one_host_resolve_to_their_own_token(tmp_path):
    """The multi-org, least-privilege case the framework exists to support."""
    env = git_auth.configure(
        [_http(f"{_H}/org-a", token="TOKEN_A"), _http(f"{_H}/org-b", token="TOKEN_B")],
        base_dir=tmp_path,
    )
    assert _credential_for(env, "org-a/some-repo.git") == "TOKEN_A"
    assert _credential_for(env, "org-b/other-repo.git") == "TOKEN_B"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_a_bare_host_entry_does_not_hijack_an_org_scoped_one(tmp_path):
    """git consults matching helpers in CONFIG ORDER and takes the first that
    answers — it does not prefer the more specific section. Emitted in the
    wrong order, the host-wide token would be handed out for every org."""
    env = git_auth.configure(
        [_http(_H, token="TOKEN_HOST"), _http(f"{_H}/org-a", token="TOKEN_A")],
        base_dir=tmp_path,
    )
    assert _credential_for(env, "org-a/some-repo.git") == "TOKEN_A"
    # ...and the bare-host entry still serves everything else on that host.
    assert _credential_for(env, "unscoped-org/repo.git") == "TOKEN_HOST"


# --- real-git rewrite semantics (git's own insteadOf engine) -----------------
# The string-level tests above prove we EMIT the right `insteadOf` lines; these
# prove git actually REWRITES with them. `git ls-remote --get-url` applies
# insteadOf and prints the resolved URL with no network I/O — deterministic.


def _get_url(env, source):
    r = subprocess.run(
        ["git", "ls-remote", "--get-url", source],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
@pytest.mark.parametrize(
    "source",
    ["ssh://git@github.com/org/repo.git", "git@github.com:org/repo.git"],
)
def test_to_https_rewrite_resolves_ssh_and_scp_sources(tmp_path, source):
    """`rewrite=to_https`: both the ssh:// and scp-style git@host: forms of a
    source resolve to the tokenless https URL (git supplies the token via the
    store helper out-of-band, so the resolved URL itself carries no secret)."""
    env = git_auth.configure([_http("github.com", rewrite="to_https")], base_dir=tmp_path)
    assert _get_url(env, source) == "https://github.com/org/repo.git"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_to_ssh_rewrite_resolves_https_source(tmp_path):
    """`rewrite=to_ssh`: an https:// source resolves to the ssh:// deploy-key URL
    (the direction the live smoke did not exercise)."""
    env = git_auth.configure([_ssh("gitlab.example.com", rewrite="to_ssh")], base_dir=tmp_path)
    assert (
        _get_url(env, "https://gitlab.example.com/grp/proj.git")
        == "ssh://git@gitlab.example.com/grp/proj.git"
    )


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_rewrite_is_scoped_to_its_host(tmp_path):
    """A rewrite for host A must NEVER touch a source on host B — over-broad
    insteadOf would silently reroute unrelated module fetches."""
    env = git_auth.configure([_http("github.com", rewrite="to_https")], base_dir=tmp_path)
    assert _get_url(env, "https://example.com/x.git") == "https://example.com/x.git"
    assert _get_url(env, "ssh://git@example.com/x.git") == "ssh://git@example.com/x.git"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_rewrite_none_does_not_touch_urls(tmp_path):
    """`rewrite=none`: sources pass through unchanged (auth still applies via the
    store helper for matching https URLs, but no protocol rewrite happens)."""
    env = git_auth.configure([_http("github.com", rewrite="none")], base_dir=tmp_path)
    assert _get_url(env, "ssh://git@github.com/org/repo.git") == "ssh://git@github.com/org/repo.git"


@pytest.mark.skipif(not _HAS_GIT, reason="git binary required")
def test_to_https_rewrite_is_path_scoped_and_spares_other_orgs(tmp_path):
    """Regression (#1028 audit finding 7): an http `to_https` rewrite scoped to
    org-a must NOT rewrite a DIFFERENT org's ssh source on the same host — a
    host-wide insteadOf would silently strip org-b's ssh (deploy-key) auth."""
    env = git_auth.configure(
        [
            _http("github.com/org-a", rewrite="to_https"),
            _ssh("github.com/org-b", rewrite="none"),
        ],
        base_dir=tmp_path,
    )
    # org-a's ssh source IS rewritten to https (token path).
    assert (
        _get_url(env, "ssh://git@github.com/org-a/repo.git") == "https://github.com/org-a/repo.git"
    )
    # org-b's ssh source is UNTOUCHED — still ssh, so its deploy key applies.
    assert (
        _get_url(env, "ssh://git@github.com/org-b/repo.git")
        == "ssh://git@github.com/org-b/repo.git"
    )


def test_env_neutralises_git_tracing(tmp_path):
    """Regression (#1028 audit finding 8): the returned env forces git tracing OFF
    so an operator-set GIT_TRACE_CURL/GIT_CURL_VERBOSE (which log the Authorization
    header) can't leak the token into the streamed run log."""
    env = git_auth.configure([_http("github.com")], base_dir=tmp_path)
    assert env["GIT_TRACE_CURL"] == "0"
    assert env["GIT_CURL_VERBOSE"] == "0"
    assert env["GIT_TRACE"] == "0"
    assert env["GIT_TRACE_PACKET"] == "0"


def test_non_dict_entry_is_skipped_not_raised(tmp_path):
    """Regression (#1028 audit finding 9): a non-dict item in the mounted list is
    skipped, honouring the phase's 'never raises' contract (was: AttributeError)."""
    entries = [
        "not-a-dict",
        42,
        None,
        {"category": "git_http_auth", "key": "github.com", "value": '{"token":"ghp_X"}'},
    ]
    git_auth.configure(entries, base_dir=tmp_path)  # must not raise
    stores = sorted(f.read_text().strip() for f in tmp_path.glob("git-credentials-*"))
    assert stores == ["https://x-access-token:ghp_X@github.com"]  # only the good entry


# --- HARD log-safety invariant ----------------------------------------------


def test_gitconfig_never_contains_secret_material(tmp_path):
    """The gitconfig is NOT a 0600 file and its insteadOf/rewrite lines could be
    echoed by ``git config --list`` — so it must be entirely tokenless/keyless.
    All secrets live in the separate 0600 creds/key files only."""
    git_auth.configure(
        [
            _http("github.com/org", token="ghp_LEAKME"),
            _ssh("gitlab.example.com", key="PRIVKEYLEAK"),
        ],
        base_dir=tmp_path,
    )
    cfg = (tmp_path / "gitconfig").read_text()
    assert "ghp_LEAKME" not in cfg
    assert "PRIVKEYLEAK" not in cfg
    # No credential-embedded HTTPS URL anywhere (the classic `https://TOKEN@host`
    # leak). `git@host` in a tokenless ssh insteadOf source is fine — that's the
    # ssh username, not a credential.
    assert re.search(r"https://[^\s/]*@", cfg) is None


# --- robustness --------------------------------------------------------------


def test_no_entries_returns_empty(tmp_path):
    assert git_auth.configure([], base_dir=tmp_path) == {}


def test_malformed_value_is_skipped(tmp_path):
    entries = [
        {"category": "git_http_auth", "key": "github.com", "value": "not-json"},
        _http("gitlab.com"),
    ]
    git_auth.configure(entries, base_dir=tmp_path)
    stores = sorted(f.read_text().strip() for f in tmp_path.glob("git-credentials-*"))
    # exactly one store — the malformed github entry produced none, the good one did.
    assert stores == ["https://x-access-token:ghp_SECRETTOKEN@gitlab.com"]


def test_load_absent_file_returns_empty(tmp_path):
    assert git_auth._load(tmp_path / "nope.json") == []


def test_run_no_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(git_auth, "_GIT_AUTH_FILE", tmp_path / "absent.json")
    assert git_auth.run(base_dir=tmp_path / "cfg") == {}


class TestFailuresAreNotSilent:
    """#1442: materialisation failed on a read-only `$HOME`, was logged at
    warning, and the run continued with no credentials at all.

    `init` then failed minutes later against a private module source with an
    error naming neither the credential nor the cause — and the log line above
    it claimed auth had been configured. Every test here is about that gap
    between "wrote a file" and "the run can authenticate".
    """

    def test_an_unwritable_base_dir_fails_the_run(self, tmp_path, monkeypatch) -> None:
        """The exact reported failure: $HOME read-only under
        `readOnlyRootFilesystem`, so nothing can be written."""
        entries = [
            {
                "category": "git_http_auth",
                "key": "github.com",
                "value": '{"username":"x","token":"t"}',
            }
        ]
        monkeypatch.setattr(git_auth, "_load", lambda: entries)

        unwritable = tmp_path / "ro" / "nested"
        with patch.object(git_auth, "configure", side_effect=OSError("Read-only file system")):
            with pytest.raises(git_auth.GitAuthUnavailable) as e:
                git_auth.run(base_dir=unwritable)

        # The message must name the credential count and the path, or an
        # operator is back to guessing.
        assert "1 git credential" in str(e.value)
        assert str(unwritable) in str(e.value)

    def test_config_git_cannot_read_fails_the_run(self, tmp_path, monkeypatch) -> None:
        """ "Wrote a file" is not "git reads it".

        The original report's second symptom: a materially perfect gitconfig on
        disk at a path git never consults.
        """
        entries = [
            {
                "category": "git_http_auth",
                "key": "github.com",
                "value": '{"username":"x","token":"t"}',
            }
        ]
        monkeypatch.setattr(git_auth, "_load", lambda: entries)
        monkeypatch.setattr(
            git_auth, "configure", lambda e, base_dir: {"GIT_CONFIG_GLOBAL": "/nowhere/gitconfig"}
        )

        with pytest.raises(git_auth.GitAuthUnavailable, match="does not read"):
            git_auth.run(base_dir=tmp_path)

    def test_no_entries_is_still_silent(self, monkeypatch) -> None:
        """A workspace declaring no credentials must be entirely unaffected."""
        monkeypatch.setattr(git_auth, "_load", lambda: [])
        assert git_auth.run() == {}

    def test_a_real_write_verifies_end_to_end(self, tmp_path, monkeypatch) -> None:
        """The happy path, through real git: config written, and git reads it."""
        entries = [
            {
                "category": "git_http_auth",
                "key": "github.com",
                "value": '{"username":"x-access-token","token":"tok"}',
            }
        ]
        monkeypatch.setattr(git_auth, "_load", lambda: entries)

        env = git_auth.run(base_dir=tmp_path / "gitauth")

        assert env["GIT_CONFIG_GLOBAL"]
        listed = subprocess.run(
            ["git", "config", "--global", "--list"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # The helper key is namespaced per credential URL now
        # (`credential.https://host.helper`), not a bare `credential.helper` —
        # that is what makes the org scoping work (#1449).
        assert ".helper=store --file=" in listed
        assert "tok" not in listed, "the token must not live in gitconfig"


class TestSshOnlyCredentialsAreNotBrokenByVerification:
    """A workspace whose credentials are all `git_ssh_auth` writes a
    `core.sshCommand` and no `credential.helper` — that key exists only for HTTP.

    An earlier draft of the verification required it, which would have failed
    every SSH-credential run. Caught before release; this pins it.
    """

    def test_an_ssh_only_workspace_succeeds(self, tmp_path, monkeypatch) -> None:
        key = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        entries = [
            {
                "category": "git_ssh_auth",
                "key": "github.com",
                "value": json.dumps(
                    {"private_key": key, "known_hosts": "github.com ssh-ed25519 AAAA"}
                ),
            }
        ]
        monkeypatch.setattr(git_auth, "_load", lambda: entries)

        env = git_auth.run(base_dir=tmp_path / "gitauth")

        assert env["GIT_CONFIG_GLOBAL"], "ssh-only credentials must still configure git"
        listed = subprocess.run(
            ["git", "config", "--global", "--list"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "core.sshcommand" in listed.lower()
        assert "credential.helper" not in listed, (
            "precondition: ssh-only writes no helper — which is why the check must not require one"
        )
