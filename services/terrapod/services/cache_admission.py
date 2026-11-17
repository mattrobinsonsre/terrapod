"""The rule an operator upload is admitted by, shared across every cache (#2599).

Three caches accept uploads -- packages, engine/platform-tool binaries, provider
archives -- and they do not share a row shape: `CachedPackageFile` records a size
and a storage key, `CachedBinary` derives its key from its coordinates and keeps
only a `shasum`, and `CachedProviderPackage` carries an `h1_hash` as well. Forcing
one row shape on all three would be a worse distortion than the duplication it
removed.

What they DO share is the decision, and that is the part that must not drift:

  * re-uploading identical bytes is not a replacement, so it is not refused;
  * anything else at occupied coordinates is refused unless replacement was asked
    for, because what it displaces may be what a run already depends on;
  * a replacement records what it displaced, or that fact is gone the instant the
    row is rewritten.

Those three sentences are the whole of it, and they were previously written out
once in `package_cache/uploads.py`. Copying them into two more modules is how the
three come to disagree -- a cache that quietly overwrites, or one that refuses an
identical re-upload an operator is using to repair a truncated object. So the rule
lives here as a pure function over facts, and each cache keeps its own I/O.
"""

from __future__ import annotations

import enum

__all__ = ["Outcome", "decide"]


class Outcome(enum.Enum):
    """What an upload at these coordinates should do."""

    #: Nothing is there. Create the row, write the object.
    FRESH = "fresh"
    #: The identical bytes are already there, admitted by an operator. Rewrite the
    #: object -- this is the ONLY repair for one that went missing or was truncated
    #: under a row that is otherwise correct, and for an uploaded artifact there is
    #: no upstream to re-fetch from. Leave the provenance alone: `uploaded_by` is
    #: the first assertion of these bytes and that is the honest answer to "who put
    #: this here".
    UNCHANGED = "unchanged"
    #: Something else is there and replacement was asked for. Record what it
    #: displaced.
    REPLACE = "replace"
    #: Something else is there and replacement was NOT asked for. Refuse.
    CONFLICT = "conflict"


def decide(
    *,
    occupied: bool,
    existing_is_upload: bool,
    existing_digest: str,
    new_digest: str,
    replace: bool,
) -> Outcome:
    """Decide an upload's fate from the facts, for any cache.

    `existing_is_upload` is deliberately separate from `existing_digest`: bytes
    that match something FETCHED are not an operator re-asserting their own
    artifact, they are an operator uploading over upstream content, and that is a
    replacement they must ask for. Treating a digest match alone as `UNCHANGED`
    would let an upload silently take ownership of a fetched row -- changing who
    the ledger says vouched for it, without anyone requesting a change.
    """
    if not occupied:
        return Outcome.FRESH
    if existing_is_upload and existing_digest == new_digest and not replace:
        return Outcome.UNCHANGED
    if not replace:
        return Outcome.CONFLICT
    return Outcome.REPLACE
