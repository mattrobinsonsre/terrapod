"""Tests for `git_fetch` — the git-CLI-backed sparse VCS fetch.

Pure helpers (path normalisation / hashing, host resolution) are
exercised here as plain Python. The end-to-end fetch is exercised
against a real local bare repo using the actual `git` CLI — that
proves the SHA we ask for is the SHA we get (not HEAD), that
sparse-checkout narrows the working tree, and that the producer
streams a tarball whose layout matches the runner's `tar xzf` contract.

The network-facing call against real GitHub/GitLab is validated in
Tilt — out of scope for unit tests.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tarfile
from unittest.mock import MagicMock

import pytest

from terrapod.services import git_fetch

# ── normalize_paths / paths_hash ───────────────────────────────────────


class TestNormalizePaths:
    def test_empty_input(self):
        assert git_fetch.normalize_paths(None) == []
        assert git_fetch.normalize_paths([]) == []
        assert git_fetch.normalize_paths(["", "  ", "/"]) == []

    def test_strips_slashes_and_whitespace(self):
        assert git_fetch.normalize_paths(["/infra/eks/", " modules/vpc "]) == [
            "infra/eks",
            "modules/vpc",
        ]

    def test_dedupes_and_sorts(self):
        assert git_fetch.normalize_paths(["b", "a", "a", "c"]) == ["a", "b", "c"]

    def test_collapses_strict_prefixes(self):
        """If `infra` is in the set, `infra/eks` is redundant — drop it."""
        assert git_fetch.normalize_paths(["infra/eks", "infra"]) == ["infra"]
        assert git_fetch.normalize_paths(["infra/eks", "infra/eks/sub"]) == ["infra/eks"]

    def test_does_not_collapse_partial_segment_matches(self):
        """`infra-prod` doesn't share a path component with `infra`, so both stay."""
        assert sorted(git_fetch.normalize_paths(["infra", "infra-prod"])) == [
            "infra",
            "infra-prod",
        ]


class TestPathsHash:
    def test_empty_returns_full_sentinel(self):
        assert git_fetch.paths_hash(None) == "full"
        assert git_fetch.paths_hash([]) == "full"

    def test_stable_across_call_orders(self):
        assert git_fetch.paths_hash(["b", "a"]) == git_fetch.paths_hash(["a", "b"])

    def test_different_path_sets_collide_only_on_collision(self):
        assert git_fetch.paths_hash(["a"]) != git_fetch.paths_hash(["b"])
        assert git_fetch.paths_hash(["a"]) != git_fetch.paths_hash(["a", "b"])

    def test_hash_length_is_12_hex(self):
        h = git_fetch.paths_hash(["x"])
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)


# ── _resolve_clone_host ────────────────────────────────────────────────


class TestResolveCloneHost:
    def test_github_default(self):
        assert git_fetch._resolve_clone_host("github", "https://api.github.com") == "github.com"

    def test_github_default_when_none(self):
        assert git_fetch._resolve_clone_host("github", None) == "github.com"

    def test_github_enterprise_strips_api_prefix(self):
        assert (
            git_fetch._resolve_clone_host("github", "https://api.ghe.example.com")
            == "ghe.example.com"
        )

    def test_gitlab_default(self):
        assert git_fetch._resolve_clone_host("gitlab", None) == "gitlab.com"

    def test_gitlab_self_hosted(self):
        assert (
            git_fetch._resolve_clone_host("gitlab", "https://gitlab.example.com")
            == "gitlab.example.com"
        )


# ── _resolve_auth ──────────────────────────────────────────────────────


class TestResolveAuth:
    """Git's smart-HTTP transport rejects Bearer auth — must use Basic
    with the provider's documented magic username + token-as-password.
    Verified against real GitHub in Tilt: Bearer fails with 401, Basic
    succeeds. Regressing this would silently break every VCS poll."""

    @pytest.mark.asyncio
    async def test_gitlab_uses_oauth2_basic_auth(self):
        import base64

        conn = MagicMock()
        conn.provider = "gitlab"
        conn.token = "glpat_secret"
        header = await git_fetch._resolve_auth(conn)
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header[len("Basic ") :]).decode("ascii")
        assert decoded == "oauth2:glpat_secret"

    @pytest.mark.asyncio
    async def test_github_uses_x_access_token_basic_auth(self, monkeypatch):
        import base64

        conn = MagicMock()
        conn.provider = "github"

        async def fake_token(_conn):
            return "ghs_install_token"

        monkeypatch.setattr(git_fetch.github_service, "get_installation_token", fake_token)

        header = await git_fetch._resolve_auth(conn)
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header[len("Basic ") :]).decode("ascii")
        assert decoded == "x-access-token:ghs_install_token"


# ── _write_tarball_from_dir ────────────────────────────────────────────


class TestWriteTarballFromDir:
    """`_write_tarball_from_dir` produces a deterministic gzipped tarball
    with repo-rooted entries. The runner's `tar xzf --no-same-owner`
    consumer expects this shape. `.git/` must be excluded — we don't
    ship the git internals to the runner.
    """

    def test_writes_repo_rooted_tarball(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "main.tf").write_text("# top\n")
        (wt / "infra").mkdir()
        (wt / "infra" / "eks.tf").write_text("# eks\n")

        out_path = tmp_path / "out.tar.gz"
        with open(out_path, "wb") as f:
            git_fetch._write_tarball_from_dir(f, str(wt))

        with tarfile.open(out_path, "r:gz") as tf:
            members = {
                m.name: tf.extractfile(m).read() if not m.isdir() else b"" for m in tf.getmembers()
            }
        assert members == {"main.tf": b"# top\n", "infra/eks.tf": b"# eks\n"}

    def test_excludes_git_directory(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "main.tf").write_text("# top\n")
        (wt / ".git").mkdir()
        (wt / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (wt / ".git" / "objects").mkdir()
        (wt / ".git" / "objects" / "pack.idx").write_bytes(b"x" * 1024)

        out_path = tmp_path / "out.tar.gz"
        with open(out_path, "wb") as f:
            git_fetch._write_tarball_from_dir(f, str(wt))

        with tarfile.open(out_path, "r:gz") as tf:
            names = sorted(m.name for m in tf.getmembers())
        # Only the main.tf — nothing under .git/
        assert names == ["main.tf"]


# ── End-to-end against a real local bare repo via the git CLI ──────────


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark_git = pytest.mark.skipif(
    not _git_available(),
    reason="`git` CLI not installed in test environment",
)


@pytest.fixture
def two_commit_bare_repo(tmp_path) -> tuple[str, str, str]:
    """Build a real bare git repo with two commits and return
    (file_url, sha1, sha2).

    Layout at sha1:    only `top.tf`
    Layout at sha2:    `top.tf`, `infra/main.tf`, `modules/vpc.tf` (HEAD)

    The fetch tests use sha1 to prove the SHA we ask for is the SHA we
    get (not HEAD). They also use sha2 with sparse-checkout to prove
    path narrowing works.
    """
    src = tmp_path / "src"
    src.mkdir()
    bare = tmp_path / "bare.git"

    def run(cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    run(src, "init", "--quiet", "-b", "main")
    run(src, "config", "user.email", "t@t")
    run(src, "config", "user.name", "t")
    # Allow the bare repo to accept uploads of arbitrary SHAs
    run(src, "config", "uploadpack.allowFilter", "true")
    run(src, "config", "uploadpack.allowAnySHA1InWant", "true")

    (src / "top.tf").write_text("# top\n")
    run(src, "add", "top.tf")
    run(src, "commit", "--quiet", "-m", "first")
    sha1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(src), text=True).strip()

    (src / "infra").mkdir()
    (src / "infra" / "main.tf").write_text("# infra\n")
    (src / "modules").mkdir()
    (src / "modules" / "vpc.tf").write_text("# vpc\n")
    run(src, "add", "infra", "modules")
    run(src, "commit", "--quiet", "-m", "second")
    sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(src), text=True).strip()

    # Clone bare. The bare repo inherits `uploadpack.*` from the
    # working repo? No — bare init doesn't copy config. Re-set on the
    # bare so partial-clone fetches work.
    subprocess.run(
        ["git", "clone", "--bare", "--quiet", str(src), str(bare)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(bare), "config", "uploadpack.allowFilter", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(bare), "config", "uploadpack.allowAnySHA1InWant", "true"],
        check=True,
    )

    file_url = f"file://{bare}"
    return file_url, sha1, sha2


def _fake_storage(captured_chunks: list[bytes]) -> MagicMock:
    """Mock the storage layer so we can capture what would be uploaded."""
    storage = MagicMock()

    async def put_stream(_key, chunks, content_type=None):  # noqa: ARG001
        async for c in chunks:
            captured_chunks.append(c)

    storage.put_stream = put_stream
    return storage


@pytestmark_git
class TestSparseArchiveAgainstBareRepo:
    """End-to-end against the real `git` CLI and a local bare repo
    served via `file://`. No network involved."""

    @pytest.mark.asyncio
    async def test_fetches_requested_sha_not_head(self, two_commit_bare_repo, tmp_path):
        """Drive the production helper `_run_git` directly against a bare
        repo served via `file://`. The full `sparse_archive_to_storage`
        path composes an `https://...` URL from the connection's host
        configuration; redirecting that to a `file://` URL would mean
        monkeypatching the URL builder, which doesn't add coverage over
        running the same git steps directly. Auth resolution is covered
        in `TestResolveAuth` separately.
        """
        file_url, sha1, _sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone1"
        clone_dir.mkdir()

        await git_fetch._run_git(["init", "--quiet", str(clone_dir)])
        await git_fetch._run_git(["-C", str(clone_dir), "remote", "add", "origin", file_url])
        await git_fetch._run_git(
            ["-C", str(clone_dir), "config", "extensions.partialClone", "origin"]
        )
        await git_fetch._run_git(["-C", str(clone_dir), "config", "remote.origin.promisor", "true"])
        await git_fetch._run_git(
            ["-C", str(clone_dir), "config", "remote.origin.partialclonefilter", "blob:none"]
        )
        # Fetch sha1 (NOT HEAD = _sha2). file:// transport doesn't
        # always honour --depth on local clones, so we omit it here —
        # the assertion is on which SHA's tree we get, not on shallow
        # clone correctness (which is a git CLI concern).
        await git_fetch._run_git(
            ["-C", str(clone_dir), "fetch", "--filter=blob:none", "--no-tags", "origin", sha1]
        )
        await git_fetch._run_git(["-C", str(clone_dir), "checkout", "--quiet", sha1])

        # At sha1, only top.tf exists. If we'd been silently fetching
        # HEAD (_sha2), `infra/` and `modules/` would be present.
        files = sorted(
            str(p.relative_to(clone_dir))
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        assert files == ["top.tf"]

    @pytest.mark.asyncio
    async def test_sparse_checkout_narrows_working_tree(self, two_commit_bare_repo, tmp_path):
        file_url, _sha1, sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone2"
        clone_dir.mkdir()

        await git_fetch._run_git(["init", "--quiet", str(clone_dir)])
        await git_fetch._run_git(["-C", str(clone_dir), "remote", "add", "origin", file_url])
        await git_fetch._run_git(
            ["-C", str(clone_dir), "config", "extensions.partialClone", "origin"]
        )
        await git_fetch._run_git(["-C", str(clone_dir), "config", "remote.origin.promisor", "true"])
        await git_fetch._run_git(
            ["-C", str(clone_dir), "config", "remote.origin.partialclonefilter", "blob:none"]
        )
        await git_fetch._run_git(
            ["-C", str(clone_dir), "fetch", "--filter=blob:none", "--no-tags", "origin", sha2]
        )
        await git_fetch._run_git(["-C", str(clone_dir), "sparse-checkout", "init", "--cone"])
        await git_fetch._run_git(["-C", str(clone_dir), "sparse-checkout", "set", "modules"])
        await git_fetch._run_git(["-C", str(clone_dir), "checkout", "--quiet", sha2])

        # With `sparse-checkout set modules`, `infra/main.tf` MUST NOT
        # be in the working tree. Cone mode also includes top-level
        # files (`top.tf`) by design — that's documented sparse-checkout
        # cone behaviour, not a leak.
        files = sorted(
            str(p.relative_to(clone_dir))
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        assert "modules/vpc.tf" in files
        assert "infra/main.tf" not in files

    @pytest.mark.asyncio
    async def test_run_git_failure_raises_with_stderr(self, tmp_path):
        """Bogus arg → non-zero exit → stderr captured in exception."""
        with pytest.raises(RuntimeError, match="git"):
            await git_fetch._run_git(["this-is-not-a-git-subcommand"])

    @pytest.mark.asyncio
    async def test_non_hex_sha_rejected_before_running_git(self, tmp_path):
        """Defence-in-depth: non-hex SHAs are rejected at the entry
        point, before we ever shell out. Belt-and-braces against any
        future change in git's argument parsing."""
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.provider = "github"
        with pytest.raises(ValueError, match="non-hex SHA"):
            await git_fetch.sparse_archive_to_storage(
                conn,
                "o",
                "r",
                "--upload-pack=evil",
                None,
                "key",
                clone_dir=str(tmp_path),
            )

    @pytest.mark.asyncio
    async def test_short_hex_sha_accepted(self, tmp_path, monkeypatch):
        """4-64 hex chars passes validation. We don't actually fetch
        here — we trip on auth resolution after the validator (which
        proves the validator passed)."""
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.provider = "github"

        async def boom(_conn):
            raise RuntimeError("auth-reached")

        monkeypatch.setattr(git_fetch, "_resolve_auth", boom)

        with pytest.raises(RuntimeError, match="auth-reached"):
            await git_fetch.sparse_archive_to_storage(
                conn,
                "o",
                "r",
                "abc1234",
                None,
                "key",
                clone_dir=str(tmp_path),
            )


# ── Pipe + producer plumbing (no git) ──────────────────────────────────


class TestProducerThreadPipeSemantics:
    """The producer takes ownership of the write fd and closes it on
    success and on exception. The consumer must see EOF cleanly in
    both cases, otherwise an upload would hang forever.
    """

    @pytest.mark.asyncio
    async def test_consumer_sees_eof_on_producer_success(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "a.tf").write_text("a")

        import os

        read_fd, write_fd = os.pipe()
        # Run producer in a thread; consume in the foreground.
        producer = asyncio.to_thread(git_fetch._producer_thread, write_fd, str(wt))

        chunks: list[bytes] = []

        async def consume():
            async for c in git_fetch._consumer_chunks(read_fd):
                chunks.append(c)

        await asyncio.gather(producer, consume())
        # The chunks form a valid gzipped tar containing a.tf
        import io as _io

        with tarfile.open(fileobj=_io.BytesIO(b"".join(chunks)), mode="r:gz") as tf:
            assert sorted(m.name for m in tf.getmembers()) == ["a.tf"]

    @pytest.mark.asyncio
    async def test_consumer_sees_eof_on_producer_failure(self, tmp_path, monkeypatch):
        """If the tarball writer raises mid-stream, the producer's
        except block closes the fd so the consumer sees EOF and
        doesn't deadlock. Without this, an upload error during a
        sparse fetch would hang the request forever."""
        import os

        read_fd, write_fd = os.pipe()

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated tarball writer failure")

        monkeypatch.setattr(git_fetch, "_write_tarball_from_dir", boom)

        producer = asyncio.to_thread(git_fetch._producer_thread, write_fd, str(tmp_path))

        async def consume():
            async for _c in git_fetch._consumer_chunks(read_fd):
                pass

        results = await asyncio.gather(producer, consume(), return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        # Consumer completed cleanly because the producer closed the fd.
        assert results[1] is None
