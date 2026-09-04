"""Galaxy v3 document handling (#1482).

The surface these are written against was captured from a real
`ansible-galaxy` — see `docs/galaxy-cli-surface.md` and
`scripts/galaxy-capture.py`. Four of its properties are things the documentation
would have led us to get wrong, and each has a test here, because the point of
capturing them was to stop them being re-guessed.

The rewrite tests carry most of the weight. An install that resolves through
Terrapod and then downloads from `galaxy.ansible.com` behaves identically to a
working one until someone tries it with no route out, so every field the client
*follows* is asserted to be ours — and, just as deliberately, the descriptive
metadata beside them is asserted to be left alone.
"""

from __future__ import annotations

import pytest

from terrapod.config import settings
from terrapod.services.package_cache import galaxy
from terrapod.services.package_cache.substrate import UpstreamError

BASE = "https://terrapod.example.com/api/terrapod/v1/package-cache/galaxy"
UPSTREAM = "https://galaxy.ansible.com"

COLLECTION = {
    "href": f"{UPSTREAM}/api/v3/collections/community/general/",
    "namespace": "community",
    "name": "general",
    "deprecated": False,
    "versions_url": f"{UPSTREAM}/api/v3/collections/community/general/versions/",
    "highest_version": {"version": "9.0.0", "href": f"{UPSTREAM}/…/versions/9.0.0/"},
}

VERSIONS = {
    "meta": {"count": 2},
    "links": {"next": f"{UPSTREAM}/api/v3/…?offset=100", "first": None, "last": None},
    "data": [
        {"version": "8.0.0", "href": f"{UPSTREAM}/…/versions/8.0.0/"},
        {"version": "9.0.0", "href": f"{UPSTREAM}/…/versions/9.0.0/"},
    ],
}

VERSION = {
    "version": "9.0.0",
    "href": f"{UPSTREAM}/api/v3/collections/community/general/versions/9.0.0/",
    "namespace": {"name": "community"},
    "collection": {"name": "general"},
    "artifact": {
        "filename": "community-general-9.0.0.tar.gz",
        "sha256": "b" * 64,
        "size": 4096,
    },
    "download_url": f"{UPSTREAM}/download/community-general-9.0.0.tar.gz",
    "metadata": {"dependencies": {"ansible.posix": "*"}, "tags": ["linux"]},
    "requires_ansible": ">=2.15",
}


#: The fields ansible-galaxy actually *fetches*. Every one has to be ours, and
#: any one left upstream is enough for an install to leave the network on a
#: machine that has none.
#:
#: Deliberately a list of fields rather than "every URL anywhere in the
#: document". A real version detail carries `documentation`, `repository`,
#: `homepage` and `issues` from the collection's own galaxy.yml — descriptive
#: metadata the client never follows, and rewriting a project's GitHub link to
#: point at Terrapod would be actively wrong. A whole-document assertion looks
#: stricter and is really just false: it passes on a synthetic fixture and fails
#: on the first realistic one, which is how it was caught here (a live install
#: of ansible.posix, whose metadata carries eight such links).
FOLLOWED_FIELDS = ("href", "versions_url", "download_url")


def _followed_urls(document: dict) -> list[str]:
    """Every URL the client would fetch, including nested ones."""
    found = [document[f] for f in FOLLOWED_FIELDS if isinstance(document.get(f), str)]
    for entry in document.get("data") or []:
        if isinstance(entry, dict):
            found.extend(_followed_urls(entry))
    highest = document.get("highest_version")
    if isinstance(highest, dict):
        found.extend(_followed_urls(highest))
    return found


class TestNoFollowedURLPointsUpstream:
    """The property that actually matters, asserted across every followed field.

    Checking `download_url` alone would pass a rewrite that left `href` or a
    version entry pointing upstream.
    """

    def test_collection_detail(self) -> None:
        out = galaxy.rewrite_collection(COLLECTION, BASE, "community", "general")
        assert _followed_urls(out), "the fixture must contain them, or this proves nothing"
        assert all(u.startswith(BASE) for u in _followed_urls(out)), _followed_urls(out)

    def test_version_list(self) -> None:
        out = galaxy.rewrite_versions(VERSIONS, BASE, "community", "general")
        assert _followed_urls(out)
        assert all(u.startswith(BASE) for u in _followed_urls(out)), _followed_urls(out)

    def test_version_detail(self) -> None:
        out = galaxy.rewrite_version(VERSION, BASE, "community", "general", "9.0.0")
        assert _followed_urls(out)
        assert all(u.startswith(BASE) for u in _followed_urls(out)), _followed_urls(out)

    def test_descriptive_metadata_is_left_alone(self) -> None:
        """A project's own links are not ours to rewrite.

        `repository` and `documentation` describe where the collection lives;
        pointing them at Terrapod would be a lie, and the client never fetches
        them anyway. Taken from a real ansible.posix document.
        """
        detail = {
            **VERSION,
            "metadata": {
                **VERSION["metadata"],
                "repository": "https://github.com/ansible-collections/ansible.posix",
                "documentation": "https://docs.ansible.com/ansible/latest/collections/",
            },
        }
        out = galaxy.rewrite_version(detail, BASE, "community", "general", "9.0.0")
        assert (
            out["metadata"]["repository"] == "https://github.com/ansible-collections/ansible.posix"
        )


class TestHrefIsPresent:
    """Captured finding: `href` is load-bearing.

    Omitting it aborts the install with a bare `KeyError` reported as a suspected
    client bug — a failure that points nowhere near the cause. It is cheap to
    pin and expensive to rediscover.
    """

    def test_on_the_collection(self) -> None:
        assert galaxy.rewrite_collection(COLLECTION, BASE, "community", "general")["href"]

    def test_on_the_version(self) -> None:
        assert galaxy.rewrite_version(VERSION, BASE, "community", "general", "9.0.0")["href"]

    def test_on_every_version_list_entry(self) -> None:
        out = galaxy.rewrite_versions(VERSIONS, BASE, "community", "general")
        assert all(entry["href"] for entry in out["data"])


class TestTheRewriteKeepsWhatTheClientNeeds:
    def test_the_digest_is_untouched(self) -> None:
        """The client checks our bytes against upstream's digest.

        That check is the whole security model of a pull-through cache, so a
        rewrite that dropped or altered `artifact.sha256` would turn a verified
        download into an unverified one without failing anything.
        """
        out = galaxy.rewrite_version(VERSION, BASE, "community", "general", "9.0.0")
        assert out["artifact"]["sha256"] == "b" * 64

    def test_dependencies_survive(self) -> None:
        """Dependency resolution reads these; losing them silently flattens it."""
        out = galaxy.rewrite_version(VERSION, BASE, "community", "general", "9.0.0")
        assert out["metadata"]["dependencies"] == {"ansible.posix": "*"}

    def test_upstream_cursors_are_dropped(self) -> None:
        """`links.next` upstream is a URL we have not rewritten.

        Passing it through would hand the client an escape hatch straight to the
        internet on the second page of a long version list.
        """
        out = galaxy.rewrite_versions(VERSIONS, BASE, "community", "general")
        assert out["links"]["next"] is None

    def test_the_download_url_is_ours_and_names_the_artifact(self) -> None:
        out = galaxy.rewrite_version(VERSION, BASE, "community", "general", "9.0.0")
        assert out["download_url"] == (
            f"{BASE}/v3/collections/community/general/versions/9.0.0/download/"
            "community-general-9.0.0.tar.gz"
        )


class TestArtifactResolution:
    def test_the_digest_becomes_the_substrate_form(self) -> None:
        artifact = galaxy.artifact_for("community", "general", "9.0.0", VERSION)
        assert artifact.digest == "sha256:" + "b" * 64

    def test_it_fetches_upstreams_download_url_not_a_constructed_one(self) -> None:
        """Resolution uses what the configured upstream said, never a guess.

        Upstream is free to serve artifacts from a CDN on another host; guessing
        the path would break the moment it did.
        """
        artifact = galaxy.artifact_for("community", "general", "9.0.0", VERSION)
        assert artifact.upstream_url == VERSION["download_url"]

    def test_a_version_with_no_download_url_is_an_upstream_error(self) -> None:
        """Not a crash, and not a 404 either — upstream answered but unusably."""
        with pytest.raises(UpstreamError):
            galaxy.artifact_for("community", "general", "9.0.0", {"artifact": {}})

    def test_the_filename_is_derived_not_taken_from_upstream(self) -> None:
        """It is interpolated into a storage key and a URL path.

        Upstream's `artifact.filename` is used to cross-check, never to build a
        path, so a hostile or merely odd value cannot reach either.
        """
        hostile = {**VERSION, "artifact": {**VERSION["artifact"], "filename": "../../etc/passwd"}}
        artifact = galaxy.artifact_for("community", "general", "9.0.0", hostile)
        assert artifact.filename == "community-general-9.0.0.tar.gz"


class TestPathSegmentsAreValidated:
    """These are interpolated into an upstream URL.

    The single configured upstream is what stops this being a request-forgery
    primitive; a segment that can escape the path would undo that.
    """

    @pytest.mark.parametrize(
        "value",
        ["..", "a/b", "http://evil", "Community", "with-dash", "with space", "", "a.b"],
    )
    def test_rejected(self, value: str) -> None:
        assert not galaxy.valid_segment(value)

    @pytest.mark.parametrize("value", ["community", "general", "my_collection", "ns0"])
    def test_accepted(self, value: str) -> None:
        assert galaxy.valid_segment(value)

    @pytest.mark.parametrize("value", ["../1.0.0", "1.0.0/../..", "a b", ""])
    def test_versions_rejected(self, value: str) -> None:
        assert not galaxy.valid_version(value)

    @pytest.mark.parametrize("value", ["1.0.0", "1.0.0-rc1", "2.1.0+build.5"])
    def test_versions_accepted(self, value: str) -> None:
        assert galaxy.valid_version(value)


class TestSealedNarrowsTheVersionList:
    """A sealed node must not advertise what it cannot serve.

    Offering a version whose artifact is absent sends the client down a path that
    404s *after* it has resolved, when it could have been given the newest
    version actually held.
    """

    def test_only_held_versions_remain(self) -> None:
        out = galaxy.restrict_to_cached(VERSIONS, {"8.0.0"})
        assert [e["version"] for e in out["data"]] == ["8.0.0"]
        assert out["meta"]["count"] == 1

    def test_holding_nothing_yields_an_empty_list_not_an_error(self) -> None:
        out = galaxy.restrict_to_cached(VERSIONS, set())
        assert out["data"] == []
        assert out["meta"]["count"] == 0

    def test_versions_are_read_back_out_of_the_stored_filenames(self) -> None:
        held = galaxy.versions_held(
            [
                "community-general-8.0.0.tar.gz",
                "community-general-9.0.0.tar.gz",
                "collection.json",
                "versions.json",
            ],
            "community",
            "general",
        )
        assert held == {"8.0.0", "9.0.0"}

    def test_another_collections_files_are_not_counted(self) -> None:
        held = galaxy.versions_held(["community-docker-3.0.0.tar.gz"], "community", "general")
        assert held == set()


class TestPaginationIsBounded:
    """`links.next` comes from upstream, so it decides how long we loop."""

    @pytest.fixture(autouse=True)
    def _upstream(self):
        before = settings.registry.package_cache.galaxy.upstream
        settings.registry.package_cache.galaxy.upstream = UPSTREAM
        yield
        settings.registry.package_cache.galaxy.upstream = before

    def test_a_relative_next_resolves_against_the_configured_upstream(self) -> None:
        assert galaxy._absolute_next("/api/v3/x/?offset=100") == f"{UPSTREAM}/api/v3/x/?offset=100"

    def test_a_next_on_another_host_is_refused(self) -> None:
        """Following it would walk us off the operator's chosen registry.

        Same reasoning as never fetching a client-supplied URL: the configured
        upstream is the whole of the request-forgery surface, and a `next` is
        just as much someone else's input as a query parameter.
        """
        assert galaxy._absolute_next("https://evil.example.com/api/v3/x/") == ""

    def test_absent_or_null_ends_the_walk(self) -> None:
        assert galaxy._absolute_next(None) == ""
        assert galaxy._absolute_next("") == ""
        assert galaxy._absolute_next(123) == ""
