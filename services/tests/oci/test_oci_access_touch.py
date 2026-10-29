"""Which OCI reads mark an entity as accessed, and which deliberately do not (#2178).

Unit tier — the read-side helpers against a mocked session, no HTTP and no database.

The asymmetry is the point, and it is the collector's design rather than an
oversight, so it is worth a file of its own:

* a **manifest** read touches ``last_accessed_at``, because ``gc.py`` defines
  eviction as *"reclaiming space, keyed on last use, driven by capacity"* and says
  that *"if capacity ever has to be managed, the unit is the **image**: drop a
  manifest"*. That timestamp is the state such an eviction would read, and one
  ``UPDATE`` per pull is a fair price for having it when it arrives;
* a **blob** read must not, because the same document forbids the only use it
  could have: *"A layer lives exactly as long as some manifest references it, and
  nothing else may enter into that. Not its age, not when it was last served […]
  Evicting layers by age or by use would break registered images, which is
  corruption rather than eviction."*

Before #2178 both were touched, on the justification that
``artifact_retention_service`` reaps on access. That service queries
``CachedProviderPackage``, ``CachedBinary`` and ``CachedPackageFile``; the string
``oci`` does not appear in it. So the blob touch was a committed write per layer
per pull, on the hottest read path in a cache whose reason to exist is serving
reads in restricted networks, for a value nothing could ever legitimately read.

These are unit tests, so they assert the *decision* rather than the SQL. What
reaches the database is one ``UPDATE`` per dirtied row at request teardown, which
is a property of the session rather than of these functions.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import OCIBlob, OCIManifest
from terrapod.services.oci import registry_service
from terrapod.services.oci.names import Reference

#: Deliberately in the past, so "was it touched" is a real comparison rather than
#: an equality against a clock read in the same microsecond.
_STALE = datetime(2020, 1, 1, tzinfo=UTC)

_DIGEST = "sha256:" + "a" * 64


def _db(returns):
    """A session whose single query yields `returns`."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=returns)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _repo():
    repo = MagicMock()
    repo.id = 1
    return repo


@pytest.mark.asyncio
async def test_a_manifest_read_by_digest_marks_it_accessed():
    manifest = OCIManifest(last_accessed_at=_STALE)
    before = datetime.now(UTC) - timedelta(seconds=1)

    got = await registry_service.resolve_manifest(
        _db(manifest), _repo(), Reference(tag=None, digest=_DIGEST)
    )

    assert got is manifest
    assert manifest.last_accessed_at > before, (
        "a manifest read must mark it accessed — gc.py names the image as the unit "
        "of any future capacity eviction, keyed on last use"
    )


@pytest.mark.asyncio
async def test_a_manifest_read_by_tag_marks_it_accessed():
    """Both lookups in that function, not just the one a test happened to drive —
    the spec puts tag and digest in one path position and either can be the hot
    path of a pull."""
    manifest = OCIManifest(last_accessed_at=_STALE)
    before = datetime.now(UTC) - timedelta(seconds=1)

    got = await registry_service.resolve_manifest(
        _db(manifest), _repo(), Reference(tag="latest", digest=None)
    )

    assert got is manifest
    assert manifest.last_accessed_at > before


@pytest.mark.asyncio
async def test_a_BLOB_read_does_NOT_mark_it_accessed():
    """The one that was wrong, and the hot one: once per layer per pull.

    `gc.py` reclaims a layer only when no manifest references it, explicitly
    never by age and never by use — so the timestamp has no reader, and writing
    it was a committed write on the registry's busiest read path for nothing.
    """
    blob = OCIBlob(last_accessed_at=_STALE)

    got = await registry_service.get_repository_blob(_db(blob), _repo(), _DIGEST)

    assert got is blob
    assert blob.last_accessed_at == _STALE, (
        "a blob read must NOT mark it accessed: gc.py forbids evicting a layer by "
        "age or by use, so this is a write per layer per pull that nothing reads"
    )


@pytest.mark.asyncio
async def test_a_missing_row_is_returned_as_None_by_both():
    """`_touch` is a no-op on None so callers need no guard; removing it from the
    blob path must not have changed that for either."""
    assert (
        await registry_service.resolve_manifest(_db(None), _repo(), Reference(tag="x", digest=None))
        is None
    )
    assert await registry_service.get_repository_blob(_db(None), _repo(), _DIGEST) is None


def test_nothing_in_the_tree_reads_either_timestamp():
    """The claim the removal rests on, asserted rather than left in a commit
    message.

    If a reader is ever added — an operator-facing "last pulled" view is the
    obvious one — this fails, and whoever adds it has to decide deliberately
    whether the blob touch comes back rather than finding it already gone.
    """
    from pathlib import Path

    root = Path(registry_service.__file__).resolve().parents[2]
    readers = []
    for path in root.rglob("*.py"):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text()
        for attr in ("OCIBlob.last_accessed_at", "OCIManifest.last_accessed_at"):
            if attr in text:
                readers.append(f"{path.relative_to(root)}: {attr}")
    assert not readers, (
        "something now reads an OCI last_accessed_at column, so the retention "
        f"story has changed and #2178's reasoning needs revisiting: {readers}"
    )
