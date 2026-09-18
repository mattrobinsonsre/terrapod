"""Install a Pulumi program's dependencies before it runs (#1566).

Only a Pulumi YAML program worked before this. Every other runtime is a real
toolchain that the runner image does not carry and a set of packages nobody
installs:

- `pulumi-language-nodejs`, which ships inside the Pulumi tarball, is a shim
  that shells out to `node`. There is no `node` in the image.
- A program's own dependencies are ordinary packages -- `@pulumi/pulumi` is an
  npm package like any other -- so something has to install them, pointed at
  Terrapod's proxies so it works in a sealed deployment.

Two properties of the environment shape everything here:

**The root filesystem is read-only.** Exactly three paths are writable, and they
are emptyDirs: `/workspace`, `/tmp` and `$HOME`. Every toolchain's cache
defaults to somewhere else, so each one is redirected explicitly rather than
left to find out at the first write.

**Preview and update run in different pods.** `node_modules/` does not survive
between them, so the install runs in both phases and inside each Job's own
deadline. That is not waste to be optimised away later by sharing state between
pods -- the pods are deliberately independent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from terrapod.runner import exec_subprocess
from terrapod.runner.phases import platform_tool

#: Where npm is told to keep its cache. `$HOME/.npm` would also work -- $HOME is
#: an emptyDir -- but /tmp is the larger of the two by convention here and npm's
#: cache is the biggest thing it writes.
_NPM_CACHE = "/tmp/npm-cache"

#: The API prefix the runner addresses. The legacy alias deliberately, as
#: `pulumi_exec` does: a runner image lags the API by design (the N-2 skew
#: guarantee) and only the alias is served by both.
_API_PREFIX = "/api/terrapod/v1"

#: Where a virtualenv goes when the program does not say. Under /tmp because the
#: root filesystem is read-only and the ambient site-packages cannot be written
#: to at all -- there is no "just pip install" on this image.
_DEFAULT_VENV = "/tmp/pulumi-venv"


class DependencyError(RuntimeError):
    """The program's dependencies could not be installed.

    Carries the child's exit code so the orchestrator can return it unchanged,
    the way `InitError` does for `terraform init`.
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Runtime:
    """What a Pulumi program is written in, and whether we can run it."""

    name: str
    #: False for a runtime this release cannot yet install for.
    supported: bool


#: Runtimes that need nothing installed. `yaml` is interpreted by the CLI
#: itself, with no toolchain and no dependencies.
_NO_INSTALL = {"yaml", ""}

#: What this release can install. The rest are named in the refusal rather than
#: failing later inside Pulumi with something less legible.
_SUPPORTED = {"nodejs", "python"}


def read_runtime(program_dir: Path) -> str:
    """The `runtime` a `Pulumi.yaml` declares, lowercased, or "" if unreadable.

    **This is not a YAML parser and must not grow into one.** The runner image
    carries no YAML library on purpose -- its phase modules use the standard
    library and httpx, and pulling in a parser to read one scalar would be a
    poor trade. Pulumi writes this field in one of exactly two shapes:

        runtime: nodejs

        runtime:
          name: nodejs
          options:
            typescript: true

    Anything else reads as "" and is treated as "nothing to install", which
    leaves Pulumi itself to say what is wrong with the program -- a better error
    than one invented here from a half-understood file.
    """
    path = program_dir / "Pulumi.yaml"
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    for i, line in enumerate(lines):
        m = re.match(r"^runtime:\s*(.*)$", line)
        if m is None:
            continue
        inline = m.group(1).strip().split("#", 1)[0].strip().strip("\"'")
        if inline:
            return inline.lower()
        # The block form: the first `name:` indented under it.
        for nested in lines[i + 1 :]:
            if nested.strip() and not nested[:1].isspace():
                break  # back to column zero: the block ended
            n = re.match(r"^\s+name:\s*(.*)$", nested)
            if n:
                return n.group(1).strip().split("#", 1)[0].strip().strip("\"'").lower()
        break
    return ""


def read_virtualenv(program_dir: Path) -> str:
    """The `virtualenv` option a python program declares, or "".

    Read for the same reason and with the same restraint as `read_runtime`. When
    a program sets it, Pulumi runs that interpreter and ignores
    `PULUMI_PYTHON_CMD` -- so the venv has to be built exactly where the program
    says, not somewhere convenient.
    """
    path = program_dir / "Pulumi.yaml"
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        m = re.match(r"^\s+virtualenv:\s*(.*)$", line)
        if m:
            return m.group(1).strip().split("#", 1)[0].strip().strip("\"'")
    return ""


def classify(program_dir: Path) -> Runtime:
    """The program's runtime and whether this release can install for it."""
    name = read_runtime(program_dir)
    if name in _NO_INSTALL:
        return Runtime(name=name or "yaml", supported=True)
    return Runtime(name=name, supported=name in _SUPPORTED)


def npm_registry_url(api_url: str) -> str:
    """Terrapod's npm proxy, derived the way the plugin override is."""
    return f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/npm/"


def write_npmrc(program_dir: Path, api_url: str, token: str) -> Path:
    """Point npm at Terrapod's proxy, with the run's own token.

    Written to a file rather than passed as a URL with userinfo: the runner
    streams its logs to the API and the UI, and a token in an argv or an index
    URL ends up in them. npm reads `_authToken` keyed by the registry path, so
    the credential never appears on a command line.
    """
    registry = npm_registry_url(api_url)
    # The key npm matches on is the registry URL without its scheme.
    keyed = registry.split("://", 1)[-1]
    body = f"registry={registry}\n//{keyed}:_authToken={token}\ncache={_NPM_CACHE}\n"
    path = program_dir / ".npmrc"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _node_bin(cfg, client=None) -> Path:  # type: ignore[no-untyped-def]
    """Fetch the Node runtime and return the directory holding node and npm."""
    node = platform_tool.ensure_tool(cfg, "node", client=client)
    return node.parent


def install(cfg, program_dir: Path, *, child_grace: float, log_file: str) -> None:  # type: ignore[no-untyped-def]
    """Install what the program needs, or raise DependencyError.

    A runtime needing nothing is a no-op. A runtime this release cannot install
    for is refused here, by name, rather than left to fail further in with an
    error about a missing language host.
    """
    import structlog

    log = structlog.get_logger("runner.pulumi_deps")
    runtime = classify(program_dir)

    if runtime.name in _NO_INSTALL:
        log.info("no dependencies to install", runtime=runtime.name)
        return

    if not runtime.supported:
        raise DependencyError(
            f"this Terrapod cannot run a Pulumi program with runtime "
            f"{runtime.name!r} yet: only {', '.join(sorted(_SUPPORTED))} and yaml "
            f"are supported. Track #1566."
        )

    if runtime.name == "python":
        _install_python(cfg, program_dir, child_grace=child_grace, log_file=log_file, log=log)
        return

    _install_nodejs(cfg, program_dir, child_grace=child_grace, log_file=log_file, log=log)


def _install_nodejs(cfg, program_dir: Path, *, child_grace: float, log_file: str, log) -> None:  # type: ignore[no-untyped-def]
    """`npm ci` when the program has a lockfile, `npm install` when it does not."""
    bin_dir = _node_bin(cfg)
    npm_cli = _npm_cli(bin_dir)

    write_npmrc(program_dir, cfg.api_url, cfg.auth_token)
    Path(_NPM_CACHE).mkdir(parents=True, exist_ok=True)

    locked = (program_dir / "package-lock.json").is_file()
    # `ci` is exact and refuses a lockfile that disagrees with package.json,
    # which is what a run wants; `install` is the only option without one.
    argv = [str(bin_dir / "node"), str(npm_cli), "ci" if locked else "install"]

    # The process environment, not a child-only one: `exec_subprocess.run`
    # forwards signals and takes no env of its own, and every other phase
    # configures its tool the same way (the mirror config, the Pulumi plugin
    # override). The Job is one process per phase, so there is nothing to leak
    # into.
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ.update(npm_env())

    log.info("installing node dependencies", locked=locked, dir=str(program_dir))
    result = exec_subprocess.run(
        argv,
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if result.exit_code != 0:
        raise DependencyError(
            "installing the program's npm dependencies failed; the log above is npm's own output",
            exit_code=result.exit_code,
        )


def npm_env() -> dict[str, str]:
    """npm settings the read-only root filesystem and the log stream require.

    The cache is redirected because `/usr/local` and everything outside the three
    emptyDirs is read-only. The rest turn off output and network calls a run has
    no use for -- the update notifier in particular writes under $HOME and
    reaches upstream, which a sealed deployment cannot do.
    """
    return {
        "npm_config_cache": _NPM_CACHE,
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
    }


def pip_index_url(api_url: str) -> str:
    """Terrapod's PyPI proxy, without credentials in it.

    pip accepts a token in the index URL's userinfo, and every other document
    shows it that way -- but the runner streams its logs to the API and the UI,
    and pip prints its index URL. The credential goes in a `.netrc` instead.
    """
    return f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/pypi/simple"


def write_netrc(api_url: str, token: str) -> Path:
    """The pip credential, in $HOME rather than in a URL or an argv.

    $HOME is one of the three writable mounts, and pip reads `.netrc` for an
    index it is about to fetch from. The machine is the host alone -- netrc has
    no notion of a path -- which is why this is written for the API's host and
    nothing else.
    """
    host = urllib.parse.urlparse(api_url).hostname or ""
    path = Path(os.environ.get("HOME", "/home/runner")) / ".netrc"
    path.write_text(f"machine {host}\n  login x\n  password {token}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _install_python(cfg, program_dir: Path, *, child_grace: float, log_file: str, log) -> None:  # type: ignore[no-untyped-def]
    """Build the program a virtualenv and install its requirements into it.

    A virtualenv rather than the ambient interpreter, because there is no
    ambient option: the root filesystem is read-only, so `site-packages` cannot
    be written to, and pip was removed from the image deliberately (its vendored
    bundle is what scanners report). `python -m venv` restores a working pip
    from the untouched stdlib `ensurepip`.

    Where the venv goes is the program's choice when it makes one. A program
    declaring `options.virtualenv` gets it exactly there, because Pulumi runs
    that interpreter and ignores `PULUMI_PYTHON_CMD`. A program that declares
    none gets one under /tmp, and Pulumi is pointed at it.
    """
    declared = read_virtualenv(program_dir)
    venv = (program_dir / declared) if declared else Path(_DEFAULT_VENV)

    log.info("creating virtualenv", path=str(venv), declared=bool(declared))
    made = exec_subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if made.exit_code != 0:
        raise DependencyError(
            "could not create a virtualenv for the program", exit_code=made.exit_code
        )

    write_netrc(cfg.api_url, cfg.auth_token)
    os.environ.update(pip_env(cfg.api_url))
    if not declared:
        # Pulumi honours this only when the program declares no virtualenv of
        # its own; when it does, the option wins and this is inert.
        os.environ["PULUMI_PYTHON_CMD"] = str(venv / "bin" / "python")

    requirements = program_dir / "requirements.txt"
    if not requirements.is_file():
        log.info("no requirements.txt; the venv has pulumi's own dependencies only")
        return

    log.info("installing python dependencies", requirements=str(requirements))
    result = exec_subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pip", "install", "-r", str(requirements)],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if result.exit_code != 0:
        raise DependencyError(
            "installing the program's python dependencies failed; the log above is "
            "pip's own output",
            exit_code=result.exit_code,
        )


def pip_env(api_url: str) -> dict[str, str]:
    """pip settings the read-only root filesystem and the log stream require."""
    env = {
        "PIP_INDEX_URL": pip_index_url(api_url),
        # /tmp, because pip's default cache is under $HOME and the wheels are
        # the biggest thing it writes.
        "PIP_CACHE_DIR": "/tmp/pip-cache",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        # The check reaches upstream, which a sealed deployment cannot do.
        "PIP_NO_INPUT": "1",
    }
    # pip refuses a plain-HTTP index unless the host is named as trusted -- it
    # does not fail, it *ignores the index*, and the install then dies with
    # "No matching distribution found" for a package the proxy was serving
    # perfectly well. The runner reaches the API on the in-cluster URL, which is
    # http by default, so without this Python support does not work at all.
    #
    # Only for http, and only for that one host: an https API is left strict.
    # The hop is inside the cluster, to Terrapod's own API, with the run's own
    # token -- the same trade the air-gap gate makes with `--trusted-host`.
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme == "http" and parsed.hostname:
        env["PIP_TRUSTED_HOST"] = parsed.hostname
    return env


def _npm_cli(bin_dir: Path) -> Path:
    """npm's entry script inside the Node tree.

    `bin/npm` is a shell wrapper; invoking the .js directly with the node we
    just fetched avoids depending on a shell, on the wrapper's own PATH lookup
    finding the right node, and on the symlink surviving extraction.
    """
    direct = bin_dir.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if direct.is_file():
        return direct
    found = shutil.which("npm", path=str(bin_dir))
    if found:
        return Path(found)
    raise DependencyError(f"npm was not found in the fetched Node tree at {bin_dir}")


def package_json_engines(program_dir: Path) -> str:
    """The Node range a program asks for in package.json, or "".

    Read only to report it: the version actually fetched is the deployment's,
    and quietly honouring a program's range would make the runtime a property of
    the repository rather than of the platform.
    """
    path = program_dir / "package.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    engines = data.get("engines")
    if isinstance(engines, dict):
        return str(engines.get("node", ""))
    return ""
