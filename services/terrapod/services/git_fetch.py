"""Sparse VCS fetch via dulwich's partial-clone protocol.

Why this exists
---------------
Today's VCS poll fetches a full repo tarball from GitHub/GitLab on every
new SHA. For a workspace tracking a single subdirectory of a monorepo,
that means downloading the entire repo (hundreds of MB) just to read a
few MB of HCL. With N workspaces tracking the same monorepo, the bytes
shared via the cache amortise the cost — but for a single workspace the
fetch is still the full repo.

This module replaces the tarball fetch with git's partial-clone protocol
(`--filter=blob:none`) plus sparse selection of blobs under specific
paths. Only the commit, trees, and the blobs reachable under the
requested paths cross the wire.

Two-pass design
---------------
dulwich's promisor/lazy-fetch support is limited. Rather than relying on
on-demand blob fetches during tree walking, we do two explicit fetch
rounds:

1. **Pass 1** — fetch the commit at `sha` plus all trees with
   `filter_spec=b"blob:none"`. This gives us the directory structure
   (which is small — trees only carry filename + mode + child SHA) but
   no file contents.
2. **Walk** — traverse trees rooted at the commit, descending only into
   directories whose path is a prefix of, or prefix-matches, any
   requested `paths` entry. Collect the blob SHAs we need.
3. **Pass 2** — fetch those specific blob SHAs.

After pass 2, we walk the trees again and stream a tarball directly from
the object store to object storage via `os.pipe`. No working tree, no
intermediate file.

Auth
----
Embedded in the clone URL:
- GitHub: `https://x-access-token:{installation_token}@github.com/...`
- GitLab: `https://oauth2:{access_token}@gitlab.com/...`

Both providers accept these URL forms for the smart-HTTP protocol. The
installation token is short-lived (~50 min) so it's fetched per call;
caching happens in `github_service.get_installation_token`.

Server requirements
-------------------
- `uploadpack.allowFilter=true` — partial-clone protocol. GitHub and
  GitLab.com both support it; self-hosted GitLab >= 13.0 too.
- `uploadpack.allowAnySHA1InWant=true` — fetching by arbitrary SHA
  rather than a named ref. Required for PR head SHAs that aren't on a
  branch we own. GitHub enables this; GitLab enables it on most modern
  versions.

If a server rejects either capability, the fetch fails with a clear
error and the poller falls back to retrying next cycle. The caller is
responsible for surfacing the failure; we don't fall back to the legacy
tarball path silently — silent fallback would mask broken servers.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import quote, urlparse

from dulwich.client import HttpGitClient
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services import github_service
from terrapod.storage import get_storage

logger = get_logger(__name__)

_CHUNK_SIZE = 64 * 1024
_FILTER_BLOB_NONE = b"blob:none"


def normalize_paths(paths: Iterable[str] | None) -> list[str]:
    """Normalize an iterable of repo-relative paths.

    - Strips leading/trailing slashes
    - Drops empty strings
    - Drops duplicates and entries that are prefixes of others (so the
      shorter prefix subsumes the longer; saves work in the tree walk)
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


def _build_clone_url_sync(
    provider: str, server_url: str | None, token: str, owner: str, repo: str
) -> str:
    """Build an HTTPS clone URL with embedded auth.

    Token is URL-quoted in case it contains characters that would break
    the userinfo segment. Both GitHub installation tokens and GitLab
    PATs are typically alphanumeric, but quoting is cheap.
    """
    safe_token = quote(token, safe="")
    if provider == "gitlab":
        base = (server_url or "https://gitlab.com").rstrip("/")
        parsed = urlparse(base)
        host = parsed.netloc or parsed.path  # tolerate values without scheme
        return f"https://oauth2:{safe_token}@{host}/{owner}/{repo}.git"
    # GitHub: convert the API URL to the Git host. The connection's
    # server_url is the API endpoint (https://api.github.com or
    # https://ghe.example.com/api/v3); the git host is the bare host.
    base = server_url or "https://api.github.com"
    parsed = urlparse(base)
    host = parsed.netloc or parsed.path
    if host == "api.github.com":
        host = "github.com"
    elif host.startswith("api."):
        host = host[len("api.") :]
    return f"https://x-access-token:{safe_token}@{host}/{owner}/{repo}.git"


async def _build_clone_url(conn: VCSConnection, owner: str, repo: str) -> str:
    """Resolve auth and build the clone URL for a connection."""
    if conn.provider == "gitlab":
        token = conn.token or ""
        return _build_clone_url_sync(conn.provider, conn.server_url, token, owner, repo)
    # GitHub: installation token (refreshed on demand)
    token = await github_service.get_installation_token(conn)
    return _build_clone_url_sync(conn.provider, conn.server_url, token, owner, repo)


def _split_clone_url(clone_url: str) -> tuple[str, str]:
    """Split a clone URL into (HttpGitClient base_url, repo_path).

    HttpGitClient takes the host as base_url and the path-on-server as
    a separate argument when calling fetch_pack. We pass the full URL
    minus the trailing `.git` segment + path to be safe.
    """
    parsed = urlparse(clone_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path  # leading slash, e.g. /owner/repo.git
    return base, path


def _path_matches(blob_path: bytes, paths: list[str]) -> bool:
    """True if `blob_path` (bytes, no leading slash) lives under any of `paths`.

    Empty `paths` means "everything matches" — used for full-repo fetch.
    """
    if not paths:
        return True
    decoded = blob_path.decode("utf-8", errors="replace")
    for p in paths:
        if decoded == p or decoded.startswith(p + "/"):
            return True
    return False


def _dir_intersects_paths(dir_path: bytes, paths: list[str]) -> bool:
    """True if `dir_path` is on, contains, or is contained-by any `paths` entry.

    During tree walk we need to descend if either:
      - the directory matches a path prefix (its contents are wanted), or
      - a wanted path lives inside the directory (we need to descend further to find it)
    """
    if not paths:
        return True
    decoded = dir_path.decode("utf-8", errors="replace")
    for p in paths:
        if decoded == p or decoded.startswith(p + "/") or p.startswith(decoded + "/"):
            return True
    return False


def _walk_tree_for_blobs(
    repo: Repo, tree_id: bytes, paths: list[str], prefix: bytes = b""
) -> list[tuple[bytes, int, bytes]]:
    """Walk a tree and return [(path, mode, blob_sha)] for every blob under `paths`.

    Recurses only into directories that intersect `paths` so the walk
    cost is bounded by the requested subtree, not the whole repo.
    """
    out: list[tuple[bytes, int, bytes]] = []
    tree = repo[tree_id]
    if not isinstance(tree, Tree):
        return out
    for entry in tree.items():
        full = prefix + b"/" + entry.path if prefix else entry.path
        if stat.S_ISDIR(entry.mode):
            if _dir_intersects_paths(full, paths):
                out.extend(_walk_tree_for_blobs(repo, entry.sha, paths, full))
        else:
            if _path_matches(full, paths):
                out.append((full, entry.mode, entry.sha))
    return out


def _fetch_partial(
    clone_url: str,
    target_dir: str,
    sha: str,
    paths: list[str],
) -> tuple[Repo, list[tuple[bytes, int, bytes]]]:
    """Initialize an empty repo, do the two-pass partial-clone fetch.

    Returns (repo, blob_entries). `blob_entries` is the list returned by
    `_walk_tree_for_blobs` after pass 2 — ready for tar streaming.

    Synchronous: dulwich is sync. Caller wraps in asyncio.to_thread.
    """
    base_url, repo_path = _split_clone_url(clone_url)
    repo = Repo.init(target_dir)
    client = HttpGitClient(base_url)
    sha_bytes = sha.encode("ascii")

    # Pass 1: commit + all trees, no blobs.
    def determine_wants_pass1(refs: dict[bytes, bytes], **_: Any) -> list[bytes]:
        return [sha_bytes]

    client.fetch(
        repo_path,
        repo,
        determine_wants=determine_wants_pass1,
        depth=1,
        filter_spec=_FILTER_BLOB_NONE,
    )

    commit = repo[sha_bytes]
    if not isinstance(commit, Commit):
        raise RuntimeError(f"object {sha} is not a commit")

    blob_entries = _walk_tree_for_blobs(repo, commit.tree, paths)
    if not blob_entries:
        # Empty path narrowing → nothing to fetch in pass 2.
        return repo, []

    needed_blob_shas = sorted({sha for _path, _mode, sha in blob_entries})

    # Pass 2: fetch the specific blobs.
    def determine_wants_pass2(refs: dict[bytes, bytes], **_: Any) -> list[bytes]:
        return needed_blob_shas

    client.fetch(
        repo_path,
        repo,
        determine_wants=determine_wants_pass2,
        # No filter — we want the blobs themselves.
    )

    return repo, blob_entries


def _producer_thread(
    write_fd: int,
    repo: Repo,
    blob_entries: list[tuple[bytes, int, bytes]],
) -> None:
    """Build a gzipped tarball from `blob_entries` and write to `write_fd`.

    Runs in a thread so the async event loop isn't blocked. The fd is
    owned by this function — closing the file object closes the fd,
    which signals EOF to the consumer.

    Tar member layout: paths are repo-rooted (e.g. `infra/eks/main.tf`),
    matching the format the runner expects from the existing stripped
    tarball — no top-level wrapper directory.
    """
    try:
        with os.fdopen(write_fd, "wb") as wf, tarfile.open(fileobj=wf, mode="w:gz") as tf:
            # Sort for determinism: helps reproducibility of the tarball
            # bytes if upstream content hasn't changed (useful for tests).
            for path, mode, blob_sha in sorted(blob_entries, key=lambda e: e[0]):
                blob = repo[blob_sha]
                if not isinstance(blob, Blob):
                    continue
                data = blob.as_raw_string()
                ti = tarfile.TarInfo(name=path.decode("utf-8", errors="replace"))
                ti.size = len(data)
                ti.mode = mode & 0o7777
                if stat.S_ISLNK(mode):
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = data.decode("utf-8", errors="replace")
                    ti.size = 0
                    tf.addfile(ti)
                else:
                    tf.addfile(ti, io.BytesIO(data))
    except Exception:
        # Closing the fd before re-raise lets the consumer see EOF and
        # error out cleanly rather than blocking forever on read.
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
        sha: commit SHA to fetch
        paths: repo-relative paths to include; None/empty means whole repo
        storage_key: object-storage key the tarball is uploaded to
        clone_dir: empty directory the caller has reserved for the dulwich
            repo. Caller is responsible for cleaning it up (e.g. via
            tempfile.TemporaryDirectory) — we don't manage it here so
            failures don't leave half-deleted state.

    Returns the number of bytes uploaded.

    Raises on any fetch / upload failure. The caller (vcs_archive_cache)
    is responsible for partial-upload cleanup of the storage key.
    """
    norm_paths = normalize_paths(paths)
    clone_url = await _build_clone_url(conn, owner, repo)

    repo_obj, blob_entries = await asyncio.to_thread(
        _fetch_partial, clone_url, clone_dir, sha, norm_paths
    )

    storage = get_storage()
    read_fd, write_fd = os.pipe()

    # The producer owns write_fd; we don't close it from the caller side.
    # The consumer owns read_fd; closed by os.fdopen in _consumer_chunks.
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

    producer_task = asyncio.to_thread(_producer_thread, write_fd, repo_obj, blob_entries)
    upload_task = _upload()

    # Run both concurrently. If the producer dies, its except block
    # closes write_fd → the consumer sees EOF → the upload completes
    # with whatever bytes did make it through. We then re-raise.
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
        blob_count=len(blob_entries),
        bytes_uploaded=bytes_uploaded,
        storage_key=storage_key,
    )
    return bytes_uploaded
