"""PyPI index handling (#1417).

The rewrite is where a package proxy can quietly do damage: change a URL and the
client installs from somewhere else; drop a hash and it stops checking; mangle
`requires-python` and it installs a wheel for the wrong interpreter. These pin
what may change and, more importantly, what may not.
"""

from __future__ import annotations

import pytest

from terrapod.services.package_cache import pypi

INDEX = {
    "meta": {"api-version": "1.0"},
    "name": "flask",
    "files": [
        {
            "filename": "flask-3.0.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/ab/cd/flask-3.0.0-py3-none-any.whl",
            "hashes": {"sha256": "a" * 64},
            "requires-python": ">=3.8",
        },
        {
            "filename": "flask-3.0.0.tar.gz",
            "url": "https://files.pythonhosted.org/packages/ef/gh/flask-3.0.0.tar.gz",
            "hashes": {"sha256": "b" * 64},
            "yanked": "broken sdist",
        },
    ],
}


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Flask", "flask"),
            ("FLASK", "flask"),
            ("zope.interface", "zope-interface"),
            ("zope_interface", "zope-interface"),
            ("a--b__c..d", "a-b-c-d"),
        ],
    )
    def test_pep503_normalisation(self, raw: str, expected: str) -> None:
        """Without this, one project is several cache entries of identical bytes."""
        assert pypi.normalise(raw) == expected


class TestRewrite:
    def test_only_the_url_changes(self) -> None:
        document = pypi.rewrite_json(INDEX, "Flask", file_url_base="/files")
        wheel = document["files"][0]

        assert wheel["url"] != INDEX["files"][0]["url"]
        # Everything a client makes decisions with is passed through untouched.
        assert wheel["hashes"] == {"sha256": "a" * 64}
        assert wheel["requires-python"] == ">=3.8"
        assert document["files"][1]["yanked"] == "broken sdist"
        assert document["meta"] == {"api-version": "1.0"}

    def test_the_name_is_normalised_in_the_response(self) -> None:
        assert pypi.rewrite_json(INDEX, "Flask", file_url_base="/f")["name"] == "flask"

    def test_entries_without_a_filename_are_dropped(self) -> None:
        """A malformed entry must not become a link to nothing."""
        document = pypi.rewrite_json(
            {"files": [{"url": "https://example.test/x"}, INDEX["files"][0]]},
            "flask",
            file_url_base="/f",
        )
        assert [f["filename"] for f in document["files"]] == ["flask-3.0.0-py3-none-any.whl"]


class TestHtmlRendering:
    """The PEP 503 form, for a client that did not ask for JSON."""

    def test_the_digest_is_carried_in_the_fragment(self) -> None:
        # Older pip reads its integrity check from the URL fragment; losing it
        # silently downgrades the install to unverified.
        html = pypi.render_html(pypi.rewrite_json(INDEX, "flask", file_url_base="/f"))
        assert f"#sha256={'a' * 64}" in html

    def test_requires_python_and_yanked_survive(self) -> None:
        html = pypi.render_html(pypi.rewrite_json(INDEX, "flask", file_url_base="/f"))
        assert 'data-requires-python="&gt;=3.8"' in html
        assert 'data-yanked="broken sdist"' in html

    def test_values_are_escaped(self) -> None:
        """The document is upstream's, so its contents are not trusted input."""
        hostile = {
            "name": "x",
            "files": [
                {
                    "filename": '"><script>alert(1)</script>',
                    "url": "https://example.test/a",
                    "requires-python": '"><script>',
                }
            ],
        }
        html = pypi.render_html(pypi.rewrite_json(hostile, "x", file_url_base="/f"))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestFileLookup:
    def test_finds_a_file_by_name(self) -> None:
        entry = pypi.find_file(INDEX, "flask-3.0.0.tar.gz")
        assert entry is not None and entry["hashes"]["sha256"] == "b" * 64

    def test_returns_none_for_an_unknown_file(self) -> None:
        assert pypi.find_file(INDEX, "flask-9.9.9.tar.gz") is None

    def test_artifact_carries_upstream_url_and_digest(self) -> None:
        artifact = pypi.artifact_for("Flask", INDEX["files"][0])
        assert artifact.name == "flask"
        assert artifact.version == "3.0.0"
        assert artifact.upstream_url.startswith("https://files.pythonhosted.org/")
        assert artifact.digest == f"sha256:{'a' * 64}"

    def test_a_file_with_no_hash_is_still_cacheable(self) -> None:
        """An index that publishes no digest is unusual, not fatal."""
        artifact = pypi.artifact_for("x", {"filename": "x-1.0.tar.gz", "url": "https://e.test/x"})
        assert artifact.digest == ""


class TestSealedIndex:
    """What a sealed node serves: an index of what it actually holds.

    Building this from cached rows rather than a stored copy of upstream's index
    is the point. An upstream index lists versions whose files are not here, pip
    resolves to one, and the install dies on a 503 the developer can do nothing
    about. An index of what is present resolves to something installable.
    """

    class _Row:
        def __init__(self, filename: str, digest: str = "") -> None:
            self.filename = filename
            self.digest = digest

    def test_only_cached_files_are_listed(self) -> None:
        document = pypi.index_from_cache(
            [
                self._Row("flask-3.0.0-py3-none-any.whl", f"sha256:{'a' * 64}"),
                self._Row("flask-2.0.0-py3-none-any.whl", f"sha256:{'b' * 64}"),
            ]
        )
        assert [f["filename"] for f in document["files"]] == [
            "flask-3.0.0-py3-none-any.whl",
            "flask-2.0.0-py3-none-any.whl",
        ]

    def test_digests_are_preserved_so_pip_still_verifies(self) -> None:
        document = pypi.index_from_cache([self._Row("x-1.0.whl", f"sha256:{'c' * 64}")])
        assert document["files"][0]["hashes"] == {"sha256": "c" * 64}

    def test_sidecars_are_advertised_not_listed_as_distributions(self) -> None:
        """A `.metadata` file is not something pip can install.

        Listed as its own entry, pip would try to resolve it as a distribution.
        Advertised as `core-metadata`, it keeps the fast resolve path working on
        a sealed node too.
        """
        document = pypi.index_from_cache(
            [
                self._Row("x-1.0-py3-none-any.whl"),
                self._Row("x-1.0-py3-none-any.whl" + pypi.METADATA_SUFFIX),
            ]
        )
        assert [f["filename"] for f in document["files"]] == ["x-1.0-py3-none-any.whl"]
        assert document["files"][0]["core-metadata"] is True

    def test_a_file_without_a_cached_sidecar_does_not_claim_one(self) -> None:
        """Claiming one we do not have makes pip request a 404 mid-resolve."""
        document = pypi.index_from_cache([self._Row("x-1.0-py3-none-any.whl")])
        assert "core-metadata" not in document["files"][0]

    def test_urls_are_relative_like_the_proxied_index(self) -> None:
        document = pypi.index_from_cache([self._Row("x-1.0-py3-none-any.whl")])
        assert document["files"][0]["url"] == "x-1.0-py3-none-any.whl"
