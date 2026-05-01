"""Tests for `git_fetch` — the dulwich-backed sparse VCS fetch.

The pure helpers (path normalisation / hashing, prefix matching, tree
walking, tar-producer behaviour) are exercised here against an
in-process dulwich repo fixture. The two-pass `_fetch_partial` is
exercised against a real local bare repo via `file://` URLs — that
proves the SHA we ask for is the SHA we get (not HEAD, not the default
branch). The network-facing `sparse_archive_to_storage` end-to-end
against real GitHub/GitLab is validated in Tilt.
"""

from __future__ import annotations

import os
import tarfile

import pytest
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

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


# ── _path_matches / _dir_intersects_paths ──────────────────────────────


class TestPathMatches:
    def test_empty_paths_means_everything(self):
        assert git_fetch._path_matches(b"any/file", [])

    def test_exact_match(self):
        assert git_fetch._path_matches(b"infra/main.tf", ["infra/main.tf"])

    def test_under_prefix(self):
        assert git_fetch._path_matches(b"infra/eks/main.tf", ["infra/eks"])

    def test_partial_segment_does_not_match(self):
        """`infra-prod/x` must NOT match the prefix `infra` — the matcher
        is segment-based."""
        assert not git_fetch._path_matches(b"infra-prod/x", ["infra"])

    def test_outside_paths(self):
        assert not git_fetch._path_matches(b"top.tf", ["infra"])


class TestDirIntersectsPaths:
    def test_empty_paths_descends_everywhere(self):
        assert git_fetch._dir_intersects_paths(b"anything", [])

    def test_dir_is_a_path(self):
        assert git_fetch._dir_intersects_paths(b"infra", ["infra"])

    def test_dir_contains_a_path(self):
        """`infra` should descend because `infra/eks` lives inside it."""
        assert git_fetch._dir_intersects_paths(b"infra", ["infra/eks"])

    def test_dir_under_a_path(self):
        """`infra/eks` should be entered when `infra` is requested."""
        assert git_fetch._dir_intersects_paths(b"infra/eks", ["infra"])

    def test_dir_unrelated(self):
        assert not git_fetch._dir_intersects_paths(b"docs", ["infra"])


# ── tree-walking + tar producer (in-process dulwich) ───────────────────


@pytest.fixture
def repo_with_files(tmp_path) -> tuple[Repo, bytes, list[tuple[bytes, int, bytes]]]:
    """Build a tiny dulwich repo with a known tree shape.

    Returns (repo, commit_sha, expected_full_blob_entries).

    Layout:
        top.tf
        infra/main.tf
        infra/eks/cluster.tf
        infra/eks/sub/vars.tf
        modules/vpc.tf
    """
    repo = Repo.init(str(tmp_path))
    files: dict[str, bytes] = {
        "top.tf": b"# root\n",
        "infra/main.tf": b"# infra root\n",
        "infra/eks/cluster.tf": b"# eks cluster\n",
        "infra/eks/sub/vars.tf": b"# eks sub vars\n",
        "modules/vpc.tf": b"# vpc module\n",
    }

    # Create blobs and a nested tree manually.
    blobs: dict[str, bytes] = {}  # path → blob_sha
    for path, content in files.items():
        blob = Blob.from_string(content)
        repo.object_store.add_object(blob)
        blobs[path] = blob.id

    # Build trees bottom-up. Map dir → list[(name, mode, sha)].
    dirs: dict[str, list[tuple[bytes, int, bytes]]] = {}
    for path, blob_sha in blobs.items():
        parts = path.split("/")
        for i in range(len(parts)):
            parent = "/".join(parts[:i])
            entry_name = parts[i].encode()
            if i == len(parts) - 1:
                # leaf: file
                dirs.setdefault(parent, []).append((entry_name, 0o100644, blob_sha))
            else:
                # placeholder; resolved below as we walk up
                dirs.setdefault(parent, [])  # ensure key exists

    # Walk paths to find subdirectory parents
    subdirs_of: dict[str, set[str]] = {}
    for path in blobs:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[: i - 1])
            child = parts[i - 1]
            subdirs_of.setdefault(parent, set()).add(child)

    # Now build trees — process deepest first.
    sorted_dirs = sorted(dirs.keys(), key=lambda p: -p.count("/") if p else 1)
    tree_shas: dict[str, bytes] = {}
    for d in sorted_dirs:
        t = Tree()
        # files in this dir
        for name, mode, sha in dirs[d]:
            t.add(name, mode, sha)
        # subdirs
        for sub in subdirs_of.get(d, set()):
            sub_path = f"{d}/{sub}" if d else sub
            t.add(sub.encode(), 0o040000, tree_shas[sub_path])
        repo.object_store.add_object(t)
        tree_shas[d] = t.id

    root_tree = tree_shas[""]
    commit = Commit()
    commit.tree = root_tree
    commit.author = commit.committer = b"Test <test@example.com>"
    commit.author_time = commit.commit_time = 1700000000
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"initial\n"
    repo.object_store.add_object(commit)

    expected_full = git_fetch._walk_tree_for_blobs(repo, root_tree, [])
    return repo, commit.id, expected_full


class TestWalkTreeForBlobs:
    def test_full_walk_finds_all_blobs(self, repo_with_files):
        repo, _, expected_full = repo_with_files
        # The fixture itself uses _walk_tree_for_blobs — assert it found 5 files
        assert len(expected_full) == 5
        names = sorted(p.decode() for p, _, _ in expected_full)
        assert names == [
            "infra/eks/cluster.tf",
            "infra/eks/sub/vars.tf",
            "infra/main.tf",
            "modules/vpc.tf",
            "top.tf",
        ]

    def test_narrowed_to_infra(self, repo_with_files):
        repo, commit_sha, _ = repo_with_files
        commit = repo[commit_sha]
        entries = git_fetch._walk_tree_for_blobs(repo, commit.tree, ["infra"])
        names = sorted(p.decode() for p, _, _ in entries)
        assert names == [
            "infra/eks/cluster.tf",
            "infra/eks/sub/vars.tf",
            "infra/main.tf",
        ]

    def test_narrowed_to_infra_eks_sub(self, repo_with_files):
        repo, commit_sha, _ = repo_with_files
        commit = repo[commit_sha]
        entries = git_fetch._walk_tree_for_blobs(repo, commit.tree, ["infra/eks/sub"])
        names = sorted(p.decode() for p, _, _ in entries)
        assert names == ["infra/eks/sub/vars.tf"]

    def test_narrowed_to_disjoint_paths(self, repo_with_files):
        repo, commit_sha, _ = repo_with_files
        commit = repo[commit_sha]
        entries = git_fetch._walk_tree_for_blobs(repo, commit.tree, ["modules", "infra/eks"])
        names = sorted(p.decode() for p, _, _ in entries)
        assert names == [
            "infra/eks/cluster.tf",
            "infra/eks/sub/vars.tf",
            "modules/vpc.tf",
        ]

    def test_unmatched_paths_returns_empty(self, repo_with_files):
        repo, commit_sha, _ = repo_with_files
        commit = repo[commit_sha]
        entries = git_fetch._walk_tree_for_blobs(repo, commit.tree, ["nonexistent"])
        assert entries == []


class TestProducerThread:
    """`_producer_thread` writes a deterministic gzipped tarball whose
    member layout is repo-rooted (no wrapper directory). The runner's
    `tar xzf --no-same-owner` consumer expects exactly this shape."""

    def test_writes_repo_rooted_tarball(self, repo_with_files, tmp_path):
        repo, _commit_sha, blob_entries = repo_with_files

        # Drive the producer manually: write side of pipe → file
        out_path = tmp_path / "out.tar.gz"
        write_fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            git_fetch._producer_thread(write_fd, repo, blob_entries)
        finally:
            # _producer_thread takes ownership and closes the fd via fdopen,
            # so we don't os.close() it here.
            pass

        # Now read back and verify
        with tarfile.open(out_path, "r:gz") as tf:
            members = {
                m.name: tf.extractfile(m).read() if not m.isdir() else b"" for m in tf.getmembers()
            }

        assert members == {
            "top.tf": b"# root\n",
            "infra/main.tf": b"# infra root\n",
            "infra/eks/cluster.tf": b"# eks cluster\n",
            "infra/eks/sub/vars.tf": b"# eks sub vars\n",
            "modules/vpc.tf": b"# vpc module\n",
        }

    def test_narrowed_tarball_omits_outside_paths(self, repo_with_files, tmp_path):
        repo, commit_sha, _ = repo_with_files
        commit = repo[commit_sha]
        narrowed = git_fetch._walk_tree_for_blobs(repo, commit.tree, ["infra/eks"])

        out_path = tmp_path / "narrow.tar.gz"
        write_fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        git_fetch._producer_thread(write_fd, repo, narrowed)

        with tarfile.open(out_path, "r:gz") as tf:
            names = sorted(m.name for m in tf.getmembers())
        assert names == ["infra/eks/cluster.tf", "infra/eks/sub/vars.tf"]


# ── _build_clone_url_sync ──────────────────────────────────────────────


class TestBuildCloneUrl:
    def test_github_default_host(self):
        url = git_fetch._build_clone_url_sync(
            "github", "https://api.github.com", "ghs_token", "owner", "repo"
        )
        assert url == "https://x-access-token:ghs_token@github.com/owner/repo.git"

    def test_github_enterprise_host_strips_api_prefix(self):
        url = git_fetch._build_clone_url_sync(
            "github",
            "https://api.ghe.example.com",
            "ghs_token",
            "owner",
            "repo",
        )
        assert url == "https://x-access-token:ghs_token@ghe.example.com/owner/repo.git"

    def test_gitlab_default_host(self):
        url = git_fetch._build_clone_url_sync("gitlab", None, "glpat_xxx", "group", "proj")
        assert url == "https://oauth2:glpat_xxx@gitlab.com/group/proj.git"

    def test_gitlab_self_hosted(self):
        url = git_fetch._build_clone_url_sync(
            "gitlab", "https://gitlab.example.com", "glpat_xxx", "group", "proj"
        )
        assert url == "https://oauth2:glpat_xxx@gitlab.example.com/group/proj.git"

    def test_token_with_special_chars_is_quoted(self):
        url = git_fetch._build_clone_url_sync("gitlab", None, "tok/en+with@chars", "g", "p")
        # Slashes / pluses / @ are quoted; the resulting URL has exactly
        # one `@` separator before the host.
        assert url.count("@") == 1
        assert url.startswith("https://oauth2:tok%2Fen%2Bwith%40chars@")


# ── _fetch_partial against a real local bare repo ──────────────────────


@pytest.fixture
def two_commit_bare_repo(tmp_path) -> tuple[str, str, str]:
    """Build a bare repo with TWO commits and return (file_url, sha1, sha2).

    sha1 is the first commit (file `a.tf` only). sha2 is HEAD (adds
    `b.tf`). The fetch test pulls sha1 — which is NOT HEAD — and
    verifies the tarball contains only `a.tf`. If `_fetch_partial`
    silently fell through to HEAD, `b.tf` would appear and the test
    would fail.
    """
    bare_path = tmp_path / "bare.git"
    bare_path.mkdir()
    bare = Repo.init_bare(str(bare_path))

    def _commit(parents: list[bytes], files: dict[str, bytes], msg: bytes) -> bytes:
        tree = Tree()
        for name, content in files.items():
            blob = Blob.from_string(content)
            bare.object_store.add_object(blob)
            tree.add(name.encode(), 0o100644, blob.id)
        bare.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.parents = parents
        c.author = c.committer = b"Test <test@example.com>"
        c.author_time = c.commit_time = 1700000000
        c.author_timezone = c.commit_timezone = 0
        c.message = msg
        bare.object_store.add_object(c)
        return c.id

    sha1 = _commit([], {"a.tf": b"# first\n"}, b"first\n")
    sha2 = _commit([sha1], {"a.tf": b"# first\n", "b.tf": b"# second\n"}, b"second\n")
    bare.refs[b"HEAD"] = sha2  # HEAD points at the SECOND commit

    file_url = f"file://{bare_path}"
    return file_url, sha1.decode(), sha2.decode()


class TestFetchPartialAgainstBareRepo:
    """Drives the real two-pass fetch via `LocalGitClient` (file://) so
    the SHA-resolution path is end-to-end exercised without a network."""

    def test_fetches_requested_sha_not_head(self, two_commit_bare_repo, tmp_path):
        """Asking for sha1 must return sha1's tree, not HEAD's."""
        file_url, sha1, sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        repo, blob_entries = git_fetch._fetch_partial(file_url, str(clone_dir), sha1, paths=[])

        # Only a.tf at sha1 (no b.tf). If the fetch had silently used
        # HEAD, b.tf would be in the entries and this assertion fails.
        names = sorted(p.decode() for p, _, _ in blob_entries)
        assert names == ["a.tf"]

        # The commit we got back IS the one we asked for.
        commit = repo[sha1.encode()]
        assert commit.id.decode() == sha1
        # And HEAD's commit was NOT the one we walked.
        assert sha1 != sha2

    def test_fetches_head_sha_returns_both_files(self, two_commit_bare_repo, tmp_path):
        """Sanity check: asking for sha2 returns both files."""
        file_url, _sha1, sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone2"
        clone_dir.mkdir()

        _repo, blob_entries = git_fetch._fetch_partial(file_url, str(clone_dir), sha2, paths=[])

        names = sorted(p.decode() for p, _, _ in blob_entries)
        assert names == ["a.tf", "b.tf"]

    def test_paths_narrowing_filters_blobs(self, two_commit_bare_repo, tmp_path):
        """Fetching sha2 with paths=['b.tf'] must return only b.tf."""
        file_url, _sha1, sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone3"
        clone_dir.mkdir()

        _repo, blob_entries = git_fetch._fetch_partial(
            file_url, str(clone_dir), sha2, paths=["b.tf"]
        )

        names = sorted(p.decode() for p, _, _ in blob_entries)
        assert names == ["b.tf"]

    def test_unknown_sha_raises(self, two_commit_bare_repo, tmp_path):
        """A bogus SHA causes the fetch to fail loudly — no silent
        fallback to HEAD."""
        file_url, _sha1, _sha2 = two_commit_bare_repo
        clone_dir = tmp_path / "clone4"
        clone_dir.mkdir()

        bogus = "0" * 40
        with pytest.raises(Exception):  # noqa: B017 — dulwich raises various subclasses
            git_fetch._fetch_partial(file_url, str(clone_dir), bogus, paths=[])
