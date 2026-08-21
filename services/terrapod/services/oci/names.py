"""Digest, reference and repository-name grammar for OCI Distribution (#1408).

Pure parsing and validation. No I/O, no storage, no framework — so the rules can
be tested exhaustively and reused by the router, the storage layer and the
pull-through cache without any of them re-deriving them.

Two reasons this module is strict to the point of pedantry:

**Repository names become storage keys.** A permissive parser is how ``..`` or a
leading ``/`` reaches the object store and turns a push into a write outside the
registry's prefix. Everything here rejects rather than sanitises: a name that is
not already valid is an error, never something to be cleaned up and used, because
sanitising invites a mismatch between what was validated and what is written.

**The digest is the trust boundary.** Content-addressed storage is only
trustworthy if the digest is *verified against the bytes* rather than believed.
This module parses and validates the form; callers must still confirm the content
hashes to it. Parsing succeeding means "this string is a well-formed digest", not
"these bytes are that digest".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# https://github.com/opencontainers/distribution-spec — repository names are one
# or more path components separated by "/", each component lowercase
# alphanumeric with limited internal separators. Anchored, because an unanchored
# match would happily accept "../../etc/passwd" as containing a valid name.
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
REPOSITORY_RE = re.compile(rf"^{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*$")

#: The spec's limit on a repository name. Enforced separately from the pattern
#: so the error can say *which* rule was broken.
REPOSITORY_MAX_LENGTH = 255

#: Tags are far more permissive than repository names — mixed case is allowed —
#: but may not begin with a period or hyphen, which is what stops a tag being
#: mistaken for a flag or a relative path.
TAG_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$")

#: ``algorithm:encoded``. The spec permits an extensible algorithm grammar; we
#: parse it generally and then narrow to what we will actually honour, so an
#: unknown-but-well-formed algorithm produces a clear "unsupported" rather than a
#: confusing "malformed".
DIGEST_RE = re.compile(
    r"^(?P<algorithm>[a-z0-9]+(?:[.+_-][a-z0-9]+)*):(?P<encoded>[a-zA-Z0-9=_-]+)$"
)

#: Algorithms Terrapod will accept, and the exact hex length each requires.
#: Restricting this is deliberate: accepting an algorithm we cannot verify would
#: mean storing content whose digest we can never re-check.
SUPPORTED_DIGEST_ALGORITHMS: dict[str, int] = {"sha256": 64, "sha512": 128}

#: Both supported algorithms encode as lowercase hex. The spec's *general*
#: encoded grammar is wider than hex (it also allows ``=``, ``_`` and ``-`` for
#: future algorithms), so length alone does not establish that a value is a
#: plausible sha256 — ``sha256:zzz...`` is 64 characters and is not a digest at
#: all. Checked explicitly rather than by narrowing the pattern, so the general
#: grammar stays available if another algorithm is ever supported.
_HEX = frozenset("0123456789abcdef")


class InvalidName(ValueError):
    """A repository name, tag or digest that does not satisfy the spec."""


@dataclass(frozen=True, slots=True)
class Digest:
    """A validated content digest.

    Frozen because a digest that could be mutated after validation would defeat
    the point of validating it.
    """

    algorithm: str
    encoded: str

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.encoded}"

    @property
    def storage_segment(self) -> str:
        """The digest rendered for use inside a storage key.

        ``:`` is replaced with ``/`` so that blobs shard by algorithm rather than
        landing in one enormous flat prefix — which matters on backends where
        listing a prefix is a paged operation, and costs nothing on the others.
        """
        return f"{self.algorithm}/{self.encoded}"


def parse_digest(value: str) -> Digest:
    """Parse and validate a digest string.

    Raises :class:`InvalidName` for anything malformed, of an algorithm we do not
    support, or of the wrong length for its algorithm. The length check is not
    redundant with the pattern: ``sha256:ab`` is well-formed by the grammar and
    is still not a sha256.
    """
    if not value:
        raise InvalidName("digest is empty")
    match = DIGEST_RE.match(value)
    if match is None:
        raise InvalidName(f"malformed digest: {value!r}")

    algorithm = match.group("algorithm")
    encoded = match.group("encoded")

    expected_length = SUPPORTED_DIGEST_ALGORITHMS.get(algorithm)
    if expected_length is None:
        raise InvalidName(f"unsupported digest algorithm: {algorithm!r}")
    if len(encoded) != expected_length:
        raise InvalidName(
            f"{algorithm} digest must be {expected_length} characters, got {len(encoded)}"
        )
    # Lowercase-only: the same bytes must always produce the same key, and
    # "sha256:AB..." vs "sha256:ab..." would otherwise be two entries for one
    # blob. Checked before the hex test so a case mistake reports as one.
    if encoded != encoded.lower():
        raise InvalidName(f"{algorithm} digest must be lowercase hex")
    if not set(encoded) <= _HEX:
        raise InvalidName(f"{algorithm} digest must be hex")

    return Digest(algorithm=algorithm, encoded=encoded)


def is_digest(value: str) -> bool:
    """Whether ``value`` is a well-formed, supported digest.

    Used to disambiguate a reference, where the same path segment may be either a
    tag or a digest.
    """
    try:
        parse_digest(value)
    except InvalidName:
        return False
    return True


def validate_repository(name: str) -> str:
    """Validate a repository name and return it unchanged.

    Returns the input rather than a normalised form on purpose: there is no
    normalisation here, so the value that was checked is exactly the value the
    caller goes on to use.
    """
    if not name:
        raise InvalidName("repository name is empty")
    if len(name) > REPOSITORY_MAX_LENGTH:
        raise InvalidName(
            f"repository name exceeds {REPOSITORY_MAX_LENGTH} characters ({len(name)})"
        )
    if REPOSITORY_RE.match(name) is None:
        raise InvalidName(f"invalid repository name: {name!r}")
    return name


def validate_tag(tag: str) -> str:
    """Validate a tag and return it unchanged."""
    if not tag:
        raise InvalidName("tag is empty")
    if TAG_RE.match(tag) is None:
        raise InvalidName(f"invalid tag: {tag!r}")
    return tag


@dataclass(frozen=True, slots=True)
class Reference:
    """A manifest reference: either a tag or a digest, never both.

    The spec puts both in the same path position, so every consumer has to
    disambiguate. Doing it once, here, keeps that decision from being made
    differently in three places.
    """

    tag: str | None
    digest: Digest | None

    @property
    def is_digest(self) -> bool:
        return self.digest is not None

    def __str__(self) -> str:
        return str(self.digest) if self.digest is not None else str(self.tag)


def parse_reference(value: str) -> Reference:
    """Parse a manifest reference.

    A digest is tried first. The two grammars do not overlap for supported
    algorithms — a digest contains ``:``, which no tag may — so the order is for
    clarity rather than correctness, and a malformed digest-looking value is
    reported as a bad digest rather than a bad tag, which is the more useful
    error.
    """
    if not value:
        raise InvalidName("reference is empty")
    if ":" in value:
        return Reference(tag=None, digest=parse_digest(value))
    return Reference(tag=validate_tag(value), digest=None)
