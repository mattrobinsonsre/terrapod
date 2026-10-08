"""Obtain `ansible-core` for the API's inventory resolution (#1967, #2010).

Terrapod does not implement a merge. Ansible performs the merge, the
precedence, the group DAG, the derivation of `all` and `ungrouped`, and the
expansion of `--limit`; this module is how the API gets the `ansible-inventory`
that does it.

## Through Terrapod's own PyPI proxy, with no credential anywhere

`ansible-core` is a PyPI package rather than a release binary, so this is a pip
install -- the same shape `runner/phases/ansible_env.py` uses, and for the same
reason: every fetch has to go through the pull-through cache or an air-gapped
deployment cannot work.

The runner points pip at the API over HTTP and carries the run's token in a
netrc, because the cache is in another process. **Here it is not.**
`package_cache.substrate.get_or_fetch` and the `pypi` helpers are ordinary
async functions, and `routers/package_cache.py` is the HTTP face over them
rather than the only door -- `api_opa` already acquires a platform tool through
exactly that door.

So the shim below serves pip a PEP 503 index **by calling the cache
in-process**. pip gets an index it can resolve against, picks the right wheel
for this interpreter and platform itself, and no credential exists on the path
at all: nothing is minted, nothing is stored, and the API never authenticates to
its own HTTP surface.

That last point is what #2010's body says is impossible -- *"without either
authenticating to its own HTTP surface with a credential it had minted for
itself, or reimplementing pip's resolver"*. Both horns are false. The substrate
is in-process, and pip does its own resolving against an index this serves.

**Letting pip resolve is the point, not a shortcut.** The closure is not
platform-independent: of the nine distributions, `cffi`, `cryptography`,
`markupsafe` and `PyYAML` all ship per-interpreter, per-architecture wheels
(measured). A committed closure would therefore be one file per architecture
per python minor, re-pinned on every interpreter bump, and wrong for local
development the moment that platform differs from production. pip already
solves this correctly against an index.

## `--only-binary=:all:` is a security choice, not a speed one

It refuses to build from source, so pip never executes a package's `setup.py`.
`ansible-core` and all eight of its dependencies publish wheels, so nothing is
given up. It is the same boundary that closed #1970: ansible resolving an
inventory must not become a route to running code we did not choose.

## Fails closed, unlike `api_opa`

`api_opa` degrades because its gate runs again later on the runner, which fails
closed -- so a missing OPA costs a syntax error surfacing at the next run. There
is no later here. A resolution the API cannot perform has no second chance, and
a silently wrong host list is worse than a refusal, so this raises and the read
answers 503.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import structlog

from terrapod.config import settings

logger = structlog.get_logger(__name__)

#: Serialises concurrent first-uses so two inventory reads do not both install.
_lock = asyncio.Lock()
_cached_bin: str | None = None

#: What a project name may be on the wire. PEP 503 normalisation allows letters,
#: digits, `.`, `-` and `_`; anything else is refused rather than normalised,
#: because the value reaches a filesystem key and an upstream URL.
_PROJECT = re.compile(r"^[A-Za-z0-9._-]+$")

#: What a filename may be. No separators and no `..`, so a request cannot climb
#: out of the project it names -- the same class of escape as GHSA-490, which
#: was a path traversal through a loopback shim exactly like this one.
_FILENAME = re.compile(r"^[A-Za-z0-9._+!-]+$")


class AnsibleUnavailable(RuntimeError):
    """`ansible-core` could not be obtained, so an inventory cannot be resolved.

    Raised rather than returning None: there is no weaker answer than not
    resolving, and a caller that treated this as "empty inventory" would hand a
    configure a target set that is silently too small.
    """


def _tool_dir() -> Path:
    """Where the virtualenv lives.

    The attached ephemeral PVC when one is configured -- `/tmp` on the API pod
    is RAM-backed and the install is ~47MB, which is exactly what rule 14 exists
    to keep out of memory. Falls back to the system default for local dev and
    tests, where there is no PVC and nothing at stake.
    """
    configured = settings.vcs.tmpdir
    base = configured if configured and os.path.isdir(configured) else tempfile.gettempdir()
    return Path(base) / "terrapod-tools"


class _PyPIShim(threading.Thread):
    """A loopback PEP 503 index served from the in-process pull-through cache.

    pip needs an *index* to resolve against, and resolving is the whole reason
    to use pip rather than a pinned closure. The cache's HTTP routes require
    authentication, so pointing pip at them would mean the API minting itself a
    credential. This serves the same two responses by calling the cache's own
    functions directly, so there is no credential and no request to ourselves.

    Only two paths exist, and both are validated before anything is resolved:

        GET /simple/<project>/             the project's file index
        GET /simple/<project>/<filename>   one artifact's bytes

    Anything else is a 404 -- not a 403, so a probing client learns nothing
    about what is behind. That validation is the GHSA-490 lesson: a loopback
    shim that forwards a path it has not checked is a traversal waiting to
    happen, and `httpx` normalising `..` is what turned one into a reachable
    escape last time.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(daemon=True)
        self._loop = loop
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]

    @property
    def index_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/simple"

    def run(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _await(self, coro):  # type: ignore[no-untyped-def]
        """Run a coroutine on the API's loop from this handler thread.

        The handler is synchronous (`BaseHTTPRequestHandler`) and everything it
        needs is async, so each request is bridged onto the running loop. The
        work is the cache's own, so it belongs on the loop that owns the
        database and the object store rather than on a second one.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=300)

    def _handler(self):  # type: ignore[no-untyped-def]
        shim = self

        class Handler(BaseHTTPRequestHandler):
            # pip's own output is what an operator reads; the shim's chatter
            # would bury it.
            def log_message(self, *args: object) -> None:  # noqa: A003
                return

            def _not_found(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if len(parts) < 2 or parts[0] != "simple":
                    self._not_found()
                    return

                project = parts[1]
                if not _PROJECT.match(project):
                    self._not_found()
                    return

                if len(parts) == 2:
                    self._serve_index(project)
                elif len(parts) == 3:
                    if not _FILENAME.match(parts[2]):
                        self._not_found()
                        return
                    self._serve_file(project, parts[2])
                else:
                    self._not_found()

            def _serve_index(self, project: str) -> None:
                try:
                    body = shim._await(shim._index_html(project))
                except Exception as exc:  # noqa: BLE001 -- pip reports the status
                    logger.warning("pypi shim index failed", project=project, error=str(exc))
                    self.send_response(502)
                    self.end_headers()
                    return
                encoded = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _serve_file(self, project: str, filename: str) -> None:
                try:
                    blob = shim._await(shim._file_bytes(project, filename))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pypi shim file failed", project=project, file=filename, error=str(exc)
                    )
                    self.send_response(502)
                    self.end_headers()
                    return
                if blob is None:
                    self._not_found()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                # pip checks what it copied against this, so an absent header
                # is the "expected -1 bytes" failure #1906 hit with Pulumi's
                # downloader. The body is not content-encoded here, so the
                # length is exact.
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        return Handler

    async def _index_html(self, project: str) -> str:
        """The project's index, with every file link pointing back at this shim.

        Rewritten rather than passed through: an upstream link would send pip
        straight to PyPI, which is the one thing the cache exists to prevent.
        """
        from terrapod.services.package_cache import pypi

        index = await pypi.fetch_index(project)
        # The base is the index ROOT, not this project's page: `rewrite_json`
        # appends `<normalised-project>/<filename>` itself, so passing the
        # project page doubles the segment and every file 404s. pip gets far
        # enough to resolve the version AND its hash before it tries, which is
        # what made this look like a working index.
        document = pypi.rewrite_json(index, project, self.index_url)
        return pypi.render_html(document)

    async def _file_bytes(self, project: str, filename: str) -> bytes | None:
        """One artifact, from the cache, fetching it upstream on a miss.

        Read into memory rather than streamed because these are wheels -- the
        largest in this closure is a few megabytes -- and pip wants a
        Content-Length it can check. Rule 14 is about the tens-of-megabytes
        case; a wheel is not it, and the install directory is on the PVC anyway.
        """
        from terrapod.db.session import get_db_session
        from terrapod.services.package_cache import pypi
        from terrapod.services.package_cache.substrate import get_or_fetch, lookup_present
        from terrapod.storage import get_storage

        normalised = pypi.normalise(project)
        storage = get_storage()

        async with get_db_session() as db:
            record = await lookup_present(db, storage, pypi.ECOSYSTEM, normalised, filename)
            if record is None:
                index = await pypi.fetch_index(project)
                base_filename, wants_metadata = pypi.is_metadata_request(filename)
                entry = pypi.find_file(index, base_filename)
                if entry is None:
                    return None
                artifact = (
                    pypi.metadata_artifact_for(project, entry)
                    if wants_metadata
                    else pypi.artifact_for(project, entry)
                )
                record = await get_or_fetch(db, storage, artifact)

        chunks: list[bytes] = []
        async for chunk in storage.get_stream(record.storage_key):
            chunks.append(chunk)
        return b"".join(chunks)


def _bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


async def _install(version: str, venv: Path) -> None:
    """Create the virtualenv and install `ansible-core` into it.

    Both subprocesses run off the event loop (rule 13). `asyncio.subprocess` is
    used rather than `to_thread` around `subprocess.run` so the loop is never
    waiting on a thread that is waiting on a process.
    """
    made = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "venv",
        str(venv),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await made.communicate()
    if made.returncode != 0:
        raise AnsibleUnavailable(
            f"could not create a virtualenv for ansible-core (exit {made.returncode}): "
            f"{out.decode(errors='replace')[-800:]}"
        )

    shim = _PyPIShim(asyncio.get_running_loop())
    shim.start()
    try:
        env = {
            **os.environ,
            "PIP_INDEX_URL": shim.index_url,
            # Without this pip SILENTLY IGNORES an http index and the install
            # dies with "No matching distribution found" for a package the
            # proxy was serving perfectly well. The runner's phase records the
            # same trap.
            "PIP_TRUSTED_HOST": "127.0.0.1",
            # Nothing but the shim: a fallback index would defeat the whole
            # point on a sealed deployment.
            "PIP_NO_INDEX": "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            str(_bin_dir(venv) / "python"),
            "-m",
            "pip",
            "install",
            # Never build from source: that would execute a package's setup.py.
            "--only-binary=:all:",
            f"ansible-core=={version}",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise AnsibleUnavailable(
                f"installing ansible-core=={version} failed (exit {proc.returncode}). A "
                f"sealed deployment serves this from its own PyPI pull-through cache, so "
                f"a 'No matching distribution found' here usually means the proxy has "
                f"never seen this version rather than that it does not exist. pip said: "
                f"{out.decode(errors='replace')[-1200:]}"
            )
    finally:
        shim.shutdown()


async def ansible_inventory_binary() -> str:
    """Path to a usable `ansible-inventory`, installing it on first use.

    Raises `AnsibleUnavailable` rather than returning None -- see the module
    docstring for why this fails closed where `api_opa` degrades.

    Lazy on purpose: an install that never reads an inventory never pays for it,
    and a slow or unreachable proxy cannot delay the API becoming ready.
    """
    global _cached_bin
    if _cached_bin:
        return _cached_bin

    async with _lock:
        if _cached_bin:  # another waiter won while we queued
            return _cached_bin

        version = settings.default_ansible_version
        if not version:
            raise AnsibleUnavailable(
                "api.config.default_ansible_version is empty, so there is no version of "
                "ansible-core to install for inventory resolution"
            )

        venv = _tool_dir() / f"ansible-{version}"
        binary = _bin_dir(venv) / "ansible-inventory"
        if binary.exists():
            _cached_bin = str(binary)
            return _cached_bin

        await asyncio.to_thread(venv.parent.mkdir, parents=True, exist_ok=True)
        logger.info("installing ansible-core for inventory resolution", version=version)
        await _install(version, venv)

        if not binary.exists():
            raise AnsibleUnavailable(
                f"ansible-core=={version} installed but produced no ansible-inventory at {binary}"
            )

        logger.info("ansible-core ready", version=version, path=str(binary))
        _cached_bin = str(binary)
        return _cached_bin


def _reset_for_tests() -> None:
    """Clear the memoised path. Tests only."""
    global _cached_bin
    _cached_bin = None
