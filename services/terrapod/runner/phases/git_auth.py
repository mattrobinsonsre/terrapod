"""Materialize private-git-module credentials before ``tofu init`` (#1028).

Private, non-registry git module sources (``git::https://…``, ``git::ssh://…``,
scp-style ``git@host:…``) need auth that the Terrapod-registry token doesn't
cover. An operator declares the credential once as a **sensitive workspace
variable** in one of two categories — ``git_http_auth`` or ``git_ssh_auth`` —
scoped by a URL pattern; the server resolves it (workspace + variable-set
precedence), delivers it via the per-run Secret, and this phase materializes it
into git's own config **before** ``init`` fetches modules.

Each delivered entry is ``{category, key, value}`` where ``key`` is the URL
pattern/scope (``github.com``, ``github.com/myorg``, ``gitlab.example.com``) and
``value`` is a JSON credential:

* ``git_http_auth`` → ``{"username","token","rewrite":"to_https"|"none"}``
* ``git_ssh_auth``  → ``{"private_key","known_hosts","rewrite":"to_ssh"|"none"}``

``known_hosts`` is optional for github.com/gitlab.com: their host keys are baked
into the runner image's global ``/etc/ssh/ssh_known_hosts`` (see
``runner/saas_known_hosts`` + ``Dockerfile.runner``), which ssh consults in
addition to the per-host ``UserKnownHostsFile`` this phase writes — so an empty
``known_hosts`` for those SaaS hosts still verifies. Supply it only to pin a
self-hosted host.

**Protocol rewriting (ssh↔https).** Each entry's ``rewrite`` makes source URLs
protocol-agnostic via ``insteadOf``: ``to_https`` maps ``ssh://git@host/`` and
scp ``git@host:`` onto ``https://host/`` (so ``ssh://`` sources authenticate over
the token — no keys, no ``getpwuid``); ``to_ssh`` maps ``https://host/`` onto
``ssh://git@host/`` (deploy-key path).

**Log-safety (HARD, #1028).** The runner streams stdout/stderr verbatim, so the
guarantee is mechanism, not redaction: tokens live only in a ``0600``
``git-credentials`` file read out-of-band by git's ``store`` helper; SSH keys are
``0600`` files; the ``insteadOf`` targets are **tokenless** (``https://host/``,
never ``https://TOKEN@host/``); nothing sensitive ever reaches an argv or stdout.
Everything is isolated into runner-owned files pointed at by ``GIT_CONFIG_GLOBAL``
so no user config is clobbered.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger("runner.git_auth")

# Mounted from the per-run vars Secret. Keep in sync with runner/job_template.py
# (_GIT_AUTH_SECRET_KEY / _GIT_AUTH_FILENAME) and the listener's _create_vars_secret.
_GIT_AUTH_FILE = Path("/var/run/terrapod/vars/git-auth.json")

_HTTP = "git_http_auth"
_SSH = "git_ssh_auth"


def _load(path: Path = _GIT_AUTH_FILE) -> list[dict]:
    """Read the mounted git-auth file. Returns ``[]`` when absent/unreadable —
    git auth is optional and its absence must never fail the run."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("failed to read git-auth file", error=str(exc))
        return []
    return data if isinstance(data, list) else []


def _host_of(pattern: str) -> str:
    """The host of a URL pattern: strip any scheme, take the first path segment.

    ``github.com`` → ``github.com``; ``github.com/myorg`` → ``github.com``;
    ``https://gitlab.example.com/grp`` → ``gitlab.example.com``.
    """
    p = pattern.strip()
    for scheme in ("https://", "http://", "ssh://", "git://"):
        if p.startswith(scheme):
            p = p[len(scheme) :]
            break
    p = p.split("@", 1)[-1]  # drop any user@ prefix
    return p.split("/", 1)[0].split(":", 1)[0]


def _write_private(path: Path, content: str) -> None:
    """Write a secret-bearing file at mode 0600 (owner read/write only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive perms from the outset — never a world-readable window.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def configure(entries: list[dict], *, base_dir: Path) -> dict[str, str]:
    """Materialize ``entries`` into git config + credential/key files under
    ``base_dir`` and return the env overrides the caller merges into
    ``os.environ`` (so the ``init`` subprocess inherits them).

    ``base_dir`` is a runner-owned dir for all generated files (config,
    credentials, keys), isolated via ``GIT_CONFIG_GLOBAL``. Returns ``{}`` when
    there are no usable entries.
    """
    creds_lines: list[str] = []  # git-credentials store lines (secret; 0600 file)
    config: list[str] = []  # gitconfig sections (NO secrets — tokenless)
    use_http_path = False
    ssh_hosts: list[tuple[str, Path, Path]] = []  # (host, key_path, known_hosts_path)

    for entry in entries:
        if not isinstance(entry, dict):
            # Honour the "never raises" contract: a non-dict item in the mounted
            # list must be skipped, not `.get()`-ed into an AttributeError.
            continue
        category = entry.get("category")
        pattern = (entry.get("key") or "").strip()
        if not pattern:
            continue
        try:
            cred = json.loads(entry.get("value") or "{}")
        except ValueError:
            logger.warning("git-auth entry has a non-JSON value; skipping")
            continue
        host = _host_of(pattern)
        if not host:
            continue
        rewrite = cred.get("rewrite", "none")

        if category == _HTTP:
            token = cred.get("token")
            if not token:
                continue
            username = cred.get("username") or "x-access-token"
            # Path-scoped when the pattern names an org (host/org) so two orgs on
            # one host can carry distinct tokens (git matches on the leading path).
            scope = pattern.split("://", 1)[-1].split("@", 1)[-1]  # host[/org]
            if "/" in scope:
                use_http_path = True
            creds_lines.append(f"https://{username}:{token}@{scope}")
            if rewrite == "to_https":
                # Tokenless rewrite (the store helper supplies the token per matched
                # URL). SCOPE it to the org path when the pattern names one, so a
                # host-wide insteadOf can't silently hijack a *different* org's ssh
                # source on the same host — e.g. an org-b deploy-key fetch getting
                # rewritten to https and losing its auth. Host-wide only when the
                # pattern is a bare host.
                if "/" in scope:
                    org_path = scope.split("/", 1)[1].rstrip("/")
                    https_target = f"https://{host}/{org_path}/"
                    ssh_src = f"ssh://git@{host}/{org_path}/"
                    scp_src = f"git@{host}:{org_path}/"
                else:
                    https_target = f"https://{host}/"
                    ssh_src = f"ssh://git@{host}/"
                    scp_src = f"git@{host}:"
                config += [
                    f'[url "{https_target}"]\n',
                    f"\tinsteadOf = {ssh_src}\n",
                    f"\tinsteadOf = {scp_src}\n",
                ]
        elif category == _SSH:
            key = cred.get("private_key")
            if not key:
                continue
            key = key if key.endswith("\n") else key + "\n"
            key_path = base_dir / "ssh" / f"id_{host.replace('.', '_')}"
            _write_private(key_path, key)
            kh_path = base_dir / "ssh" / f"known_hosts_{host.replace('.', '_')}"
            _write_private(kh_path, (cred.get("known_hosts") or "").rstrip("\n") + "\n")
            ssh_hosts.append((host, key_path, kh_path))
            if rewrite == "to_ssh":
                config += [
                    f'[url "ssh://git@{host}/"]\n',
                    f"\tinsteadOf = https://{host}/\n",
                ]
        else:
            continue

    if not creds_lines and not config and not ssh_hosts:
        return {}

    # git-credentials store file (secret; 0600) + the store helper wired to it.
    creds_path = base_dir / "git-credentials"
    if creds_lines:
        _write_private(creds_path, "\n".join(creds_lines) + "\n")
        header = ["[credential]\n", f"\thelper = store --file={creds_path}\n"]
        if use_http_path:
            header.append("\tuseHttpPath = true\n")
        config = header + config

    # SSH: an ssh config with per-host IdentityFile/known_hosts, wired via
    # core.sshCommand (env, not argv). Keys/known_hosts are the 0600 files above.
    env: dict[str, str] = {}
    if ssh_hosts:
        ssh_cfg = base_dir / "ssh" / "config"
        blocks = ["# generated by terrapod git_auth phase — do not edit\n"]
        for host, key_path, kh_path in ssh_hosts:
            blocks += [
                f"Host {host}\n",
                "\tUser git\n",
                f"\tIdentityFile {key_path}\n",
                "\tIdentitiesOnly yes\n",
                f"\tUserKnownHostsFile {kh_path}\n",
                "\tStrictHostKeyChecking yes\n",
            ]
        _write_private(ssh_cfg, "".join(blocks))
        config += ["[core]\n", f"\tsshCommand = ssh -F {ssh_cfg}\n"]

    git_config = base_dir / "gitconfig"
    base_dir.mkdir(parents=True, exist_ok=True)
    git_config.write_text("".join(config), encoding="utf-8")
    # GIT_CONFIG_GLOBAL isolates our config so nothing user-owned is clobbered.
    env["GIT_CONFIG_GLOBAL"] = str(git_config)
    # Log-safety backstop: force git tracing OFF so an operator-set GIT_TRACE_CURL /
    # GIT_CURL_VERBOSE (which log the Authorization header — base64 `user:token`) to
    # the stderr the runner streams verbatim can't defeat the tokenless-mechanism
    # guarantee. We only reach here when a credential is being materialised, and
    # these override any inherited env value for the git subprocess.
    env["GIT_TRACE"] = "0"
    env["GIT_TRACE_CURL"] = "0"
    env["GIT_CURL_VERBOSE"] = "0"
    env["GIT_TRACE_PACKET"] = "0"
    return env


class GitAuthUnavailable(RuntimeError):
    """Credentials were configured for this run and could not be applied.

    Raised rather than warned, because the alternative is what #1442 reported:
    materialisation failed on a read-only ``$HOME``, was logged at warning, and
    the run continued with no credentials at all. `init` then failed several
    minutes later against a private module source with an authentication error
    that named neither the credential nor the cause — and the log line above it
    said auth had been configured.

    An operator who declared a credential expects it to be used. Failing here
    attributes the problem to the thing that is actually wrong.
    """


def run(*, base_dir: Path | None = None) -> dict[str, str]:
    """Load the mounted git-auth blob and materialize it.

    Returns env overrides for ``os.environ``, empty when there is nothing to do.
    Raises `GitAuthUnavailable` when entries exist but cannot be applied — a
    malformed *individual* entry is still skipped inside `configure`, so one bad
    credential cannot fail a run that does not depend on it.
    """
    entries = _load()
    if not entries:
        return {}
    base = base_dir or Path(os.environ.get("HOME", "/home/runner")) / ".config" / "terrapod-git"
    try:
        env = configure(entries, base_dir=base)
    except OSError as exc:
        raise GitAuthUnavailable(
            f"{len(entries)} git credential(s) are configured for this workspace but could "
            f"not be written to {base}: {exc}. The run would otherwise continue without "
            f"them and fail later against a private module source."
        ) from exc

    if not env:
        return {}

    # "Wrote a file" is not "git can read it" — the distinction #1442 was
    # reported on. Ask git itself, through the same GIT_CONFIG_GLOBAL the init
    # subprocess will inherit, so a path git does not consult is caught here
    # rather than as an authentication failure minutes later.
    _verify_git_reads_config(env)
    logger.info("git module auth configured", entries=len(entries))
    return env


def _verify_git_reads_config(env: dict[str, str]) -> None:
    """Confirm git actually loads the config we just wrote.

    Reads back a tokenless key. Nothing secret is logged or compared — the
    credentials live in a separate store file, and the check only asks whether
    git sees the configuration at all.
    """
    probe = subprocess.run(  # noqa: S603 — fixed argv
        ["git", "config", "--global", "--list"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0 or "credential.helper" not in probe.stdout:
        raise GitAuthUnavailable(
            f"git does not read the configuration written to "
            f"{env.get('GIT_CONFIG_GLOBAL', '<unset>')}. Credentials would be "
            f"silently ignored and the run would fail later against a private "
            f"module source."
        )
