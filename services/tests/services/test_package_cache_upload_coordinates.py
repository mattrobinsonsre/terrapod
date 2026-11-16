"""`upload_coordinates`: an upload is the cache key arrived at from the other side (#2599).

Pure functions, so they belong here rather than in the integration tier.

**The property worth the file is PARITY**, and it is a relationship rather than a
per-ecosystem fact: the coordinates an upload resolves to must be the same
`(name, filename)` the ecosystem's own `artifact_for` resolves to on the fetch
path. Get that wrong and an upload is stored at a key nothing ever asks for —
every route answers 200, the row appears in the admin listing, and `pip install`
goes on reporting the package as uncached. So each ecosystem is tested with
BOTH sides instantiated, which is the only shape that can see it.

The second property is that the coordinates are **never validated against an
upstream index**. Nothing here does any I/O, which is how that is asserted: a
name upstream has never heard of resolves perfectly well.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from terrapod.services.package_cache import (
    galaxy,
    goproxy,
    npm,
    nuget,
    pulumi_plugins,
    pypi,
)
from terrapod.services.package_cache.substrate import CoordinateError

#: Each ecosystem, with a fetch-path Artifact and an upload-path Coordinates for
#: THE SAME artifact. Written as pairs so the parity test below cannot test one
#: side against itself.
PLUGIN = "pulumi-resource-aws-v6.0.0-linux-amd64.tar.gz"

PARITY = {
    "pypi": (
        lambda: pypi.artifact_for(
            "Flask",
            {
                "filename": "flask-3.0.0-py3-none-any.whl",
                "url": "https://upstream.test/flask-3.0.0-py3-none-any.whl",
                "hashes": {"sha256": "0" * 64},
            },
        ),
        lambda: pypi.upload_coordinates("Flask", "flask-3.0.0-py3-none-any.whl"),
    ),
    "npm": (
        lambda: npm.artifact_for(
            "@scope/pkg", "1.3.0", {"dist": {"tarball": "https://upstream.test/pkg-1.3.0.tgz"}}
        ),
        lambda: npm.upload_coordinates("@scope/pkg", "1.3.0"),
    ),
    "nuget": (
        lambda: nuget.package_artifact("Newtonsoft.Json", "13.0.3"),
        lambda: nuget.upload_coordinates("Newtonsoft.Json", "13.0.3"),
    ),
    "go": (
        lambda: goproxy.artifact_for("github.com/foo/bar", "v1.2.3", ".zip"),
        lambda: goproxy.upload_coordinates("github.com/foo/bar", "v1.2.3", ".zip"),
    ),
    "galaxy": (
        lambda: galaxy.artifact_for(
            "community",
            "general",
            "9.2.0",
            {
                "download_url": "https://upstream.test/community-general-9.2.0.tar.gz",
                "artifact": {"sha256": "0" * 64},
            },
        ),
        lambda: galaxy.upload_coordinates("community", "general", "9.2.0"),
    ),
    "pulumi": (
        lambda: pulumi_plugins.artifact_for(PLUGIN, pulumi_plugins.parse_filename(PLUGIN)),
        lambda: pulumi_plugins.upload_coordinates("resource", "aws", "6.0.0", "linux", "amd64"),
    ),
}


class TestTheUploadLandsWhereTheFetchWould:
    @pytest.mark.parametrize("ecosystem", sorted(PARITY))
    def test_name_version_filename_and_content_type_all_match(self, ecosystem: str) -> None:
        """All four fields, not just the two that form the key.

        `name` and `filename` are the unique constraint, so a mismatch there is a
        row nothing finds. `content_type` is what the object is stored and
        redirected with, and it differs per ecosystem — a Go `.zip` is
        `application/zip` and a Pulumi plugin is `application/gzip` — so taking
        the dataclass default would serve an artifact with the wrong type.
        """
        fetch, upload = PARITY[ecosystem]
        artifact, coordinates = fetch(), upload()

        assert coordinates.name == artifact.name
        assert coordinates.version == artifact.version
        assert coordinates.filename == artifact.filename
        assert coordinates.content_type == artifact.content_type


class TestEveryEcosystemCanBeUploadedTo:
    """Derived from the package, not listed.

    A new ecosystem gets a proxy, an index and a serve path; without this it
    could ship with no way to put a vetted artifact into it, and a deployment
    that seals the pull-through would simply be unable to use it. Deriving the
    subject is what turns that from an omission nobody notices into a failure.
    """

    @staticmethod
    def _ecosystem_modules():
        from terrapod.services import package_cache

        found = []
        for mod in pkgutil.iter_modules(package_cache.__path__):
            m = importlib.import_module(f"{package_cache.__name__}.{mod.name}")
            if isinstance(getattr(m, "ECOSYSTEM", None), str) and m.ECOSYSTEM:
                found.append(m)
        return found

    def test_each_one_exposes_upload_coordinates(self) -> None:
        modules = self._ecosystem_modules()
        # A floor, so a matcher that stops finding modules fails here rather than
        # passing vacuously on an empty list.
        assert len(modules) >= 6, f"only discovered {[m.__name__ for m in modules]}"
        missing = [
            m.__name__ for m in modules if not callable(getattr(m, "upload_coordinates", None))
        ]
        assert not missing, f"no upload path for: {missing}"

    def test_every_ecosystem_with_a_module_has_a_parity_pair_here(self) -> None:
        """Otherwise the parity test above silently covers five of six."""
        covered = set(PARITY)
        real = {m.ECOSYSTEM for m in self._ecosystem_modules()}
        assert real - covered == set(), f"no parity pair for: {sorted(real - covered)}"


class TestWhatIsRefused:
    """Only what has to be: these values become a cache key and a storage path.

    Nothing here is refused for not existing upstream, which is the point of the
    feature — see `TestUpstreamIsNeverConsulted`.
    """

    def test_pypi_refuses_a_filename_carrying_a_path(self) -> None:
        with pytest.raises(CoordinateError, match="no path in it"):
            pypi.upload_coordinates("flask", "../../etc/passwd")

    def test_pypi_refuses_a_project_name_with_a_separator(self) -> None:
        with pytest.raises(CoordinateError, match="not a PyPI project name"):
            pypi.upload_coordinates("flask/../evil", "flask-3.0.0.tar.gz")

    def test_npm_refuses_a_name_that_climbs(self) -> None:
        with pytest.raises(CoordinateError, match="not an npm package name"):
            npm.upload_coordinates("@scope/../evil", "1.0.0")

    def test_npm_refuses_a_version_that_is_not_one(self) -> None:
        with pytest.raises(CoordinateError, match="not a version"):
            npm.upload_coordinates("left-pad", "not-a-version")

    def test_nuget_refuses_a_bad_id(self) -> None:
        with pytest.raises(CoordinateError, match="not a NuGet id"):
            nuget.upload_coordinates("Newtonsoft/Json", "13.0.3")

    def test_go_refuses_a_suffix_that_is_not_one_of_the_three(self) -> None:
        """`go mod download` asks for exactly `.info`, `.mod` and `.zip`."""
        with pytest.raises(CoordinateError, match="not one of the files"):
            goproxy.upload_coordinates("github.com/foo/bar", "v1.2.3", ".tar.gz")

    def test_go_refuses_a_version_without_its_v(self) -> None:
        with pytest.raises(CoordinateError, match="starts 'v'"):
            goproxy.upload_coordinates("github.com/foo/bar", "1.2.3", ".zip")

    def test_galaxy_refuses_a_namespace_that_is_not_a_segment(self) -> None:
        with pytest.raises(CoordinateError, match="not a collection coordinate"):
            galaxy.upload_coordinates("Community", "general", "9.2.0")

    def test_pulumi_refuses_parts_that_do_not_assemble_into_a_plugin_name(self) -> None:
        """The one regex is the authority, so this is refused by rebuilding and
        re-parsing rather than by a second copy of its rules."""
        with pytest.raises(CoordinateError, match="not a plugin filename"):
            pulumi_plugins.upload_coordinates("resource", "aws", "6.0.0", "linux/..", "amd64")


class TestUpstreamIsNeverConsulted:
    """Requirement 3 on the issue: an operator must be able to upload something
    upstream never published — a patched provider, an internal collection, a
    rebuilt binary. If the coordinates were validated against an index, the
    feature would fail for its most important case.

    Asserted as behaviour rather than by inspecting the source: a name no
    registry carries resolves, and these functions are synchronous with no client
    to pass, so there is nowhere for a lookup to hide.
    """

    def test_a_project_pypi_has_never_heard_of_resolves(self) -> None:
        coordinates = pypi.upload_coordinates(
            "acme-internal-telemetry", "acme_internal_telemetry-0.1.0-py3-none-any.whl"
        )
        assert coordinates.name == "acme-internal-telemetry"
        assert coordinates.version == "0.1.0"

    def test_a_locally_patched_version_resolves(self) -> None:
        """The shape an operator's own rebuild takes: a version string no public
        registry will ever carry."""
        coordinates = npm.upload_coordinates("left-pad", "1.3.0-acme.1")
        assert coordinates.filename == "left-pad-1.3.0-acme.1.tgz"

    def test_an_internal_collection_resolves(self) -> None:
        coordinates = galaxy.upload_coordinates("acme", "internal", "0.0.1")
        assert coordinates.name == "acme.internal"
        assert coordinates.filename == "acme-internal-0.0.1.tar.gz"


class TestConveniencesThatMustNotChangeTheKey:
    def test_pypi_normalises_the_project_the_way_the_index_does(self) -> None:
        """`Zope.Interface`, `zope-interface` and `zope_interface` are one
        project, so they must be one row rather than three copies of the bytes."""
        for spelling in ("Zope.Interface", "zope-interface", "zope_interface"):
            assert pypi.upload_coordinates(spelling, "zope.interface-6.0.tar.gz").name == (
                "zope-interface"
            )

    def test_pulumi_accepts_the_version_with_or_without_its_v(self) -> None:
        """A practitioner reads `v6.0.0` off a release page and `6.0.0` out of a
        program; both are the same plugin and must not be two rows."""
        with_v = pulumi_plugins.upload_coordinates("resource", "aws", "v6.0.0", "linux", "amd64")
        without = pulumi_plugins.upload_coordinates("resource", "aws", "6.0.0", "linux", "amd64")
        assert with_v == without

    def test_nuget_lowercases_the_id_the_way_the_protocol_addresses_it(self) -> None:
        assert nuget.upload_coordinates("Newtonsoft.Json", "13.0.3").name == "newtonsoft.json"
