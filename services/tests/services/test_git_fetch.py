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


# ── submodules (#1437) ─────────────────────────────────────────────────


def _git_env(home) -> dict:
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        # file:// submodules are refused by default since CVE-2022-39253.
        # The production path uses https, so this is a test-harness concern only.
        "GIT_ALLOW_PROTOCOL": "file",
    }


@pytest.fixture
def superproject_with_submodule(tmp_path):
    """A superproject whose `infra/vendored` is a real submodule.

    Returns (superproject_worktree, submodule_sha). The worktree stands in for
    the clone `_init_submodules` operates on, which is what the production code
    hands it.
    """
    env = _git_env(tmp_path)

    def run(cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    # The repository that becomes the submodule.
    sub = tmp_path / "sub"
    sub.mkdir()
    run(sub, "init", "--quiet", "-b", "main")
    (sub / "module.tf").write_text("# vendored module\n")
    run(sub, "add", "module.tf")
    run(sub, "commit", "--quiet", "-m", "sub")

    # The superproject, with the submodule under infra/.
    top = tmp_path / "top"
    top.mkdir()
    run(top, "init", "--quiet", "-b", "main")
    (top / "top.tf").write_text("# top\n")
    run(top, "add", "top.tf")
    run(
        top,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        f"file://{sub}",
        "infra/vendored",
    )
    run(top, "commit", "--quiet", "-m", "add submodule")

    # Put the superproject into the state a FRESH CLONE is actually in, which
    # is what production hands `_init_submodules`: the gitlink is recorded, the
    # working tree is empty, and `.git/config` carries no submodule entry.
    # `submodule add` registers that entry, and while it is present git reads
    # the URL from there rather than from `.gitmodules` — so a test that edits
    # `.gitmodules` would have no effect at all.
    run(top, "submodule", "deinit", "-f", "infra/vendored")
    # deinit leaves the submodule's objects in .git/modules, and `update --init`
    # will happily re-checkout from those without contacting the URL at all —
    # which would make a broken-URL test pass for the wrong reason.
    shutil.rmtree(top / ".git" / "modules", ignore_errors=True)
    return top


@pytestmark_git
class TestInitSubmodules:
    """A submodule has no working-tree content of its own, so packing the tree
    without initialising it produced empty directories and an `init` failure on
    a path that visibly exists in the repository (#1437)."""

    def _conn(self):
        conn = MagicMock()
        conn.name = "test-conn"
        conn.provider = "github"
        return conn

    @pytest.mark.asyncio
    async def test_content_is_materialised_for_a_needed_path(
        self, superproject_with_submodule, monkeypatch
    ):
        top = superproject_with_submodule
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
        assert not (top / "infra" / "vendored" / "module.tf").exists(), "precondition"

        await git_fetch._init_submodules(
            str(top), ["infra"], auth_header=None, host="github.com", conn=self._conn()
        )

        # The whole point: the packed tarball now carries the file, instead of
        # an empty directory that fails `init` later.
        assert (top / "infra" / "vendored" / "module.tf").read_text() == "# vendored module\n"

    @pytest.mark.asyncio
    async def test_a_narrowed_out_submodule_is_not_fetched(
        self, superproject_with_submodule, monkeypatch
    ):
        """A workspace using two directories must not pull every submodule in
        the repository."""
        top = superproject_with_submodule
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

        # Must be a clean no-op, not a failure: `submodule update -- <path>`
        # errors when the pathspec matches nothing, so a repository whose
        # narrowed paths contain no submodule would otherwise fail the fetch.
        await git_fetch._init_submodules(
            str(top), ["modules"], auth_header=None, host="github.com", conn=self._conn()
        )

        assert not (top / "infra" / "vendored" / "module.tf").exists()

    @pytest.mark.asyncio
    async def test_no_gitmodules_runs_no_git_at_all(self, tmp_path, monkeypatch):
        """Byte-identical behaviour for the repositories that have no
        submodules, which is almost all of them."""
        calls = []

        async def spy(*args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(git_fetch, "_run_git", spy)
        await git_fetch._init_submodules(
            str(tmp_path), ["infra"], auth_header=None, host="github.com", conn=self._conn()
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_the_git_invocation_is_blobless_scoped_and_never_remote(
        self, superproject_with_submodule, monkeypatch
    ):
        """Pin the flags rather than the transfer size: --remote is the one that
        would silently convert a pinned dependency into a floating one, and
        dropping --filter makes `submodule update` a deep clone of every
        submodule's full history."""
        seen = {}

        async def spy(args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

        monkeypatch.setattr(git_fetch, "_run_git", spy)
        await git_fetch._init_submodules(
            str(superproject_with_submodule),
            ["infra"],
            auth_header="Basic xxx",
            host="github.com",
            conn=self._conn(),
        )

        args = seen["args"]
        assert "--filter=blob:none" in args
        assert "--init" in args and "--recursive" in args
        assert "--remote" not in args, "would fetch the branch tip, not the pinned commit"
        # Scoped to the submodules that actually live under the sparse set,
        # not to the sparse paths themselves — `submodule update -- <path>`
        # errors when a pathspec matches no submodule.
        assert args[-2:] == ["--", "infra/vendored"]
        # The credential is scoped to the connection's host, and the SSH
        # rewrites are host-scoped too.
        assert seen["kwargs"]["auth_host"] == "github.com"
        assert seen["kwargs"]["extra_config"] == [
            "url.https://github.com/.insteadOf=git@github.com:",
            "url.https://github.com/.insteadOf=ssh://git@github.com/",
        ]

    @pytest.mark.asyncio
    async def test_an_unfetchable_submodule_fails_with_a_useful_message(
        self, superproject_with_submodule, monkeypatch
    ):
        """Git's own failure is a 404 that reads as "this repository does not
        exist", which sends people to look in the wrong place."""
        top = superproject_with_submodule
        # Point the submodule at a URL that cannot resolve.
        (top / ".gitmodules").write_text(
            '[submodule "infra/vendored"]\n'
            "\tpath = infra/vendored\n"
            "\turl = file:///nonexistent/nope.git\n"
        )
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

        with pytest.raises(RuntimeError) as e:
            await git_fetch._init_submodules(
                str(top), ["infra"], auth_header=None, host="github.com", conn=self._conn()
            )

        msg = str(e.value)
        # The four things the operator needs: which submodule, which URL, which
        # connection, and what to do about it.
        assert "infra/vendored" in msg
        assert "nonexistent/nope.git" in msg
        assert "test-conn" in msg
        assert "same credential" in msg.lower()

    @pytest.mark.asyncio
    async def test_the_failure_never_packs_empty_directories(
        self, superproject_with_submodule, monkeypatch
    ):
        """It fails the fetch rather than silently shipping the empty directory,
        which is the bug being fixed."""
        top = superproject_with_submodule
        (top / ".gitmodules").write_text(
            '[submodule "infra/vendored"]\n'
            "\tpath = infra/vendored\n"
            "\turl = file:///nonexistent/nope.git\n"
        )
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

        with pytest.raises(RuntimeError):
            await git_fetch._init_submodules(
                str(top), ["infra"], auth_header=None, host="github.com", conn=self._conn()
            )


class TestSubmoduleStepIsWiredIntoTheFetch:
    """The tests above drive `_init_submodules` directly, which proves the helper
    works and proves nothing about whether the fetch calls it. Deleting the call
    site leaves every one of them green — the exact shape of #1244, where a
    wrapper reached through an optional argument was never executed by the
    caller that matters. This drives the real entry point instead.
    """

    @pytest.mark.asyncio
    async def test_the_fetch_initialises_submodules_after_checkout(self, tmp_path, monkeypatch):
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        # A repository that declares a submodule under the sparse path.
        (clone_dir / ".gitmodules").write_text(
            '[submodule "infra/vendored"]\n\tpath = infra/vendored\n\turl = https://x/y.git\n'
        )

        calls: list[list[str]] = []

        async def spy_run_git(args, **kwargs):  # noqa: ARG001
            calls.append(list(args))

        async def fake_auth(_conn):
            return "Basic xxx"

        monkeypatch.setattr(git_fetch, "_run_git", spy_run_git)
        monkeypatch.setattr(git_fetch, "_resolve_auth", fake_auth)
        monkeypatch.setattr(git_fetch, "get_storage", lambda: _fake_storage([]))

        conn = MagicMock()
        conn.provider = "github"
        conn.server_url = None
        conn.name = "c"

        await git_fetch.sparse_archive_to_storage(
            conn, "org", "repo", "a" * 40, ["infra"], "key", clone_dir=str(clone_dir)
        )

        verbs = [c for c in calls if "submodule" in c]
        assert verbs, "the fetch never initialised submodules — the call site is gone"

        # And it must happen AFTER checkout: before it, the gitlink has not been
        # resolved and there is nothing for git to update.
        checkout_at = next(i for i, c in enumerate(calls) if "checkout" in c)
        submodule_at = next(i for i, c in enumerate(calls) if "submodule" in c)
        assert submodule_at > checkout_at
