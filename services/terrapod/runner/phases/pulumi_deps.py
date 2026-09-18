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
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

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
_SUPPORTED = {"nodejs", "python", "go", "dotnet"}


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

    if runtime.name == "dotnet":
        _install_dotnet(cfg, program_dir, child_grace=child_grace, log_file=log_file, log=log)
        return

    if runtime.name == "go":
        _install_go(cfg, program_dir, child_grace=child_grace, log_file=log_file, log=log)
        return

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


class _ModuleProxy(threading.Thread):
    """A loopback shim that holds the run's token so the go command need not.

    The go command will talk plain HTTP to a module proxy quite happily -- but it
    will **never** carry a credential over one. Not in the URL ("refusing to pass
    credentials to insecure URL"), not from a netrc, and not from a `GOAUTH`
    helper either: the header is dropped in silence and the fetch comes back 401.
    All three were tried against a real proxy before this was written.

    The runner reaches the API over an in-cluster HTTP URL in many deployments,
    and every Terrapod package-cache route requires authentication -- so on that
    path Go can reach the proxy and can never use it.

    This closes the gap without weakening anything. It listens on 127.0.0.1,
    forwards to the API with the run's own token, and `GOPROXY` points at it: Go
    carries no credential, so it has nothing to refuse. The token travels exactly
    the hop it already travels for this run's artifacts, its state and its
    binaries -- Go's blanket rule is simply stricter than the one the rest of the
    Job lives by.

    It exists only for the length of the install and serves GET alone.
    """

    def __init__(self, api_url: str, token: str) -> None:
        super().__init__(daemon=True)
        self._upstream = f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/go"
        self._token = token
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]

    def _handler(self):  # type: ignore[no-untyped-def]
        upstream, token = self._upstream, self._token

        class Handler(BaseHTTPRequestHandler):
            # The runner's log is the run's log; the proxy's own chatter would
            # bury the go command's output in it.
            def log_message(self, *args: object) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    # Redirects are followed HERE, not handed to Go. The proxy
                    # answers a module download with a 302 to presigned storage,
                    # and the go command does not follow one -- it reports the
                    # 302 as the error. The target needs no credential, so
                    # following it costs nothing and keeps Go out of it.
                    with httpx.stream(
                        "GET",
                        upstream + self.path,
                        headers=headers,
                        timeout=120.0,
                        follow_redirects=True,
                    ) as r:
                        self.send_response(r.status_code)
                        self.end_headers()
                        # Streamed, not buffered: a module zip runs to tens of
                        # megabytes and this is in the runner's own process.
                        for chunk in r.iter_bytes():
                            self.wfile.write(chunk)
                except Exception as exc:  # noqa: BLE001 - reported to the client
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(str(exc).encode())

        return Handler

    def run(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        """Stop serving. Safe to call whether or not the thread ever started.

        `shutdown()` waits for the serve loop to acknowledge it, so on a server
        that never began serving it blocks for ever -- which, called from the
        `finally` that guarantees cleanup, would hang the run instead of ending
        it. The liveness check is what makes the guarantee safe to make.
        """
        if self.is_alive():
            self._server.shutdown()
        self._server.server_close()


def nuget_source_url(api_url: str) -> str:
    """Terrapod's NuGet service index."""
    return f"{api_url.rstrip('/')}{_API_PREFIX}/package-cache/nuget/index.json"


def write_nuget_config(program_dir: Path, api_url: str, token: str) -> Path:
    """Point NuGet at Terrapod's proxy, with the run's own token.

    A file again, not a URL: `dotnet restore` echoes its sources. `<clear/>`
    matters as much as the source itself -- without it nuget.org stays in the
    list and a sealed deployment hangs on it before ever reaching ours.
    """
    source = nuget_source_url(api_url)
    # NuGet refuses an HTTP source outright -- "NuGet requires HTTPS sources" --
    # unless the config says otherwise, and the runner reaches the API on an
    # in-cluster HTTP URL in many deployments. Named only when the URL actually
    # is http; an https API is left strict. The same trade as pip's trusted-host:
    # the hop is inside the cluster, to Terrapod's own API, with the run's token.
    insecure = ' allowInsecureConnections="true"' if api_url.startswith("http://") else ""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear/>
    <add key="terrapod" value="{source}"{insecure} />
  </packageSources>
  <packageSourceCredentials>
    <terrapod>
      <add key="Username" value="x" />
      <add key="ClearTextPassword" value="{token}" />
      <!-- Without this NuGet negotiates: it sends every request anonymously
           first and only supplies the credential after the 401. That doubles
           the request count and, worse, the anonymous half lands in the API's
           UNAUTHENTICATED rate-limit bucket, which is one bucket per source
           IP sized for public traffic, not for one probe per package. A
           restore exhausts it, the probes start answering 429 instead of 401,
           and the client never gets as far as authenticating: NU1301 on every
           package. Naming the scheme makes NuGet send Basic on the first
           request, so the run's own token is on it from the start. -->
      <add key="ValidAuthenticationTypes" value="basic" />
    </terrapod>
  </packageSourceCredentials>
</configuration>
"""
    path = program_dir / "nuget.config"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def dotnet_env() -> dict[str, str]:
    """.NET settings the read-only root filesystem and a sealed cache require."""
    return {
        # All of these default under $HOME or the install dir; only the three
        # writable mounts exist.
        "NUGET_PACKAGES": "/tmp/nuget/packages",
        "DOTNET_CLI_HOME": "/tmp/dotnet-home",
        # First-run writes an extraction cache and prints a banner; both are
        # noise in a run's log and one of them writes where it may not.
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        # Telemetry reaches upstream, which a sealed deployment cannot do.
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        # .NET refuses to start at all without ICU -- "Couldn't find a valid ICU
        # package installed on the system" -- and the runner image is Debian
        # slim, which carries none. The alternative is installing libicu, which
        # would put roughly 30MB of it in EVERY runner image including the
        # Terraform-only ones, and #1407 §2 is explicit that another engine's
        # ambitions must cost them nothing.
        #
        # What invariant mode gives up is culture-specific collation and
        # formatting. A Pulumi program describes infrastructure; if one ever
        # needs a locale, the answer is a custom runner image with libicu, which
        # `docs/runners.md` already documents as the way to add to the image.
        "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
    }


def _install_dotnet(cfg, program_dir: Path, *, child_grace: float, log_file: str, log) -> None:  # type: ignore[no-untyped-def]
    """`dotnet restore` against Terrapod's NuGet proxy."""
    dotnet = platform_tool.ensure_tool(cfg, "dotnet")
    # On PATH for the same reason Go is: Pulumi's language host runs the program
    # itself, after the restore has long finished.
    os.environ["PATH"] = f"{dotnet.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ.update(dotnet_env())
    for d in ("/tmp/nuget/packages", "/tmp/dotnet-home"):
        Path(d).mkdir(parents=True, exist_ok=True)

    write_nuget_config(program_dir, cfg.api_url, cfg.auth_token)

    log.info("restoring dotnet dependencies", dir=str(program_dir))
    result = exec_subprocess.run(
        [str(dotnet), "restore"],
        log_file=log_file,
        child_grace_seconds=child_grace,
        tee_to_stdout=True,
    )
    if result.exit_code != 0:
        raise DependencyError(
            "restoring the program's NuGet packages failed; the log above is the "
            "dotnet CLI's own output",
            exit_code=result.exit_code,
        )


def go_env(port: int) -> dict[str, str]:
    """Go settings the read-only filesystem, the shim and a sealed cache need."""
    return {
        # No credentials here, and none needed: the shim carries them.
        "GOPROXY": f"http://127.0.0.1:{port}",
        # Everything Go writes has to land on one of the three writable mounts.
        "GOMODCACHE": "/tmp/go/pkg/mod",
        "GOCACHE": "/tmp/go/cache",
        "GOPATH": "/tmp/go",
        # The checksum database is a second upstream, which a sealed deployment
        # cannot reach. The module hashes in the program's own go.sum are still
        # verified -- that check is not what this turns off.
        "GOSUMDB": "off",
        # Otherwise a go.mod naming a newer toolchain makes the go command fetch
        # one, from upstream, outside the proxy.
        "GOTOOLCHAIN": "local",
    }


def _install_go(cfg, program_dir: Path, *, child_grace: float, log_file: str, log) -> None:  # type: ignore[no-untyped-def]
    """Download the program's modules through the shim."""
    go = platform_tool.ensure_tool(cfg, "go")

    # On PATH, and for the whole phase rather than just the download: Pulumi's
    # Go language host looks the toolchain up by name when it runs the program,
    # and without it the preview fails with "couldn't find go binary" long after
    # the modules are safely on disk.
    os.environ["PATH"] = f"{go.parent}{os.pathsep}{os.environ.get('PATH', '')}"

    proxy = _ModuleProxy(cfg.api_url, cfg.auth_token)
    proxy.start()
    log.info("module proxy listening", port=proxy.port)
    try:
        os.environ.update(go_env(proxy.port))
        result = exec_subprocess.run(
            [str(go), "mod", "download"],
            log_file=log_file,
            child_grace_seconds=child_grace,
            tee_to_stdout=True,
        )
    finally:
        proxy.stop()

    if result.exit_code != 0:
        raise DependencyError(
            "downloading the program's go modules failed; the log above is the go "
            "command's own output",
            exit_code=result.exit_code,
        )


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
