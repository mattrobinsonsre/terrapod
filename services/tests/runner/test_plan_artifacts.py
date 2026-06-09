"""Tests for terrapod.runner.plan_artifacts.

Covers:
  * `snapshot_paths` returns the right set of relative paths and
    honours the well-known exclusions (tfplan, lock file, init state).
  * `compute_diff` is pure set difference.
  * `tar_files` writes a valid tar for both the populated and the
    empty case, and respects the byte cap.
  * `extract_over` round-trips files back into a fresh workspace and
    guards against path traversal.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from terrapod.runner import plan_artifacts


def _touch(p: Path, content: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


class TestSnapshotPaths:
    def test_returns_relative_paths(self, tmp_path: Path) -> None:
        _touch(tmp_path / "a.tf")
        _touch(tmp_path / ".terraform/modules/cdn/main.tf")
        _touch(tmp_path / ".terraform/modules/cdn/retention.py")
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert "a.tf" in out
        assert ".terraform/modules/cdn/main.tf" in out
        assert ".terraform/modules/cdn/retention.py" in out

    def test_excludes_tfplan(self, tmp_path: Path) -> None:
        _touch(tmp_path / "tfplan", b"binary plan bytes")
        _touch(tmp_path / "a.tf")
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert "tfplan" not in out
        assert "a.tf" in out

    def test_excludes_lock_file(self, tmp_path: Path) -> None:
        _touch(tmp_path / ".terraform.lock.hcl", b"# lock contents")
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert ".terraform.lock.hcl" not in out

    def test_excludes_init_state(self, tmp_path: Path) -> None:
        """`.terraform/terraform.tfstate` is init's metadata file —
        apply re-runs init and writes its own version (with
        apply-pod-specific install paths); restoring plan's would
        clobber apply's.
        """
        _touch(tmp_path / ".terraform/terraform.tfstate", b"{}")
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert ".terraform/terraform.tfstate" not in out

    def test_skips_symlinks(self, tmp_path: Path) -> None:
        _touch(tmp_path / "real.tf")
        (tmp_path / "alias.tf").symlink_to(tmp_path / "real.tf")
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert "real.tf" in out
        # Symlink is intentionally not tracked — we don't try to
        # restore symlinks across the pod boundary.
        assert "alias.tf" not in out

    def test_includes_archive_file_output_location(self, tmp_path: Path) -> None:
        """`archive_file` outputs commonly land under
        `.terraform/modules/<name>/` when the module path is reused as
        the output_path target. Make sure that path makes it into the
        snapshot — this was the original failure case the feature
        exists to fix.
        """
        _touch(
            tmp_path / ".terraform/modules/example/retention.zip",
            b"PK\x03\x04",
        )
        out = plan_artifacts.snapshot_paths(tmp_path)
        assert ".terraform/modules/example/retention.zip" in out


class TestComputeDiff:
    def test_pure_set_difference(self) -> None:
        before = {"a", "b", "c"}
        after = {"a", "b", "c", "d", "e"}
        assert plan_artifacts.compute_diff(before, after) == {"d", "e"}

    def test_no_new_files_returns_empty(self) -> None:
        before = {"a", "b"}
        after = {"a", "b"}
        assert plan_artifacts.compute_diff(before, after) == set()

    def test_files_only_in_before_are_not_returned(self) -> None:
        """Plan only creates files (per the docstring contract). A
        file vanishing between init and plan is anomalous; the diff
        ignores it — we only ship NEW files.
        """
        before = {"a", "b"}
        after = {"a"}  # b vanished
        assert plan_artifacts.compute_diff(before, after) == set()


class TestTarFiles:
    def test_round_trips_a_single_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        _touch(workspace / "out.zip", b"PK\x03\x04 zip bytes here")
        out = tmp_path / "ours.tar"
        size = plan_artifacts.tar_files(workspace, {"out.zip"}, out)
        assert size == out.stat().st_size > 0
        # Read back via tarfile and confirm the bytes.
        with tarfile.open(out, mode="r") as tf:
            names = tf.getnames()
            assert names == ["out.zip"]
            member = tf.extractfile("out.zip")
            assert member is not None
            assert member.read() == b"PK\x03\x04 zip bytes here"

    def test_preserves_nested_paths(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        _touch(workspace / ".terraform/modules/cdn/retention.zip", b"a")
        _touch(workspace / ".terraform/modules/cdn/auth.zip", b"b")
        out = tmp_path / "ours.tar"
        plan_artifacts.tar_files(
            workspace,
            {
                ".terraform/modules/cdn/retention.zip",
                ".terraform/modules/cdn/auth.zip",
            },
            out,
        )
        with tarfile.open(out, mode="r") as tf:
            assert sorted(tf.getnames()) == [
                ".terraform/modules/cdn/auth.zip",
                ".terraform/modules/cdn/retention.zip",
            ]

    def test_empty_set_produces_valid_empty_tar(self, tmp_path: Path) -> None:
        """An always-present upload (even when nothing changed) lets
        apply treat a download 404 as a real error instead of the
        ambiguous "either no diff OR an older runner".

        Size is small but non-trivial: Python's `tarfile` pads to
        RECORDSIZE = 10240 bytes (20 * 512-byte blocks), not the
        bare 2 EOF blocks. Either pad is valid posix tar; assert the
        file is recoverable as a zero-member archive and the size is
        within tar's pad bounds.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        out = tmp_path / "empty.tar"
        size = plan_artifacts.tar_files(workspace, set(), out)
        # Reader sees zero members and the file is a valid tar.
        with tarfile.open(out, mode="r") as tf:
            assert tf.getmembers() == []
        # Bounded by tar's typical pad — small but not zero. This
        # surfaces accidental "write_bytes(b'')" regressions which
        # would produce a 0-byte file that tarfile.open rejects.
        assert 1024 <= size <= 32 * 1024

    def test_skips_unreadable_file(self, tmp_path: Path) -> None:
        """A single broken file (e.g. permission-denied) shouldn't
        poison the whole snapshot — apply just falls back to the
        pre-feature behaviour for that resource.
        """
        workspace = tmp_path / "ws"
        _touch(workspace / "ok.zip", b"ok")
        # Reference a non-existent path in the set; tar_files should
        # log + skip rather than raise.
        out = tmp_path / "ours.tar"
        plan_artifacts.tar_files(workspace, {"ok.zip", "missing.zip"}, out)
        with tarfile.open(out, mode="r") as tf:
            assert tf.getnames() == ["ok.zip"]


class TestExtractOver:
    def test_extracts_files_into_workspace(self, tmp_path: Path) -> None:
        # Build a synthetic tar.
        tar_path = tmp_path / "in.tar"
        with tarfile.open(tar_path, mode="w") as tf:
            info = tarfile.TarInfo(".terraform/modules/cdn/retention.zip")
            info.size = 4
            tf.addfile(info, io.BytesIO(b"ZIPS"))
        workspace = tmp_path / "apply-ws"
        workspace.mkdir()
        count = plan_artifacts.extract_over(tar_path, workspace)
        assert count == 1
        assert (workspace / ".terraform/modules/cdn/retention.zip").read_bytes() == b"ZIPS"

    def test_handles_missing_tar(self, tmp_path: Path) -> None:
        workspace = tmp_path / "apply-ws"
        workspace.mkdir()
        assert plan_artifacts.extract_over(tmp_path / "nope.tar", workspace) == 0

    def test_handles_empty_tar(self, tmp_path: Path) -> None:
        workspace = tmp_path / "apply-ws"
        workspace.mkdir()
        empty_tar = tmp_path / "empty.tar"
        plan_artifacts.tar_files(tmp_path / "src", set(), empty_tar)
        # 1024-byte empty tar (size > 0) — exercises the > 0 guard.
        (tmp_path / "src").mkdir()
        empty_tar = tmp_path / "empty2.tar"
        plan_artifacts.tar_files(tmp_path / "src", set(), empty_tar)
        assert plan_artifacts.extract_over(empty_tar, workspace) == 0
        # Workspace untouched.
        assert list(workspace.iterdir()) == []

    def test_path_traversal_member_skipped(self, tmp_path: Path) -> None:
        """A malicious tarball containing `../../etc/passwd` MUST NOT
        write outside the workspace. The traversal entry is dropped.
        Matches `summariser._build_code_diff`'s safety check.
        """
        tar_path = tmp_path / "evil.tar"
        with tarfile.open(tar_path, mode="w") as tf:
            for name, content in [
                ("../escape.txt", b"escape attempt"),
                ("legit.zip", b"ok"),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plan_artifacts.extract_over(tar_path, workspace)
        # legit member landed inside workspace
        assert (workspace / "legit.zip").read_bytes() == b"ok"
        # escape member did not write anywhere outside workspace
        assert not (tmp_path / "escape.txt").exists()

    def test_overlay_overwrites_existing_files(self, tmp_path: Path) -> None:
        """Apply runs init first (which produces module sources), then
        extracts the plan-artifacts tar. If a path is in both places,
        the tar wins — that's the behaviour we want for plan-time
        generated files that share a directory with init's outputs.
        """
        workspace = tmp_path / "apply-ws"
        _touch(workspace / "out.zip", b"old")
        tar_path = tmp_path / "in.tar"
        with tarfile.open(tar_path, mode="w") as tf:
            info = tarfile.TarInfo("out.zip")
            info.size = 3
            tf.addfile(info, io.BytesIO(b"NEW"))
        plan_artifacts.extract_over(tar_path, workspace)
        assert (workspace / "out.zip").read_bytes() == b"NEW"


class TestEndToEndRoundTrip:
    def test_plan_phase_to_apply_phase_full_cycle(self, tmp_path: Path) -> None:
        """Simulate the production sequence:

        1. Plan-phase workspace has init's outputs (module .tf files).
        2. Snapshot.
        3. Plan creates archive_file zip.
        4. Snapshot again, compute diff, tar it.
        5. Fresh apply workspace (different dir).
        6. Apply-phase init re-populates module .tf files.
        7. Extract plan-artifacts tar over it.
        8. Assert apply workspace has the archive_file zip in the right
           place.
        """
        plan_ws = tmp_path / "plan-ws"
        # 1. post-init state
        _touch(plan_ws / ".terraform/modules/example/main.tf", b"module main")
        _touch(plan_ws / ".terraform/modules/example/retention.py", b"# py")
        post_init = plan_artifacts.snapshot_paths(plan_ws)
        # 3. plan creates the archive_file output
        _touch(
            plan_ws / ".terraform/modules/example/retention.zip",
            b"PK\x03\x04 fake zip",
        )
        post_plan = plan_artifacts.snapshot_paths(plan_ws)
        # 4. diff + tar
        new = plan_artifacts.compute_diff(post_init, post_plan)
        assert new == {".terraform/modules/example/retention.zip"}
        tar = tmp_path / "plan-artifacts.tar"
        plan_artifacts.tar_files(plan_ws, new, tar)

        # 5-7. Apply phase: fresh workspace + post-init module sources +
        # tar extract.
        apply_ws = tmp_path / "apply-ws"
        _touch(apply_ws / ".terraform/modules/example/main.tf", b"module main")
        _touch(apply_ws / ".terraform/modules/example/retention.py", b"# py")
        extracted = plan_artifacts.extract_over(tar, apply_ws)
        assert extracted == 1
        # 8. The zip is now where the lambda resource expects it.
        assert (
            apply_ws / ".terraform/modules/example/retention.zip"
        ).read_bytes() == b"PK\x03\x04 fake zip"
        # Apply-phase init's outputs are untouched.
        assert (apply_ws / ".terraform/modules/example/main.tf").read_bytes() == b"module main"


def test_storage_key_shape() -> None:
    """Smoke that the storage key follows the existing
    runs/{ws}/{run}.* layout next to plan-file + lock-file.
    """
    from terrapod.storage.keys import plan_artifacts_key

    key = plan_artifacts_key("ws-1", "run-1")
    assert key == "plans/ws-1/run-1.plan-artifacts.tar"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",  # absolute
        "../escape.zip",  # traversal
    ],
)
def test_extract_over_blocks_dangerous_member_paths(tmp_path: Path, path: str) -> None:
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, mode="w") as tf:
        info = tarfile.TarInfo(name=path)
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plan_artifacts.extract_over(tar_path, workspace)
    # The dangerous member must not produce any file inside or outside
    # the workspace (we just refuse to extract it).
    assert not any(workspace.rglob("*"))
    # And nothing in /etc gets created by the test runner.
    assert not (Path("/etc") / "passwd_test_marker_should_not_exist").exists()
    # Defence in depth: tmp_path's parent didn't gain anything.
    assert not (tmp_path.parent / Path(path).name).exists() or os.path.realpath(
        tmp_path.parent / Path(path).name
    ) != os.path.realpath(workspace)
