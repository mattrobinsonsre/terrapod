"""Registry submodules (#1583): the subdirectory normaliser and the archive scoping."""

import io
import tarfile

import pytest

from terrapod.services.module_hcl_parser import extract_module_interface
from terrapod.services.module_subdirectory import (
    SubdirectoryError,
    normalize_subdirectory,
    scope_archive_to_subdirectory,
)


class TestNormalizeSubdirectory:
    @pytest.mark.parametrize("value", [None, "", "   ", "/", "//"])
    def test_the_repository_root(self, value):
        assert normalize_subdirectory(value) == ""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("modules/create", "modules/create"),
            ("/modules/create/", "modules/create"),
            ("  modules/create  ", "modules/create"),
            ("a", "a"),
        ],
    )
    def test_canonical_form(self, value, expected):
        assert normalize_subdirectory(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "../x",
            "modules/../x",
            "./modules",
            "modules/./create",
            "modules//create",
            "a\\b",
            "a\x00b",
        ],
    )
    def test_refuses_paths_that_escape_or_are_ambiguous(self, value):
        with pytest.raises(SubdirectoryError):
            normalize_subdirectory(value)

    def test_refuses_an_overlong_path(self):
        with pytest.raises(SubdirectoryError):
            normalize_subdirectory("a/" * 300)


def _archive(files: dict[str, bytes], links: dict[str, str] | None = None, dirs=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for d in dirs:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.LNKTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


def _members(archive: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        return {m.name: m for m in tf.getmembers()}


def _read(archive: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        return tf.extractfile(name).read()


REPO = {
    "main.tf": b'variable "root_only" {}\n',
    "modules/create/main.tf": b'resource "null_resource" "x" {}\n',
    "modules/create/variables.tf": b'variable "name" {\n  type = string\n}\n',
    "modules/create/outputs.tf": b'output "id" {\n  value = "x"\n}\n',
    "modules/create/sub/nested.tf": b"# nested\n",
    "modules/create-extra/main.tf": b"# a sibling that shares the string prefix\n",
}


class TestScopeArchiveToSubdirectory:
    def test_keeps_only_the_subdirectory_re_rooted(self):
        scoped = scope_archive_to_subdirectory(
            _archive(REPO, dirs=["modules/create"]), "modules/create"
        )

        names = {n for n, m in _members(scoped).items() if not m.isdir()}
        assert names == {"main.tf", "variables.tf", "outputs.tf", "sub/nested.tf"}
        assert _read(scoped, "main.tf") == REPO["modules/create/main.tf"]

    def test_a_sibling_sharing_the_prefix_stays_out(self):
        scoped = scope_archive_to_subdirectory(_archive(REPO), "modules/create")
        assert not any("extra" in n for n in _members(scoped))
        assert b"sibling" not in b"".join(
            _read(scoped, n) for n, m in _members(scoped).items() if m.isfile()
        )

    def test_nothing_under_the_subdirectory_is_none(self):
        assert scope_archive_to_subdirectory(_archive(REPO), "modules/missing") is None

    def test_only_directories_under_it_is_none(self):
        # A directory entry alone is not a module.
        archive = _archive({"main.tf": b""}, dirs=["modules/empty"])
        assert scope_archive_to_subdirectory(archive, "modules/empty") is None

    def test_hard_links_inside_are_rewritten_and_outside_are_dropped(self):
        archive = _archive(
            REPO,
            links={
                "modules/create/alias.tf": "modules/create/main.tf",
                "modules/create/escape.tf": "main.tf",
            },
        )
        members = _members(scope_archive_to_subdirectory(archive, "modules/create"))

        assert members["alias.tf"].islnk() and members["alias.tf"].linkname == "main.tf"
        assert "escape.tf" not in members

    def test_leading_dot_slash_names_are_matched(self):
        archive = _archive({"./modules/create/main.tf": b"# dotted\n"})
        scoped = scope_archive_to_subdirectory(archive, "modules/create")
        assert set(_members(scoped)) == {"main.tf"}

    def test_the_scoped_archive_gives_the_submodules_interface(self):
        # The point of re-rooting: the unchanged root-files parser now reads the
        # submodule, not the repository root.
        scoped = scope_archive_to_subdirectory(_archive(REPO), "modules/create")
        iface = extract_module_interface(scoped)

        assert [i["name"] for i in iface["inputs"]] == ["name"]
        assert [o["name"] for o in iface["outputs"]] == ["id"]
