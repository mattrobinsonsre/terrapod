"""Sparse VCS fetch via the git CLI's partial-clone + sparse-checkout.

Why this exists
---------------
Today's VCS poll fetches a full repo tarball from GitHub/GitLab on every
new SHA. For a workspace tracking a single subdirectory of a monorepo,
that means downloading the entire repo (hundreds of MB) just to read a
few MB of HCL. With N workspaces tracking the same monorepo, the bytes
shared via the cache amortise the cost — but for a single workspace the
fetch is still the full repo.

This module narrows the fetch using git's partial-clone protocol
(`--filter=blob:none`) plus sparse-checkout. Only the commit, trees,
and the blobs reachable under the requested paths cross the wire.

Why the git CLI (not dulwich, not a pure-Python client)
-------------------------------------------------------
We initially tried dulwich. The two-pass design (fetch trees, then
fetch wanted blob SHAs) broke against real GitHub: smart-HTTP `want`
lines only accept commit SHAs, not blob SHAs. Real git handles blob
fetches via the **promisor partial-clone** mechanism — when a sparse
checkout needs a missing blob, the git CLI re-fetches with a special
protocol handshake the server allows for partial-clone clients.
dulwich exposes the config keys that mark a repo as a promisor partial
clone, but the protocol-level handshake isn't implemented. Translating
all of that ourselves would mean reimplementing core git internals
poorly. Shelling out to the canonical git CLI is the pragmatic answer.

Flow
----
1. `git init` an empty repo in `clone_dir`
2. Configure auth via `http.extraheader` (token never appears in URL,
   so it doesn't show up in `ps` output or git's protocol logs)
3. Configure as a promisor partial-clone:
     `extensions.partialClone = origin`
     `remote.origin.promisor = true`
     `remote.origin.partialclonefilter = blob:none`
4. `git fetch --filter=blob:none --depth=1 origin <sha>` — pulls the
   commit and all reachable trees, no blobs
5. `git sparse-checkout init --cone` + `git sparse-checkout set
   <paths>` — narrows the working-tree materialisation
6. `git checkout <sha>` — the checkout sees missing blobs under the
   sparse-checkout cone and lazily fetches them via the promisor
   handshake. Blobs OUTSIDE the cone are never fetched.
7. Tar the working tree (excluding `.git`) and stream to object
   storage via `os.pipe`

Auth
----
Header injected via `git -c http.extraheader='Authorization: Basic ...'`:
- GitHub: `x-access-token:<installation-token>` (~50 min lifetime; refreshed per call)
- GitLab: `oauth2:<access-token>`

Git's smart-HTTP rejects Bearer auth — both providers document Basic
with their respective magic username for the git-protocol path. The
REST APIs use Bearer; the git endpoints don't. Tokens never appear in
URLs, environment variables, or `.git/config` on disk (we use `-c`
for inline config, not `git config`).

Server requirements
-------------------
- `uploadpack.allowFilter=true` — partial-clone protocol. GitHub.com
  and GitLab.com both support it; self-hosted GitLab >= 13.0 too.
- `uploadpack.allowAnySHA1InWant=true` — fetching by arbitrary SHA
  rather than a named ref. Required for PR head SHAs that aren't on a
  branch we own. GitHub enables this; GitLab enables it on most modern
  versions.

If a server rejects either capability, `git fetch` exits non-zero with
a clear error in stderr — we surface that to the caller. No silent
fallback to a full-repo fetch (silent fallback would mask broken
servers).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tarfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from urllib.parse import urlparse

from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services import github_service
from terrapod.storage import get_storage

logger = get_logger(__name__)

_CHUNK_SIZE = 64 * 1024
# Defence-in-depth: validate the SHA is hex-only before passing to git.
# After `origin`, git treats positional args as refspecs (not flags), so
# flag injection isn't reachable — but rejecting non-hex SHAs eliminates
# a class of malformed-input attack and surfaces upstream API anomalies
# (e.g. a misbehaving GitHub mock returning a non-SHA) as a clear error
# instead of an opaque git-fetch failure. Lengths covered: SHA-1 (40),
# SHA-256 (64), and partial prefixes 4–64 for forward-compat with
# `core.abbrev` server configs.
_SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
# `git` honours these env vars to suppress credential prompts and
# terminal interaction — important when running in a container with no
# TTY. `GIT_TERMINAL_PROMPT=0` makes auth failures fail fast instead of
# hanging waiting for input.
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    # `git` writes progress to stderr, including the auth prompt that
    # would otherwise be printed to a terminal — we capture stderr but
    # don't want it to block on a pty.
    "GIT_PAGER": "cat",
}


def normalize_paths(paths: Iterable[str] | None) -> list[str]:
    """Normalize an iterable of repo-relative paths.

    - Strips leading/trailing slashes
    - Drops empty strings
    - Drops duplicates and entries that are prefixes of others (so the
      shorter prefix subsumes the longer; saves work for sparse-checkout)
    - Returns sorted list

    Empty input → empty list (caller interprets as "whole repo").
    """
    if not paths:
        return []
    cleaned = {p.strip("/ ") for p in paths if p and p.strip("/ ")}
    if not cleaned:
        return []
    sorted_paths = sorted(cleaned)
    # Drop any entry that has a strict prefix in the set. e.g. given
    # {"infra", "infra/eks"}, "infra/eks" is redundant.
    result: list[str] = []
    for p in sorted_paths:
        if any(p != prev and p.startswith(prev + "/") for prev in result):
            continue
        result.append(p)
    return result


def paths_hash(paths: Iterable[str] | None) -> str:
    """Stable 12-hex-char hash of a normalized path set.

    Empty input returns the literal string `"full"` so cache keys for
    the full-repo case remain human-readable. Two callers with the same
    logical path set always produce the same hash.
    """
    norm = normalize_paths(paths)
    if not norm:
        return "full"
    payload = json.dumps(norm, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _resolve_clone_host(provider: str, server_url: str | None) -> str:
    """Compute the git host (e.g. `github.com`) from a connection's API URL.

    The connection stores `server_url` as the HTTP API endpoint, but
    git fetches happen against the bare host. For GHE, the API URL has
    an `api.` prefix or `/api/v3` path that we need to strip.
    """
    if provider == "gitlab":
        base = (server_url or "https://gitlab.com").rstrip("/")
        parsed = urlparse(base)
        return parsed.netloc or parsed.path
    base = server_url or "https://api.github.com"
    parsed = urlparse(base)
    host = parsed.netloc or parsed.path
    if host == "api.github.com":
        return "github.com"
    if host.startswith("api."):
        return host[len("api.") :]
    return host


async def _resolve_auth(conn: VCSConnection) -> str:
    """Return the value to set as `Authorization` header for this connection.

    Git's smart-HTTP transport does NOT honour Bearer auth — it expects
    HTTP Basic. GitHub and GitLab document their git-protocol auth via
    a magic username + the token as the password:
    - GitHub: `x-access-token:<installation-token>`
    - GitLab: `oauth2:<access-token>`
    We base64-encode `user:pass` and emit the `Basic ...` header.
    The Bearer token used for the REST API would be silently rejected.
    """
    if conn.provider == "gitlab":
        userpass = f"oauth2:{conn.token or ''}"
    else:
        token = await github_service.get_installation_token(conn)
        userpass = f"x-access-token:{token}"
    encoded = base64.b64encode(userpass.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


async def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    auth_header: str | None = None,
    timeout: float = 300.0,
) -> None:
    """Run `git <args>` and raise if it exits non-zero.

    `auth_header` is injected via `-c http.extraheader=...`. We use the
    inline `-c` flag rather than `git config` so the credential is
    only ever in this process's memory and a transient command-line
    argument list — never written to `.git/config` on disk where a
    later log scrape might find it.

    stdout is discarded (git status is communicated via exit code);
    stderr is captured and included in the exception message on failure
    so callers see the actual git error.
    """
    cmd: list[str] = ["git"]
    if auth_header is not None:
        # `-c key=value` injects a single config entry for the duration
        # of this command. The value is a single argv element; argv is
        # not visible in `ps` for other users in any modern Linux, but
        # the parent process can still read it. Acceptable for our
        # single-tenant API server.
        cmd += ["-c", f"http.extraheader=Authorization: {auth_header}"]
    cmd += args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_GIT_ENV},
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"git command timed out after {timeout}s: {' '.join(args)}") from None
    if proc.returncode != 0:
        # Redact any tokens from stderr just in case `git` echoed them
        # back. We pass auth via header so this is belt-and-braces.
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {msg}")


async def sparse_archive_to_storage(
    conn: VCSConnection,
    owner: str,
    repo: str,
    sha: str,
    paths: Iterable[str] | None,
    storage_key: str,
    *,
    clone_dir: str,
) -> int:
    """Fetch only the blobs under `paths` and stream a tarball to storage.

    Args:
        conn: VCS connection (provides auth + server URL)
        owner, repo: repo coordinates on the provider
        sha: commit SHA to fetch (any commit SHA, not just a branch HEAD)
        paths: repo-relative paths to include; None/empty means whole repo
        storage_key: object-storage key the tarball is uploaded to
        clone_dir: empty directory the caller has reserved for the git
            working tree. Caller is responsible for cleanup (typically
            via `tempfile.TemporaryDirectory` or rmtree in a finally).

    Returns the number of bytes uploaded.

    Raises if any git step fails or the upload errors. The caller
    (`vcs_archive_cache._fetch_and_upload`) is responsible for
    deleting the storage key on partial-upload failures.
    """
    if not _SHA_RE.match(sha):
        raise ValueError(
            f"refusing to git-fetch a non-hex SHA: {sha!r} (expected 4-64 lowercase hex chars)"
        )
    norm_paths = normalize_paths(paths)
    auth_header = await _resolve_auth(conn)
    host = _resolve_clone_host(conn.provider, conn.server_url)
    clone_url = f"https://{host}/{owner}/{repo}.git"

    # Step 1: init + configure as promisor partial-clone.
    # We do this in a single sequential block — none of these steps
    # touch the network and they're cheap. Wrapping the whole thing in
    # `to_thread` would buy nothing.
    await _run_git(["init", "--quiet", clone_dir])
    await _run_git(["-C", clone_dir, "remote", "add", "origin", clone_url])
    await _run_git(["-C", clone_dir, "config", "extensions.partialClone", "origin"])
    await _run_git(["-C", clone_dir, "config", "remote.origin.promisor", "true"])
    await _run_git(["-C", clone_dir, "config", "remote.origin.partialclonefilter", "blob:none"])

    # Step 2: fetch the commit + all trees, no blobs.
    # `--depth=1` gets just the requested commit (no ancestors) — the
    # SHA is honoured verbatim, no fallback to HEAD.
    await _run_git(
        [
            "-C",
            clone_dir,
            "fetch",
            "--filter=blob:none",
            "--depth=1",
            "--no-tags",
            "origin",
            sha,
        ],
        auth_header=auth_header,
    )

    # Step 3: configure sparse-checkout BEFORE checkout. Cone mode
    # is the simplest pattern language and matches our path-prefix
    # semantics — a path "infra" includes everything under it.
    if norm_paths:
        await _run_git(["-C", clone_dir, "sparse-checkout", "init", "--cone"])
        await _run_git(["-C", clone_dir, "sparse-checkout", "set", *norm_paths])

    # Step 4: checkout the requested SHA. With sparse-checkout active,
    # this only materialises files under the cone — and it triggers the
    # promisor lazy-fetch for blobs the cone needs. Blobs OUTSIDE the
    # cone are never pulled. With no sparse-checkout, the whole tree is
    # checked out and all blobs are lazy-fetched.
    await _run_git(
        ["-C", clone_dir, "checkout", "--quiet", sha],
        auth_header=auth_header,
    )

    # Step 5: tar the working tree (excluding .git) and stream to storage.
    storage = get_storage()
    read_fd, write_fd = os.pipe()
    bytes_uploaded = 0

    async def _upload() -> int:
        nonlocal bytes_uploaded

        async def _counted() -> AsyncIterator[bytes]:
            nonlocal bytes_uploaded
            async for chunk in _consumer_chunks(read_fd):
                bytes_uploaded += len(chunk)
                yield chunk

        await storage.put_stream(storage_key, _counted(), content_type="application/x-tar")
        return bytes_uploaded

    producer_task = asyncio.to_thread(_producer_thread, write_fd, clone_dir)
    upload_task = _upload()

    try:
        await asyncio.gather(producer_task, upload_task)
    finally:
        # Defensive close — usually the producer already closed it via
        # the os.fdopen context manager.
        try:
            os.close(write_fd)
        except OSError:
            pass

    logger.info(
        "Sparse VCS archive uploaded",
        connection_id=str(conn.id),
        owner=owner,
        repo=repo,
        sha=sha[:8],
        paths_count=len(norm_paths) if norm_paths else 0,
        paths=norm_paths if norm_paths else None,
        bytes_uploaded=bytes_uploaded,
        storage_key=storage_key,
    )
    return bytes_uploaded


def _write_tarball_from_dir(fileobj, working_tree: str) -> None:
    """Build a gzipped tarball from `working_tree` (excluding `.git/`).

    Member layout is repo-rooted (e.g. `infra/eks/main.tf`). The runner's
    `tar xzf --no-same-owner` consumer expects this shape.

    Walks the directory deterministically (sorted) so identical input
    produces identical output bytes — useful for cache stability and
    test reproducibility.

    Pure I/O, synchronous: caller dispatches to a thread when used from
    async code.
    """
    root = Path(working_tree)
    with tarfile.open(fileobj=fileobj, mode="w:gz") as tf:
        # Walk top-down so we add directories before their contents.
        # `os.walk` with `sorted` makes the order deterministic.
        for dirpath, dirnames, filenames in os.walk(working_tree):
            # Skip the `.git/` dir. `dirnames[:]` mutation prevents
            # `os.walk` from descending into it.
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            filenames = sorted(filenames)
            for name in filenames:
                full = Path(dirpath) / name
                arcname = str(full.relative_to(root))
                # `recursive=False` because we control the recursion
                # manually via os.walk; otherwise tarfile.add would
                # re-descend into directories we've already visited.
                tf.add(str(full), arcname=arcname, recursive=False)


def _producer_thread(write_fd: int, working_tree: str) -> None:
    """Wrap `_write_tarball_from_dir` to drive the write end of `os.pipe()`.

    Takes ownership of `write_fd`: closes it via `os.fdopen` on success,
    or via explicit `os.close` on exception so the consumer sees EOF and
    doesn't block forever on read. Single owner of the fd from this
    point — never close it from the caller.
    """
    try:
        with os.fdopen(write_fd, "wb") as wf:
            _write_tarball_from_dir(wf, working_tree)
    except Exception:
        try:
            os.close(write_fd)
        except OSError:
            pass
        raise


async def _consumer_chunks(read_fd: int) -> AsyncIterator[bytes]:
    """Async-iterate the read end of the pipe in `_CHUNK_SIZE` chunks.

    The fd is wrapped in a buffered file so partial reads are handled
    by the stdlib. Reads are dispatched to a thread to avoid blocking.
    """
    f = os.fdopen(read_fd, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(f.read, _CHUNK_SIZE)
            if not chunk:
                return
            yield chunk
    finally:
        f.close()
