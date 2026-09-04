"""Publishing Ansible collections (#1482).

`ansible-galaxy collection publish` sends one multipart POST carrying the
tarball and nothing else, so every fact about a published collection is read out
of the archive. These tests exercise that reading, because it is the only thing
standing between an upload and what the registry then asserts about it.

The archives here are built in-process rather than fixtured, so a test that
claims "a collection without a manifest is refused" is refused for that reason
and not because a checked-in binary drifted.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from terrapod.services import registry_collection_service as collections


def _archive(
    tmp_path,
    *,
    manifest: dict | None = None,
    nested: bool = False,
    name: str = "c.tar.gz",
    extra_depth: bool = False,
) -> str:
    """A gzipped tar shaped like a collection artifact."""
    path = str(tmp_path / name)
    with tarfile.open(path, "w:gz") as tar:
        if manifest is not None:
            raw = json.dumps(manifest).encode()
            prefix = "acme-widgets-1.0.0/" if nested else ""
            if extra_depth:
                prefix = "a/b/"
            info = tarfile.TarInfo(f"{prefix}MANIFEST.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        payload = b"whatever"
        info = tarfile.TarInfo("README.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return path


def _info(**overrides) -> dict:
    base = {
        "namespace": "acme",
        "name": "widgets",
        "version": "1.0.0",
        "dependencies": {"ansible.posix": ">=1.0.0"},
        "tags": ["demo"],
    }
    base.update(overrides)
    return {"collection_info": base}


class TestReadingTheManifest:
    def test_a_bare_manifest_is_found(self, tmp_path) -> None:
        info = collections._read_manifest(_archive(tmp_path, manifest=_info()))
        assert info["namespace"] == "acme"

    def test_a_manifest_under_one_directory_is_found(self, tmp_path) -> None:
        """Both layouts come out of `ansible-galaxy collection build`.

        Which one you get depends on the ansible-core version. Refusing either
        would reject a perfectly good artifact for a cosmetic reason.
        """
        info = collections._read_manifest(_archive(tmp_path, manifest=_info(), nested=True))
        assert info["name"] == "widgets"

    def test_a_manifest_buried_deeper_is_not_found(self, tmp_path) -> None:
        """Depth is bounded, so a stray MANIFEST.json inside the collection's
        own files cannot be mistaken for the collection's own."""
        with pytest.raises(collections.PublishError, match="no MANIFEST.json"):
            collections._read_manifest(_archive(tmp_path, manifest=_info(), extra_depth=True))

    def test_an_archive_without_one_is_refused(self, tmp_path) -> None:
        with pytest.raises(collections.PublishError, match="no MANIFEST.json"):
            collections._read_manifest(_archive(tmp_path, manifest=None))

    def test_a_non_archive_is_refused_as_unreadable(self, tmp_path) -> None:
        path = tmp_path / "not.tar.gz"
        path.write_bytes(b"this is not a gzip stream")
        with pytest.raises(collections.PublishError, match="could not read the archive"):
            collections._read_manifest(str(path))

    def test_invalid_json_is_refused(self, tmp_path) -> None:
        path = str(tmp_path / "bad.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            raw = b"{not json"
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        with pytest.raises(collections.PublishError, match="not valid JSON"):
            collections._read_manifest(path)

    def test_a_manifest_without_collection_info_is_refused(self, tmp_path) -> None:
        with pytest.raises(collections.PublishError, match="no collection_info"):
            collections._read_manifest(_archive(tmp_path, manifest={"files": []}))


class TestCoordinatesComeFromTheManifest:
    """The request carries no coordinates, so these are the only ones there are.

    Which makes validating them a boundary rather than a nicety: they name a
    namespace someone else may own and are interpolated into a storage key.
    """

    def test_a_good_manifest_yields_its_coordinates(self) -> None:
        assert collections._coordinates(_info()["collection_info"]) == (
            "acme",
            "widgets",
            "1.0.0",
        )

    @pytest.mark.parametrize(
        "bad",
        ["../etc", "Acme", "with-dash", "with space", "", "a/b", "http://x"],
    )
    def test_an_unusable_namespace_is_refused(self, bad: str) -> None:
        with pytest.raises(collections.PublishError, match="unusable namespace"):
            collections._coordinates(_info(namespace=bad)["collection_info"])

    @pytest.mark.parametrize("bad", ["../1.0.0", "1.0.0/../..", "a b", ""])
    def test_an_unusable_version_is_refused(self, bad: str) -> None:
        with pytest.raises(collections.PublishError, match="unusable version"):
            collections._coordinates(_info(version=bad)["collection_info"])

    @pytest.mark.parametrize("good", ["1.0.0", "2.1.0-rc1", "1.0.0+build.5"])
    def test_semver_tails_are_accepted(self, good: str) -> None:
        assert collections._coordinates(_info(version=good)["collection_info"])[2] == good

    def test_a_missing_field_is_refused_rather_than_defaulted(self) -> None:
        info = _info()["collection_info"]
        del info["version"]
        with pytest.raises(collections.PublishError, match="unusable version"):
            collections._coordinates(info)


class TestTheDigestDescribesWhatWasStored:
    """`ansible-galaxy` checks received bytes against the digest we advertise.

    So it has to be computed from the file, not copied from anything the client
    asserted — a digest describing something else turns a verified install into
    a broken one, and the client's error would point at the network.
    """

    def test_it_matches_the_file(self, tmp_path) -> None:
        import hashlib

        path = _archive(tmp_path, manifest=_info())
        sha, size = collections._digest_and_size(path)
        raw = open(path, "rb").read()
        assert sha == hashlib.sha256(raw).hexdigest()
        assert size == len(raw)

    def test_it_is_computed_in_bounded_chunks(self, tmp_path) -> None:
        """Larger than one read, so the loop is actually exercised."""
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * (collections._CHUNK * 2 + 17))
        sha, size = collections._digest_and_size(str(path))
        assert size == collections._CHUNK * 2 + 17
        assert len(sha) == 64


class TestReadingTheManifestBytesForSigning:
    """Signatures are made over the file as it sits in the archive.

    Re-serialising the parsed manifest would produce a different byte string and
    fail verification against a perfectly good signature, so the raw bytes are
    read rather than reconstructed.
    """

    def test_the_bytes_are_verbatim(self, tmp_path) -> None:
        manifest = _info()
        # Deliberately unusual spacing: a re-serialised copy would not match.
        raw = json.dumps(manifest, indent=3).encode()
        path = str(tmp_path / "m.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        assert collections._read_manifest_bytes(path) == raw

    def test_an_archive_without_one_is_refused(self, tmp_path) -> None:
        with pytest.raises(collections.PublishError, match="no MANIFEST.json"):
            collections._read_manifest_bytes(_archive(tmp_path, manifest=None))
