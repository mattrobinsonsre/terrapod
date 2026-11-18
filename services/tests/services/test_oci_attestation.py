"""Finding the Sigstore attestations an OCI registry publishes (#2589).

The fixtures are the real shapes, copied from ghcr.io for one of our own
published images, because the defect these tests exist to prevent is invisible
against an invented one: the index entry for the primary cosign signature
reports a media type that is not the bundle type, so a parser that filters the
index on `artifactType` keeps the attestations and silently drops the
signature.
"""

from __future__ import annotations

from terrapod.services.oci import attestation as att

#: The real index at `ghcr.io/mattrobinsonsre/terrapod-api:sha256-65dbafe2...`,
#: trimmed to the fields the parser reads. Three entries: the cosign signature
#: and two attestations.
#:
#: Note the FIRST entry's `artifactType`. It is
#: `application/vnd.oci.empty.v1+json` -- the config media type -- while the
#: manifest it points at declares the bundle type and carries a bundle layer.
#: That disagreement is the whole reason `_candidates_in` does not filter here.
REAL_INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "size": 877,
            "digest": "sha256:4f316b0716c42f7315f2b151476debda56e29b42e090a0d472fc2c6f38d1e5ab",
            "artifactType": "application/vnd.oci.empty.v1+json",
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "size": 813,
            "digest": "sha256:74dee0ea867d8702759000000000000000000000000000000000000000000000a",
            "artifactType": att.BUNDLE_ARTIFACT_TYPE,
            "annotations": {
                "dev.sigstore.bundle.content": "dsse-envelope",
                att.PREDICATE_ANNOTATION: "https://slsa.dev/provenance/v1",
            },
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "size": 815,
            "digest": "sha256:82cb1f8ba2537c0bd7b000000000000000000000000000000000000000000000b",
            "artifactType": att.BUNDLE_ARTIFACT_TYPE,
            "annotations": {
                "dev.sigstore.bundle.content": "dsse-envelope",
                att.PREDICATE_ANNOTATION: "https://spdx.dev/Document/v2.3",
            },
        },
    ],
}

#: The real manifest behind that first entry: typed as a bundle, with a bundle
#: layer, despite how the index described it.
REAL_SIGNATURE_MANIFEST = {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "artifactType": att.BUNDLE_ARTIFACT_TYPE,
    "config": {"mediaType": "application/vnd.oci.empty.v1+json"},
    "layers": [
        {
            "mediaType": att.BUNDLE_ARTIFACT_TYPE,
            "digest": "sha256:c506005836f1013000000000000000000000000000000000000000000000000c",
            "size": 10743,
        }
    ],
}

ORDINARY_IMAGE_MANIFEST = {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"mediaType": "application/vnd.oci.image.config.v1+json"},
    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
}


class TestTheBundleTag:
    def test_the_colon_becomes_a_hyphen(self) -> None:
        # A tag cannot contain a colon; that is the entire reason, and it is
        # worth a test because the substitution otherwise looks arbitrary.
        assert att.bundle_tag("sha256:abc123") == "sha256-abc123"

    def test_only_the_first_colon_is_substituted(self) -> None:
        assert att.bundle_tag("sha256:ab:cd") == "sha256-ab:cd"


class TestEveryIndexEntryIsACandidate:
    """The regression this module exists for.

    Filtering the index on the bundle `artifactType` drops the primary cosign
    signature, because its entry is typed `application/vnd.oci.empty.v1+json`
    there. Two of three survive, and the missing one is the signature rather
    than an attestation.
    """

    def test_all_three_real_entries_are_candidates(self) -> None:
        got = att._candidates_in(REAL_INDEX)
        assert len(got) == 3, "the signature entry was dropped -- see the module docstring"

    def test_the_mistyped_signature_entry_is_kept(self) -> None:
        digests = {c.digest for c in att._candidates_in(REAL_INDEX)}
        assert "sha256:4f316b0716c42f7315f2b151476debda56e29b42e090a0d472fc2c6f38d1e5ab" in digests

    def test_the_index_annotation_is_carried_as_a_hint(self) -> None:
        def predicate_of(prefix: str) -> str:
            return next(
                c.predicate for c in att._candidates_in(REAL_INDEX) if c.digest.startswith(prefix)
            )

        assert predicate_of("sha256:74dee0ea") == "https://slsa.dev/provenance/v1"
        assert predicate_of("sha256:82cb1f8b") == "https://spdx.dev/Document/v2.3"

    def test_the_signature_entry_carries_no_predicate(self) -> None:
        """Absent rather than wrong: the signature is not an attestation.

        A caller choosing by predicate must therefore not assume every bundle
        has one.
        """
        sig = next(
            c for c in att._candidates_in(REAL_INDEX) if c.digest.startswith("sha256:4f316b")
        )
        assert sig.predicate == ""

    def test_entries_without_a_digest_are_skipped(self) -> None:
        assert att._candidates_in({"manifests": [{"size": 1}, "not-a-dict", {"digest": ""}]}) == []

    def test_an_index_with_no_manifests_yields_nothing(self) -> None:
        assert att._candidates_in({}) == []
        assert att._candidates_in({"manifests": []}) == []


class TestTheManifestDecidesWhetherItIsABundle:
    def test_the_real_signature_manifest_is_recognised(self) -> None:
        # The one the index mislabelled. If this regresses, discovery finds the
        # attestations and misses the signature.
        assert att._is_bundle_manifest(REAL_SIGNATURE_MANIFEST) is True

    def test_a_manifest_typed_as_a_bundle_is_recognised(self) -> None:
        assert att._is_bundle_manifest({"artifactType": att.BUNDLE_ARTIFACT_TYPE}) is True

    def test_a_bundle_layer_alone_is_enough(self) -> None:
        """Either signal suffices, because the two disagree in the wild."""
        assert (
            att._is_bundle_manifest({"layers": [{"mediaType": att.BUNDLE_ARTIFACT_TYPE}]}) is True
        )

    def test_an_ordinary_image_manifest_is_rejected(self) -> None:
        # The negative control. Without it, a predicate that accepted
        # everything would pass every test above.
        assert att._is_bundle_manifest(ORDINARY_IMAGE_MANIFEST) is False

    def test_an_empty_manifest_is_rejected(self) -> None:
        assert att._is_bundle_manifest({}) is False

    def test_a_malformed_layer_list_does_not_raise(self) -> None:
        assert att._is_bundle_manifest({"layers": ["not-a-dict", None]}) is False
