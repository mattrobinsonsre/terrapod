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

    creds = (tmp_path / "git-credentials").read_text().splitlines()
    assert creds == ["https://x-access-token:ghp_SECRETTOKEN@github.com/myorg"]
    assert _mode(tmp_path / "git-credentials") == 0o600  # secret file, owner-only

    cfg = (tmp_path / "gitconfig").read_text()
    # store helper wired to the creds file; path-scoped (host/org).
    assert "helper = store --file=" in cfg
    assert "useHttpPath = true" in cfg
    # ssh→https rewrite is present and TOKENLESS.
    assert "insteadOf = ssh://git@github.com/" in cfg
    assert "insteadOf = git@github.com:" in cfg
    assert 'url "https://github.com/"' in cfg


def test_http_default_username_is_x_access_token(tmp_path):
    git_auth.configure([_http("github.com", username=None)], base_dir=tmp_path)
    creds = (tmp_path / "git-credentials").read_text().splitlines()
    assert creds == ["https://x-access-token:ghp_SECRETTOKEN@github.com"]


def test_http_bare_host_is_not_path_scoped(tmp_path):
    git_auth.configure([_http("github.com", rewrite="none")], base_dir=tmp_path)
    cfg = (tmp_path / "gitconfig").read_text()
    assert "useHttpPath = true" not in cfg  # bare host → host-level match
    assert "insteadOf" not in cfg  # rewrite none


def test_multiple_orgs_same_host_get_distinct_credential_lines(tmp_path):
    git_auth.configure(
        [_http("github.com/orgA", token="TOKEN_A"), _http("github.com/orgB", token="TOKEN_B")],
        base_dir=tmp_path,
    )
    lines = (tmp_path / "git-credentials").read_text().splitlines()
    # exact line membership (not substring-in-blob) — one distinct token per org.
    assert "https://x-access-token:TOKEN_A@github.com/orgA" in lines
    assert "https://x-access-token:TOKEN_B@github.com/orgB" in lines
    assert "useHttpPath = true" in (tmp_path / "gitconfig").read_text()


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
    creds = (tmp_path / "git-credentials").read_text().splitlines()
    # exactly one line — the malformed github entry produced none, the good one did.
    assert creds == ["https://x-access-token:ghp_SECRETTOKEN@gitlab.com"]


def test_load_absent_file_returns_empty(tmp_path):
    assert git_auth._load(tmp_path / "nope.json") == []


def test_run_no_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(git_auth, "_GIT_AUTH_FILE", tmp_path / "absent.json")
    assert git_auth.run(base_dir=tmp_path / "cfg") == {}
