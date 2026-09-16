"""Registry submodules (#1583): a module published from a subdirectory of its repository.

A submodule is an ordinary registry module whose ``subdirectory`` is set. It
shares its repository's ``vcs_repo_url`` with the root module and any other
submodules — the UI groups modules by repository — and a partial unique index
allows one registration per (repository, subdirectory).

At publish, the repository archive is scoped to the subdirectory and re-rooted,
so the stored tarball has the submodule's files at its root, exactly like any
other module's. The download protocol, the interface parser and catalog items
therefore work unchanged, and consumers never need a ``//subdir`` suffix.
"""

import io
import posixpath
import tarfile

# The column is String(500).
MAX_SUBDIRECTORY_LENGTH = 500


class SubdirectoryError(ValueError):
    """A value that cannot name a directory inside a repository."""


def normalize_subdirectory(value: str | None) -> str:
    """The canonical form of a module's subdirectory.

    Repository-relative and ``/``-separated, with no leading or trailing slash;
    ``""`` means the repository root. Refuses anything that could escape the
    repository or name a directory ambiguously: ``..`` and ``.`` segments,
    empty segments (``a//b``), backslashes and control characters.
    """
    if value is None:
        return ""
    s = value.strip().strip("/")
    if not s:
        return ""
    if "\\" in s:
        raise SubdirectoryError("a subdirectory is written with forward slashes")
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise SubdirectoryError("a subdirectory cannot contain control characters")
    if any(part in ("", ".", "..") for part in s.split("/")):
        raise SubdirectoryError(f"{value!r} is not a directory inside the repository")
    if len(s) > MAX_SUBDIRECTORY_LENGTH:
        raise SubdirectoryError(f"a subdirectory is at most {MAX_SUBDIRECTORY_LENGTH} characters")
    return s


def scope_archive_to_subdirectory(archive: bytes, subdirectory: str) -> bytes | None:
    """Re-root a wrapper-stripped gzipped tarball at ``subdirectory``.

    Keeps only the members under ``subdirectory/``, with that prefix removed,
    so the submodule's files sit at the root of the result. Returns None when
    nothing is under it — the tag predates the submodule, or the path is wrong —
    so the caller can skip the tag rather than publish an empty module.

    Hard links are kept only when their target is inside the subdirectory
    (rewritten to match); a link to a file outside it would dangle. Symbolic
    links are kept as they are, as the unscoped archive had them.

    Synchronous and CPU-bound: call it via ``asyncio.to_thread``.
    """
    prefix = subdirectory.rstrip("/") + "/"
    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()
    kept = 0

    with (
        tarfile.open(fileobj=in_buf, mode="r:gz") as src,
        tarfile.open(fileobj=out_buf, mode="w:gz") as dst,
    ):
        for member in src.getmembers():
            name = posixpath.normpath(member.name)
            # `modules/create-extra/...` shares a string prefix with
            # `modules/create`; matching on `prefix` (with its trailing slash)
            # keeps it out.
            if not name.startswith(prefix):
                continue
            if member.islnk():
                target = posixpath.normpath(member.linkname)
                if not target.startswith(prefix):
                    continue
                member.linkname = target[len(prefix) :]
            member.name = name[len(prefix) :]
            if member.isfile():
                f = src.extractfile(member)
                if f is None:
                    continue
                dst.addfile(member, f)
            else:
                dst.addfile(member)
            if not member.isdir():
                kept += 1

    return out_buf.getvalue() if kept else None
