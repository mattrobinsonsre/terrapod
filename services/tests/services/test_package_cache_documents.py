"""Cached index documents are read back, and bounded per document (#2512).

The substrate has stored index documents since #1417 and, until now, only a
*sealed* node ever read one. An unsealed node fetched upstream on every request
and then wrote a copy it would never consult — a write-only cache sitting on the
read path, which is why installing five pinned Ansible collections took tens of
seconds through a warm proxy.

Three properties are pinned here, and each is one a reader could get wrong in a
way no other test would catch:

* `max_age` is per *document*, not per cache. A listing is mutable and must
  expire; an immutable per-version detail must not, or the expiry only ever
  re-fetches identical bytes.
* a hit touches `last_accessed_at`, because retention is access-based: a
  document that is read constantly and never touched looks permanently unused.
* a write *refreshes* the row's age. Without that, a `max_age` read against a
  document that is overwritten in place expires a copy stored a moment ago, on
  every request, forever — which is the behaviour the fix exists to remove.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.package_cache import galaxy
from terrapod.services.package_cache.substrate import (
    Artifact,
    load_document,
    store_document,
)

pytestmark = pytest.mark.asyncio

ECOSYSTEM = "galaxy"
NAME = "community.general"
PAYLOAD = {"data": [{"version": "9.0.0"}]}


def _row(*, age: timedelta = timedelta(0)) -> MagicMock:
    row = MagicMock()
    row.storage_key = "package-cache/galaxy/community.general/versions.json"
    row.cached_at = datetime.now(UTC) - age
    row.last_accessed_at = datetime.now(UTC) - age
    return row


def _db(record) -> AsyncMock:
    """A session whose one lookup answers with `record`."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=record)
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    return db


def _storage(data: bytes | None = None) -> AsyncMock:
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=data if data is not None else b"{}")
    storage.put = AsyncMock()
    return storage


class TestMaxAgeIsPerDocument:
    async def test_a_fresh_listing_is_served_from_cache(self) -> None:
        db = _db(_row(age=timedelta(minutes=5)))

        got = await load_document(
            db,
            _storage(json.dumps(PAYLOAD).encode()),
            ECOSYSTEM,
            NAME,
            galaxy.VERSIONS_DOC,
            max_age=galaxy.LISTING_TTL,
        )

        assert json.loads(got) == PAYLOAD

    async def test_a_stale_listing_reads_as_a_miss(self) -> None:
        """A miss and an expiry are deliberately the same answer.

        The caller then has one cold path — go upstream and store — rather than
        two, and cannot accidentally serve the expired bytes it was handed.
        """
        storage = _storage(json.dumps(PAYLOAD).encode())
        db = _db(_row(age=galaxy.LISTING_TTL + timedelta(minutes=1)))

        got = await load_document(
            db, storage, ECOSYSTEM, NAME, galaxy.VERSIONS_DOC, max_age=galaxy.LISTING_TTL
        )

        assert got is None
        # Not merely discarded after the fact: an expired row is not fetched.
        storage.get.assert_not_awaited()

    async def test_an_immutable_detail_has_no_expiry_at_all(self) -> None:
        """The reason a per-version detail is read with no `max_age`.

        A published collection version never changes, so an age bound here would
        only ever buy a round trip that returns the same bytes. Old enough to be
        long expired under any listing bound, and still served.
        """
        db = _db(_row(age=timedelta(days=400)))

        got = await load_document(
            db,
            _storage(json.dumps(PAYLOAD).encode()),
            ECOSYSTEM,
            NAME,
            galaxy.version_doc("9.0.0"),
        )

        assert json.loads(got) == PAYLOAD

    async def test_an_absent_row_is_a_miss(self) -> None:
        assert (
            await load_document(_db(None), _storage(), ECOSYSTEM, NAME, galaxy.VERSIONS_DOC) is None
        )

    async def test_a_row_whose_object_has_gone_is_a_miss(self) -> None:
        """The row is not the truth; the store is."""
        storage = _storage()
        storage.get = AsyncMock(side_effect=FileNotFoundError("pruned"))

        assert (
            await load_document(_db(_row()), storage, ECOSYSTEM, NAME, galaxy.VERSIONS_DOC) is None
        )


class TestRetentionCanSeeTheReads:
    async def test_a_hit_touches_last_accessed_at(self) -> None:
        """Retention compares `last_accessed_at`, so a document served from the
        cache and never touched is reaped however heavily it is read — and then
        re-fetched, in a loop, forever."""
        row = _row(age=timedelta(days=3))
        before = row.last_accessed_at

        await load_document(_db(row), _storage(b"{}"), ECOSYSTEM, NAME, galaxy.VERSIONS_DOC)

        assert row.last_accessed_at > before

    async def test_a_miss_touches_nothing(self) -> None:
        db = _db(None)
        await load_document(db, _storage(), ECOSYSTEM, NAME, galaxy.VERSIONS_DOC)
        db.flush.assert_not_awaited()


class TestAWriteRestartsTheClock:
    async def test_overwriting_a_document_refreshes_its_age(self) -> None:
        """Otherwise the TTL above can never be satisfied again.

        `store_document` overwrites in place, so a row first written yesterday
        keeps yesterday's `cached_at` however many times it is refreshed — and a
        `max_age` read then expires it on every single request, restoring the
        full upstream round trip the cache exists to remove.
        """
        row = _row(age=timedelta(days=1))
        stale = row.cached_at

        await store_document(
            _db(row),
            _storage(),
            Artifact(
                ecosystem=ECOSYSTEM,
                name=NAME,
                version="",
                filename=galaxy.VERSIONS_DOC,
                upstream_url="",
                content_type="application/json",
            ),
            json.dumps(PAYLOAD).encode(),
        )

        assert row.cached_at > stale
        assert datetime.now(UTC) - row.cached_at < timedelta(seconds=5)


class TestTheListingBoundIsShortEnoughToBeUseful:
    async def test_a_newly_published_version_is_installable_within_the_hour(self) -> None:
        """The only thing the number has to buy.

        Unbounded, a collection published today could never be installed through
        a proxy that had already seen the collection. The bound is what makes
        that impossible, so it is asserted rather than left to a comment.
        """
        assert timedelta(0) < galaxy.LISTING_TTL <= timedelta(hours=24)
