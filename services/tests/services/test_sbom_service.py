"""Inventorying a cached artifact into an SBOM (#2589 step 2b).

**These exist because the plausible implementation inventories nothing and
reports success.** #2589's own measurement was a Go binary -- 275 components --
and concluded the machinery works. It does, for binaries. Measured against what
the package cache actually holds, with syft 1.57.2:

| source | real packages |
|---|---|
| `file:` a `.whl` or an npm `.tgz` | **0** |
| `dir:` a directory *containing* the archive | **0** |
| `dir:` the extracted npm tarball, default catalogers | **0** |
| `dir:` extracted, `--override-default-catalogers all` | lodash 4.17.21 |

Two independent causes: syft has no generic archive scheme (`--help` lists only
docker-archive, oci-archive, oci-dir, dir and file), and its default DIRECTORY
catalogers exclude javascript's installed cataloger.

Every one of those zero rows produced a **well-formed CycloneDX 1.7 document**.
So a schema check, or "did syft exit 0", passes all of them -- which is why the
assertions here are on the count of purl-bearing packages and on the argv, not
on the document being valid.
"""

from __future__ import annotations

import json
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import sbom_service as sb


def _wheel(tmp_path: Path, name: str = "pkg-1.0-py3-none-any.whl") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pkg/__init__.py", "x = 1\n")
        zf.writestr("pkg-1.0.dist-info/METADATA", "Name: pkg\nVersion: 1.0\n")
    return path


def _tarball(tmp_path: Path, name: str = "pkg-1.0.tgz") -> Path:
    path = tmp_path / name
    inner = tmp_path / "package"
    inner.mkdir(exist_ok=True)
    (inner / "package.json").write_text(json.dumps({"name": "pkg", "version": "1.0.0"}))
    with tarfile.open(path, "w:gz") as tar:
        tar.add(inner, arcname="package")
    return path


class TestArchiveDetection:
    """What gets extracted and what syft reads directly.

    By suffix rather than magic bytes, deliberately: the caches name objects
    after what upstream published, so the suffix is upstream's own statement of
    the format.
    """

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("flask-3.0.0-py3-none-any.whl", "zip"),
            ("thing.nupkg", "zip"),
            ("thing.jar", "zip"),
            ("lodash-4.17.21.tgz", "tar"),
            ("thing.tar.gz", "tar"),
            ("thing.tar.xz", "tar"),
            # Not archives: syft reads these directly, and a Go binary is the
            # case that works without any of this.
            ("syft", None),
            ("terraform", None),
            ("opa_linux_amd64_static", None),
        ],
    )
    def test_detection(self, name: str, expected: str | None) -> None:
        assert sb._is_archive(Path(name)) == expected


class TestPackagesIn:
    """The metric that tells a real inventory from an empty one."""

    def test_only_components_with_a_purl_are_packages(self) -> None:
        """A wheel's component list is mostly FILE entries. Counting the list
        reads as thirty times more inventory than there is -- the real wheel
        measured 32 components and one package.
        """
        document = {
            "components": [
                {"name": "flask", "version": "3.0.0", "purl": "pkg:pypi/flask@3.0.0"},
                {"name": "/flask-3.0.0.dist-info/METADATA"},
                {"name": "/flask-3.0.0.dist-info/RECORD"},
                "not-a-dict",
            ]
        }
        assert sb.packages_in(document) == 1

    def test_an_empty_document_is_zero_not_an_error(self) -> None:
        assert sb.packages_in({}) == 0
        assert sb.packages_in({"components": []}) == 0


class TestExtractionIsBounded:
    """An artifact is whatever upstream served, so it is attacker-influenced."""

    def test_a_zip_member_escaping_the_root_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "evil.whl"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escaped.txt", "x")
        into = tmp_path / "out"
        into.mkdir()

        with pytest.raises(ValueError, match="escaping the root"):
            sb._extract_zip(archive, into)
        assert not (tmp_path / "escaped.txt").exists()

    def test_too_many_zip_members_are_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "many.whl"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "x")
        into = tmp_path / "out"
        into.mkdir()

        with patch.object(sb, "_MAX_EXTRACTED_MEMBERS", 0):
            with pytest.raises(ValueError, match="members"):
                sb._extract_zip(archive, into)

    def test_an_oversized_zip_is_refused(self, tmp_path: Path) -> None:
        """The decompression-bomb bound: a small archive can declare a large
        expansion, and the cost must be bounded rather than the whole PVC."""
        archive = tmp_path / "bomb.whl"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\0" * 100_000)
        into = tmp_path / "out"
        into.mkdir()

        with patch.object(sb, "_MAX_EXTRACTED_BYTES", 1000):
            with pytest.raises(ValueError, match="extraction bound"):
                sb._extract_zip(archive, into)

    def test_an_oversized_tar_is_refused(self, tmp_path: Path) -> None:
        archive = _tarball(tmp_path)
        into = tmp_path / "out"
        into.mkdir()

        with patch.object(sb, "_MAX_EXTRACTED_BYTES", 1):
            with pytest.raises(ValueError, match="extraction bound"):
                sb._extract_tar(archive, into)

    def test_an_ordinary_archive_extracts(self, tmp_path: Path) -> None:
        """The control. Without it, an extractor that refused everything would
        pass every case above."""
        archive = _wheel(tmp_path)
        into = tmp_path / "out"
        into.mkdir()

        sb._extract_zip(archive, into)

        assert (into / "pkg" / "__init__.py").read_text() == "x = 1\n"
        assert (into / "pkg-1.0.dist-info" / "METADATA").exists()


class _FakeProcess:
    """Stands in for syft, recording nothing and returning what it is told."""

    def __init__(self, stdout: bytes = b"{}", returncode: int = 0) -> None:
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - only the timeout path
        pass

    async def wait(self) -> None:  # pragma: no cover
        pass


def _spawned(argv_sink: list, **kwargs):
    """Patch the subprocess boundary BELOW the code under test.

    Patching `_scan` would leave these asserting on a function that never runs;
    the whole value here is WHICH argv syft is given.
    """

    async def _exec(*argv, **_kw):
        argv_sink.append(list(argv))
        return _FakeProcess(**kwargs)

    return patch("asyncio.create_subprocess_exec", new=_exec)


@pytest.mark.asyncio
class TestInventoryWiring:
    """What `inventory` actually asks syft for.

    Both assertions here guard a silent-empty failure rather than a crash: get
    either wrong and syft returns a well-formed document with no packages in it.
    """

    async def test_an_archive_is_extracted_and_scanned_as_a_directory(self, tmp_path) -> None:
        """`file:` on a wheel inventories NOTHING -- measured. It must be
        extracted and handed to syft as `dir:`."""
        archive = _wheel(tmp_path)
        argv: list = []
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned(argv):
                await sb.inventory(archive)

        assert len(argv) == 1, argv
        target = argv[0][1]
        assert target.startswith("dir:"), f"an archive was scanned as {target!r}"
        assert not target.endswith(".whl")

    async def test_a_plain_file_is_scanned_directly(self, tmp_path) -> None:
        """The control. A Go binary works as `file:` and must not be dragged
        through an extractor that would fail on it."""
        binary = tmp_path / "syft"
        binary.write_bytes(b"\x7fELF not really")
        argv: list = []
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned(argv):
                await sb.inventory(binary)

        assert argv[0][1] == f"file:{binary}"

    async def test_the_cataloger_override_is_always_passed(self, tmp_path) -> None:
        """Without it an extracted npm tarball reports "No packages discovered"
        -- a valid, empty document. This is the one flag between a real npm
        inventory and silence.
        """
        archive = _tarball(tmp_path)
        argv: list = []
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned(argv):
                await sb.inventory(archive)

        assert "--override-default-catalogers" in argv[0]
        assert argv[0][argv[0].index("--override-default-catalogers") + 1] == "all"

    async def test_cyclonedx_is_what_is_requested(self, tmp_path) -> None:
        """Because `trivy sbom` consumes it -- the reason there is no third
        tool. A different format would make the matching half silently useless.
        """
        binary = tmp_path / "thing"
        binary.write_bytes(b"x")
        argv: list = []
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned(argv):
                await sb.inventory(binary)

        assert argv[0][argv[0].index("-o") + 1] == "cyclonedx-json"

    async def test_the_extraction_directory_is_cleaned_up(self, tmp_path) -> None:
        """The PVC is shared and finite; an inventory per admission that left
        its extraction behind would fill it."""
        archive = _wheel(tmp_path)
        root = tmp_path / "tools"
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with patch("terrapod.services.api_tool.tool_dir", return_value=root):
                with _spawned([]):
                    await sb.inventory(archive)

        leftovers = list((root / "sbom").glob("inv.*")) if (root / "sbom").exists() else []
        assert leftovers == [], leftovers


@pytest.mark.asyncio
class TestEveryFailureDegrades:
    """An SBOM is evidence, never a gate (#2589, and #2599 for uploads).

    So none of these may raise: admission has already happened, and gating on
    our own scan would second-guess the attestor we just said we trust.
    """

    async def test_no_syft_is_none_not_an_error(self, tmp_path) -> None:
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value=None)):
            assert await sb.inventory(tmp_path / "x") is None

    async def test_a_nonzero_exit_is_none(self, tmp_path) -> None:
        binary = tmp_path / "thing"
        binary.write_bytes(b"x")
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned([], returncode=2):
                assert await sb.inventory(binary) is None

    async def test_output_that_is_not_json_is_none(self, tmp_path) -> None:
        binary = tmp_path / "thing"
        binary.write_bytes(b"x")
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned([], stdout=b"not json at all"):
                assert await sb.inventory(binary) is None

    async def test_a_json_array_is_none(self, tmp_path) -> None:
        """Valid JSON that is not a document. `document.get(...)` would raise on
        a list, and this runs after admission where an exception is worse than
        an absent SBOM."""
        binary = tmp_path / "thing"
        binary.write_bytes(b"x")
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with _spawned([], stdout=b"[]"):
                assert await sb.inventory(binary) is None

    async def test_an_unextractable_archive_is_none(self, tmp_path) -> None:
        """A `.whl` that is not a zip at all -- a truncated download, or an
        upstream serving an error page under the right name."""
        broken = tmp_path / "broken.whl"
        broken.write_bytes(b"this is not a zip")
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            assert await sb.inventory(broken) is None

    async def test_syft_not_executable_is_none(self, tmp_path) -> None:
        binary = tmp_path / "thing"
        binary.write_bytes(b"x")
        with patch.object(sb, "syft_binary", new=AsyncMock(return_value="/x/syft")):
            with patch("asyncio.create_subprocess_exec", side_effect=OSError("no such file")):
                assert await sb.inventory(binary) is None
