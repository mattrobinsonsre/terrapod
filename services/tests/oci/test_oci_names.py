"""Grammar tests for the OCI registry surface (#1408).

Unit tier: pure parsing, no DB, no Redis, no app. The negative cases carry the
weight here — repository names become storage keys, so a parser that accepts
``../`` is a write outside the registry's prefix, and a digest parser that
accepts the wrong length lets content be stored under a key nothing can verify.
"""

import pytest

from terrapod.services.oci.names import (
    REPOSITORY_MAX_LENGTH,
    Digest,
    InvalidName,
    is_digest,
    parse_digest,
    parse_reference,
    validate_repository,
    validate_tag,
)

SHA256 = "sha256:" + "a" * 64
SHA512 = "sha512:" + "b" * 128


class TestParseDigest:
    def test_accepts_supported_algorithms(self) -> None:
        assert parse_digest(SHA256) == Digest("sha256", "a" * 64)
        assert parse_digest(SHA512) == Digest("sha512", "b" * 128)

    def test_roundtrips_to_its_input(self) -> None:
        assert str(parse_digest(SHA256)) == SHA256

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "sha256",  # no separator
            "sha256:",  # no encoded part
            ":" + "a" * 64,  # no algorithm
            "sha256:" + "a" * 63,  # too short for the algorithm
            "sha256:" + "a" * 65,  # too long
            "sha256:" + "A" * 64,  # uppercase: would key the same blob twice
            "sha256:" + "z" * 64,  # not hex
            "md5:" + "a" * 32,  # well-formed but unsupported
            "sha256:../../etc/passwd",
        ],
    )
    def test_rejects(self, value: str) -> None:
        with pytest.raises(InvalidName):
            parse_digest(value)

    def test_length_is_checked_per_algorithm(self) -> None:
        """A sha512-length value under a sha256 label is not a sha256."""
        with pytest.raises(InvalidName, match="64 characters"):
            parse_digest("sha256:" + "a" * 128)

    def test_storage_segment_shards_by_algorithm(self) -> None:
        assert parse_digest(SHA256).storage_segment == "sha256/" + "a" * 64

    def test_is_digest_predicate(self) -> None:
        assert is_digest(SHA256)
        assert not is_digest("latest")
        assert not is_digest("md5:" + "a" * 32)


class TestValidateRepository:
    @pytest.mark.parametrize(
        "name",
        [
            "ansible-ee",
            "terrapod/ansible-ee",
            "a",
            "org/team/project/image",
            "name.with.dots",
            "name_with_underscore",
            "name__double__underscore",
            "name--double--hyphen",
            "n0/w1th/d1g1ts",
        ],
    )
    def test_accepts(self, name: str) -> None:
        assert validate_repository(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "UPPERCASE",  # lowercase only
            "/leading-slash",
            "trailing-slash/",
            "double//slash",
            "-leading-hyphen",
            ".leading-dot",
            "trailing-hyphen-",
            "has space",
            "..",
            "../etc/passwd",
            "foo/../../bar",
            "foo/./bar",
        ],
    )
    def test_rejects(self, name: str) -> None:
        with pytest.raises(InvalidName):
            validate_repository(name)

    def test_rejects_over_length(self) -> None:
        with pytest.raises(InvalidName, match=str(REPOSITORY_MAX_LENGTH)):
            validate_repository("a" * (REPOSITORY_MAX_LENGTH + 1))

    def test_accepts_at_exactly_the_limit(self) -> None:
        name = "a" * REPOSITORY_MAX_LENGTH
        assert validate_repository(name) == name

    def test_returns_input_unchanged_rather_than_sanitising(self) -> None:
        """Validation must never silently rewrite: the checked value and the
        used value have to be the same string."""
        assert validate_repository("terrapod/ansible-ee") == "terrapod/ansible-ee"


class TestValidateTag:
    @pytest.mark.parametrize(
        "tag", ["latest", "v1.2.3", "2.16-rhel9", "MixedCase", "_leading_underscore", "a" * 128]
    )
    def test_accepts(self, tag: str) -> None:
        assert validate_tag(tag) == tag

    @pytest.mark.parametrize(
        "tag",
        [
            "",
            ".leading-dot",  # could be read as a relative path
            "-leading-hyphen",  # could be read as a flag
            "a" * 129,  # one over the limit
            "has space",
            "has/slash",
            "has:colon",
        ],
    )
    def test_rejects(self, tag: str) -> None:
        with pytest.raises(InvalidName):
            validate_tag(tag)


class TestParseReference:
    def test_digest_reference(self) -> None:
        ref = parse_reference(SHA256)
        assert ref.is_digest
        assert ref.digest is not None and str(ref.digest) == SHA256
        assert ref.tag is None

    def test_tag_reference(self) -> None:
        ref = parse_reference("latest")
        assert not ref.is_digest
        assert ref.tag == "latest"
        assert ref.digest is None

    def test_a_colon_makes_it_a_digest_error_not_a_tag_error(self) -> None:
        """Anything containing ':' is judged as a digest, so a malformed digest
        reports as one — the useful error — rather than as an invalid tag."""
        with pytest.raises(InvalidName, match="digest"):
            parse_reference("sha256:nope")

    def test_rejects_empty(self) -> None:
        with pytest.raises(InvalidName):
            parse_reference("")

    def test_str_roundtrips_both_forms(self) -> None:
        assert str(parse_reference(SHA256)) == SHA256
        assert str(parse_reference("latest")) == "latest"
